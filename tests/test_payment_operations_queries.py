"""The reads an operator recovering payments actually makes.

Nothing here writes a payment. What is asserted is which payments a work list contains, in
which order, how far it goes, and whether one payment can be understood without joining three
tables by hand.

The set matters more than it looks. A listing that quietly included settled payments would
bury the ones that need somebody under the ones that do not, and a listing that quietly
excluded ADMITTED would hide exactly the payments a crash leaves behind.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.audit.models import ActorType
from agentrank_api.checkout.execution import CheckoutExecutionService
from agentrank_api.checkout.models import CheckoutSession, CheckoutStatus
from agentrank_api.checkout.quote import QuotedLine
from agentrank_api.checkout.repository import CheckoutRepository
from agentrank_api.commerce.repository import CatalogRepository, MerchantRepository
from agentrank_api.constraints.repository import IntentConstraintRepository
from agentrank_api.constraints.rules import ConstraintOperator, IntentConstraintSpec
from agentrank_api.errors import NotFoundError
from agentrank_api.inventory.models import ReservationStatus
from agentrank_api.mandates.models import SpendingMandate
from agentrank_api.mandates.repository import MandateRepository
from agentrank_api.payments.admission import PaymentAdmissionService
from agentrank_api.payments.execution import PaymentExecutionService
from agentrank_api.payments.fake import FakeOutcome, FakePaymentProvider
from agentrank_api.payments.models import OPEN_STATUSES, PaymentAttempt, PaymentAttemptStatus
from agentrank_api.payments.operations import (
    MAX_EVENT_LIMIT,
    UNRESOLVED_STATUSES,
    PaymentOperationsService,
)
from agentrank_api.payments.repository import (
    MAX_UNRESOLVED_LIMIT,
    PaymentAttemptRepository,
    bounded_unresolved_limit,
)

pytestmark = pytest.mark.anyio

NOW = datetime.now(UTC)
HOUR = timedelta(hours=1)
PRICE = 499900
STOCK = 20
BLACK = IntentConstraintSpec.required_attribute("color", ConstraintOperator.EQ, "black")


@dataclass(frozen=True, slots=True)
class Shop:
    merchant_id: uuid.UUID
    black: uuid.UUID


@pytest.fixture
async def shop(session: AsyncSession) -> Shop:
    """A merchant with plenty of stock, so a test can create many payments at once."""
    merchant = await MerchantRepository(session).create(slug="ampere-supply", name="Ampere")
    catalog = CatalogRepository(session)
    product = await catalog.create_product(
        merchant_id=merchant.id, external_id="amp-1", title="Charger", category="chargers"
    )
    black = await catalog.create_variant(
        product=product,
        sku="AMP-BLACK",
        price_amount_minor=PRICE,
        currency="INR",
        inventory_quantity=STOCK,
        attributes={"color": "black"},
    )
    await session.commit()
    return Shop(merchant_id=merchant.id, black=black.id)


async def a_mandate(session: AsyncSession, shop: Shop) -> SpendingMandate:
    """A fresh single purchase authorization.

    One per payment, because a mandate allows exactly one non terminal attempt and every test
    here that builds several payments needs several mandates rather than several checkouts.
    """
    mandate = await MandateRepository(session).create(
        merchant_id=shop.merchant_id,
        max_total_amount_minor=PRICE,
        currency="INR",
        valid_from=NOW - HOUR,
        valid_until=NOW + HOUR,
    )
    await IntentConstraintRepository(session).create(
        merchant_id=shop.merchant_id, mandate_id=mandate.id, specs=[BLACK]
    )
    await session.commit()
    return mandate


async def prepared(session: AsyncSession, shop: Shop) -> CheckoutSession:
    checkout = await CheckoutRepository(session).create(
        merchant_id=shop.merchant_id,
        mandate_id=(await a_mandate(session, shop)).id,
        currency="INR",
        lines=[
            QuotedLine(
                variant_id=shop.black,
                quantity=1,
                unit_price_amount_minor=PRICE,
                product_category="chargers",
                variant_attributes={"color": "black"},
            )
        ],
        expires_at=NOW + HOUR,
    )
    await session.commit()
    readiness = await CheckoutExecutionService(session).prepare_execution(checkout.id, at=NOW)
    assert readiness.ready
    return checkout


async def admitted(session: AsyncSession, shop: Shop, *, key: str) -> PaymentAttempt:
    """A payment that has provably never reached a provider."""
    checkout = await prepared(session, shop)
    admission = await PaymentAdmissionService(session).admit_payment(
        checkout.id, idempotency_key=key, at=NOW
    )
    assert admission.attempt is not None
    return admission.attempt


async def settled(
    session: AsyncSession, shop: Shop, *, key: str, outcome: FakeOutcome
) -> PaymentAttempt:
    """A payment dispatched to a provider configured to answer in one particular way."""
    attempt = await admitted(session, shop, key=key)
    provider = FakePaymentProvider(default=outcome)
    await PaymentExecutionService(session, provider).dispatch(attempt.id)
    resolved = await PaymentAttemptRepository(session).get(attempt.id)
    assert resolved is not None
    return resolved


async def force_in_flight(session: AsyncSession, attempt_id: uuid.UUID) -> None:
    """Leave an attempt where a crash between the dispatch commit and the wire would leave it.

    Written by hand because no service can produce it deliberately, which is the point: a
    process that dies mid dispatch is the only thing that does, and an operator listing has to
    show what it left behind. It moves an ADMITTED attempt, which is the only state the
    database trigger will accept this transition from.
    """
    await session.execute(
        text(
            "UPDATE payment_attempt SET status = 'IN_FLIGHT', dispatched_at = now() WHERE id = :id"
        ),
        {"id": attempt_id},
    )
    await session.commit()
    session.expire_all()


async def test_the_operational_definition_of_unresolved_is_the_domain_one(
    session: AsyncSession,
) -> None:
    """One definition, referenced twice, so the two cannot drift.

    `OPEN_STATUSES` already means "may still reach a provider or is waiting on one", which is
    exactly what an operator work list is. A second tuple listing the same three values would
    be a copy that a fourth status could be added to only one of.
    """
    assert UNRESOLVED_STATUSES is OPEN_STATUSES
    assert set(UNRESOLVED_STATUSES) == {
        PaymentAttemptStatus.ADMITTED,
        PaymentAttemptStatus.IN_FLIGHT,
        PaymentAttemptStatus.UNKNOWN,
    }


async def test_the_work_list_holds_every_unresolved_state_and_neither_terminal_one(
    session: AsyncSession, shop: Shop
) -> None:
    """The whole contract of the listing, in one table of five payments.

    ADMITTED is there because a crash after admission leaves one and nothing else would ever
    surface it. IN_FLIGHT is there because a crash mid dispatch leaves one and it must never be
    re-sent. UNKNOWN is there because that is what an ambiguous answer produces. The two
    terminal states are absent because there is nothing left to do about either.
    """
    stuck = await admitted(session, shop, key="pay-admitted-01")
    crashed = await admitted(session, shop, key="pay-inflight-01")
    await force_in_flight(session, crashed.id)
    unknown = await settled(session, shop, key="pay-unknown-001", outcome=FakeOutcome.AMBIGUOUS)
    paid = await settled(session, shop, key="pay-success-001", outcome=FakeOutcome.SUCCESS)
    declined = await settled(session, shop, key="pay-decline-001", outcome=FakeOutcome.DECLINE)

    listing = await PaymentOperationsService(session).list_unresolved()

    assert {payment.attempt_id for payment in listing.payments} == {
        stuck.id,
        crashed.id,
        unknown.id,
    }
    listed = {payment.attempt_id: payment.status for payment in listing.payments}
    assert listed[stuck.id] is PaymentAttemptStatus.ADMITTED
    assert listed[crashed.id] is PaymentAttemptStatus.IN_FLIGHT
    assert listed[unknown.id] is PaymentAttemptStatus.UNKNOWN
    assert paid.id not in listed
    assert declined.id not in listed


async def test_the_work_list_is_oldest_first_and_says_when_it_looked(
    session: AsyncSession, shop: Shop
) -> None:
    """Oldest means admitted longest ago, and the clock is the database's.

    Three payments admitted in order come back in that order, and every age is measured from
    one instant so that two rows in one listing cannot be aged against two readings. The
    instant comes from PostgreSQL rather than from the machine running the command, which is
    what stops a skewed operator clock producing a negative age.
    """
    first = await admitted(session, shop, key="pay-first-0001")
    second = await admitted(session, shop, key="pay-second-001")
    third = await admitted(session, shop, key="pay-third-0001")

    listing = await PaymentOperationsService(session).list_unresolved()

    assert [payment.attempt_id for payment in listing.payments] == [first.id, second.id, third.id]
    ages = [payment.age(listing.observed_at) for payment in listing.payments]
    assert all(age >= timedelta(0) for age in ages)
    # Oldest first means the oldest age first, which is the same claim read the other way.
    assert ages == sorted(ages, reverse=True)


async def test_the_work_list_carries_what_each_payment_belongs_to(
    session: AsyncSession, shop: Shop
) -> None:
    """One row, everything an operator has to know, and no second query to get it.

    A listing that gave back attempts alone would make an operator ask what checkout each one
    named, what the hold currently says and whether the quote had already been paid, which for
    fifty payments is a hundred and fifty more statements.
    """
    attempt = await settled(session, shop, key="pay-unknown-001", outcome=FakeOutcome.AMBIGUOUS)

    listing = await PaymentOperationsService(session).list_unresolved()
    [payment] = listing.payments

    assert payment.attempt_id == attempt.id
    assert payment.status is PaymentAttemptStatus.UNKNOWN
    assert payment.merchant_id == shop.merchant_id
    assert payment.checkout_id == attempt.checkout_id
    assert payment.mandate_id == attempt.mandate_id
    assert payment.reservation_id == attempt.reservation_id
    assert payment.idempotency_key == "pay-unknown-001"
    assert payment.amount_minor == PRICE
    assert payment.currency == "INR"
    # The current state of both, not the state they were in when the payment was admitted.
    assert payment.checkout_status is CheckoutStatus.OPEN
    assert payment.reservation_status is ReservationStatus.COMMITTED
    assert payment.dispatched_at is not None
    # UNKNOWN is not a resolution, so nothing stamped one.
    assert payment.resolved_at is None
    assert payment.failure_code is None
    assert payment.is_unresolved


async def test_the_work_list_is_bounded_and_says_which_bound_it_used(
    session: AsyncSession, shop: Shop
) -> None:
    """An operator command must not be able to ask for the whole table.

    The limit is clamped rather than refused, because the caller is a person typing a number
    and the useful answer to a mistake is a screenful. `truncated` is what separates a short
    list from a cut off one, which an operator cannot work out from the length alone.
    """
    for index in range(4):
        await admitted(session, shop, key=f"pay-bounded-{index:03d}")

    operations = PaymentOperationsService(session)
    capped = await operations.list_unresolved(limit=2)

    assert capped.limit == 2
    assert len(capped.payments) == 2
    assert capped.truncated

    whole = await operations.list_unresolved(limit=10)
    assert len(whole.payments) == 4
    assert whole.truncated is False
    # The first two of the full listing are the ones the bounded read returned, because the
    # order is total rather than arbitrary.
    assert [payment.attempt_id for payment in capped.payments] == [
        payment.attempt_id for payment in whole.payments[:2]
    ]

    absurd = await operations.list_unresolved(limit=10_000)
    assert absurd.limit == MAX_UNRESOLVED_LIMIT
    assert bounded_unresolved_limit(0) == 1
    assert bounded_unresolved_limit(-5) == 1
    assert bounded_unresolved_limit(MAX_UNRESOLVED_LIMIT + 1) == MAX_UNRESOLVED_LIMIT


async def test_showing_one_payment_reads_its_state_and_its_trail_separately(
    session: AsyncSession, shop: Shop
) -> None:
    """Authoritative state comes from the row, and the events are beside it as history.

    The distinction is the reason this assertion is written twice over. Every field describing
    the payment is read from `payment_attempt` and its joins. The events are a bounded tail of
    what was recorded, in the trail's own order, and nothing is inferred from them.
    """
    attempt = await settled(session, shop, key="pay-unknown-001", outcome=FakeOutcome.AMBIGUOUS)

    view = await PaymentOperationsService(session).show(attempt.id)

    assert view.payment.attempt_id == attempt.id
    assert view.payment.status is PaymentAttemptStatus.UNKNOWN
    assert view.payment.age(view.observed_at) >= timedelta(0)
    assert [event.event_type for event in view.events] == ["payment.admitted", "payment.unknown"]
    assert view.events[0].actor_type is ActorType.BUYER
    assert view.events[-1].actor_type is ActorType.PAYMENT_PROVIDER
    assert view.events[-1].payload["status"] == "UNKNOWN"


async def test_showing_a_settled_payment_works_and_showing_a_missing_one_raises(
    session: AsyncSession, shop: Shop
) -> None:
    """The detail view is not restricted to unresolved payments, and it does not invent one.

    An operator who has just resolved something has to read it back, so a settled payment is
    shown rather than hidden. An identifier that names nothing raises rather than returning an
    empty view, because a caller naming an attempt has already decided it should exist.
    """
    paid = await settled(session, shop, key="pay-success-001", outcome=FakeOutcome.SUCCESS)

    operations = PaymentOperationsService(session)
    view = await operations.show(paid.id)

    assert view.payment.status is PaymentAttemptStatus.SUCCEEDED
    assert view.payment.checkout_status is CheckoutStatus.PAID
    assert view.payment.reservation_status is ReservationStatus.CONSUMED
    assert view.payment.provider_reference is not None
    assert view.payment.resolved_at is not None
    assert view.payment.is_unresolved is False

    with pytest.raises(NotFoundError) as refused:
        await operations.show(uuid.uuid7())
    assert refused.value.resource == "payment_attempt"


async def test_the_event_tail_is_bounded(session: AsyncSession, shop: Shop) -> None:
    """The trail only grows, so the read that includes it has a ceiling like every other one."""
    attempt = await settled(session, shop, key="pay-unknown-001", outcome=FakeOutcome.AMBIGUOUS)

    operations = PaymentOperationsService(session)
    assert len((await operations.show(attempt.id, events=1)).events) == 1
    # Asking for more than the ceiling is clamped rather than refused, exactly as the listing
    # bound is, and the payment here has fewer events than either number anyway.
    assert len((await operations.show(attempt.id, events=MAX_EVENT_LIMIT + 100)).events) == 2


async def test_the_summary_counts_every_state_including_the_empty_ones(
    session: AsyncSession, shop: Shop
) -> None:
    """Counts an operator can read at a glance, with a stable set of lines.

    A status with no payments is reported as zero rather than omitted, so the shape of the
    output does not change with the data. The terminal counts are lifetime totals and are
    documented as such: nothing here defines a recent window, because nobody has set a policy
    for how long recent is.
    """
    await admitted(session, shop, key="pay-admitted-01")
    await settled(session, shop, key="pay-unknown-001", outcome=FakeOutcome.AMBIGUOUS)
    await settled(session, shop, key="pay-success-001", outcome=FakeOutcome.SUCCESS)
    await settled(session, shop, key="pay-decline-001", outcome=FakeOutcome.DECLINE)

    summary = await PaymentOperationsService(session).counts()

    assert summary.counts == {
        PaymentAttemptStatus.ADMITTED: 1,
        PaymentAttemptStatus.IN_FLIGHT: 0,
        PaymentAttemptStatus.UNKNOWN: 1,
        PaymentAttemptStatus.SUCCEEDED: 1,
        PaymentAttemptStatus.FAILED: 1,
    }
    assert summary.unresolved == 2


async def test_an_empty_work_list_is_an_empty_list_rather_than_an_error(
    session: AsyncSession,
) -> None:
    """The healthy case. Nothing unresolved is the answer, not the absence of one."""
    operations = PaymentOperationsService(session)
    listing = await operations.list_unresolved()

    assert listing.payments == ()
    assert listing.truncated is False
    assert listing.observed_at is not None
    assert (await operations.counts()).unresolved == 0
