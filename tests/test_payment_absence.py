"""An unresolved payment the provider has no record of, and how it ends.

The state this file exists for is the one a review found could last forever. A payment is
admitted, the dispatch mark commits, the process dies before the provider is ever called, and
the attempt is left unresolved. Every query afterwards finds nothing. Nothing may release the
stock on the strength of that, because "I cannot find it" is not "it never happened", and the
mandate stays held by a non terminal attempt for as long as the answer stays the first one.

What ends it is the provider saying the stronger thing. Once a processor guarantees that the
identity was never executed and that nothing can appear from the original dispatch, no money
moved, and the attempt may fail, the hold may go back and the checkout may be paid for again.

The load bearing assertion in the whole file is the execute count. It is zero from the first
line to the last, because the scenario is a crash before the network call and none of the
recovery is allowed to turn into a second attempt at charging anybody.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.audit.repository import AuditRepository
from agentrank_api.checkout.execution import CheckoutExecutionService
from agentrank_api.checkout.models import CheckoutSession, CheckoutStatus
from agentrank_api.checkout.quote import QuotedLine
from agentrank_api.checkout.repository import CheckoutRepository
from agentrank_api.commerce.models import Variant
from agentrank_api.commerce.repository import CatalogRepository, MerchantRepository
from agentrank_api.constraints.repository import IntentConstraintRepository
from agentrank_api.constraints.rules import ConstraintOperator, IntentConstraintSpec
from agentrank_api.inventory.models import ReservationStatus
from agentrank_api.inventory.repository import InventoryReservationRepository
from agentrank_api.inventory.service import RESERVATION_RESOURCE
from agentrank_api.mandates.models import SpendingMandate
from agentrank_api.mandates.repository import MandateRepository
from agentrank_api.payments.admission import PAYMENT_RESOURCE, PaymentAdmissionService
from agentrank_api.payments.execution import PROVIDER_NEVER_EXECUTED, PaymentExecutionService
from agentrank_api.payments.fake import FakeOutcome, FakePaymentProvider
from agentrank_api.payments.models import OutcomeSource, PaymentAttempt, PaymentAttemptStatus
from agentrank_api.payments.provider import (
    PaymentInstruction,
    PaymentQuery,
    ProviderQueryResult,
    ProviderResult,
)
from agentrank_api.payments.repository import PaymentAttemptRepository

pytestmark = pytest.mark.anyio

NOW = datetime.now(UTC)
HOUR = timedelta(hours=1)
MINUTE = timedelta(minutes=1)
PRICE = 499900
STOCK = 5
KEY = "pay-ampere-0001"
OTHER_KEY = "pay-ampere-0002"
BLACK = IntentConstraintSpec.required_attribute("color", ConstraintOperator.EQ, "black")

# What the fake guarantees about its own visibility. It lives here rather than in the
# application because it is a property of a provider, and nothing above the provider interface
# is allowed to know a duration at all.
VISIBILITY_WINDOW = 5 * MINUTE


@dataclass(frozen=True, slots=True)
class Shop:
    merchant_id: uuid.UUID
    mandate: SpendingMandate
    black: uuid.UUID


async def build_shop(session: AsyncSession) -> Shop:
    merchant = await MerchantRepository(session).create(slug="ampere-supply", name="Ampere")
    mandate = await MandateRepository(session).create(
        merchant_id=merchant.id,
        max_total_amount_minor=PRICE,
        currency="INR",
        valid_from=NOW - HOUR,
        valid_until=NOW + HOUR,
    )
    await IntentConstraintRepository(session).create(
        merchant_id=merchant.id, mandate_id=mandate.id, specs=[BLACK]
    )
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
    return Shop(merchant_id=merchant.id, mandate=mandate, black=black.id)


async def prepared(session: AsyncSession, shop: Shop) -> CheckoutSession:
    checkout = await CheckoutRepository(session).create(
        merchant_id=shop.merchant_id,
        mandate_id=shop.mandate.id,
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
    readiness = await CheckoutExecutionService(session).prepare_execution(
        checkout.id, merchant_id=checkout.merchant_id, at=NOW
    )
    assert readiness.ready
    return checkout


async def admitted(session: AsyncSession, shop: Shop, *, key: str = KEY) -> PaymentAttempt:
    checkout = await prepared(session, shop)
    admission = await PaymentAdmissionService(session).admit_payment(
        checkout.id, merchant_id=checkout.merchant_id, idempotency_key=key, at=NOW
    )
    assert admission.attempt is not None
    return admission.attempt


@pytest.fixture
async def shop(session: AsyncSession) -> Shop:
    return await build_shop(session)


@pytest.fixture
def provider() -> FakePaymentProvider:
    """A provider that does guarantee visibility, with its clock at the dispatch instant.

    The window is the fake's own property. A test moves the clock to make it elapse, which is
    the only way time passes anywhere in this file.
    """
    return FakePaymentProvider(
        default=FakeOutcome.AMBIGUOUS, visibility_window=VISIBILITY_WINDOW, clock=NOW
    )


async def stock(session: AsyncSession, variant_id: uuid.UUID) -> int:
    return int(
        await session.scalar(select(Variant.inventory_quantity).where(Variant.id == variant_id))
        or 0
    )


async def events(session: AsyncSession, resource_type: str, resource_id: uuid.UUID) -> list[str]:
    return [
        event.event_type
        for event in await AuditRepository(session).list_for_resource(
            resource_type=resource_type, resource_id=resource_id
        )
    ]


async def test_a_payment_that_never_reached_the_provider_terminates_without_a_second_charge(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider
) -> None:
    """The whole blocker, reproduced and then closed, in one test.

    The timeline is the one that used to have no exit. Admission commits, the dispatch mark
    commits, the process dies before the network call, and the provider has never heard of the
    identity. Reconciliation finds nothing and keeps finding nothing, and while that is the
    answer nothing moves: the attempt stays unresolved, the hold stays committed, the checkout
    stays open and the mandate stays occupied.

    Then the provider's visibility guarantee matures. It stops saying "I have no record" and
    starts saying "this was never executed", which is a statement about what happened rather
    than about what it can see, and that is what lets the attempt fail, the stock go back and
    the mandate be free for another payment.

    The execute count is zero at every single step, including after the recovery. That is the
    assertion this file exists for: nothing in the way out of an unresolved payment is allowed
    to become a second attempt at charging somebody.
    """
    checkout = await prepared(session, shop)
    admission = await PaymentAdmissionService(session).admit_payment(
        checkout.id, merchant_id=checkout.merchant_id, idempotency_key=KEY, at=NOW
    )
    assert admission.attempt is not None
    attempt_id = admission.attempt.id
    reservation_id = admission.attempt.reservation_id
    before = await stock(session, shop.black)

    # The crash. The dispatch mark committed and the network call never happened, which is
    # exactly the state `mark_in_flight` committing before the wire is designed to produce.
    await _force_in_flight(session, attempt_id)
    assert provider.executions == []

    service = PaymentExecutionService(session, provider)
    first = await service.reconcile(attempt_id)

    assert first.attempt.status is PaymentAttemptStatus.UNKNOWN
    assert first.attempt.resolved_at is None
    assert first.provider_called
    reservation = await InventoryReservationRepository(session).get(reservation_id)
    assert reservation is not None
    assert reservation.status is ReservationStatus.COMMITTED
    assert await stock(session, shop.black) == before

    # Asked again, and again, while the answer is still "no record right now". Nothing may
    # accumulate into a decision: a hundred absences are not one failure.
    second = await service.reconcile(attempt_id)
    third = await service.reconcile(attempt_id)

    assert second.attempt.status is PaymentAttemptStatus.UNKNOWN
    assert third.attempt.status is PaymentAttemptStatus.UNKNOWN
    assert second.changed is False
    assert third.changed is False
    assert provider.executions == []

    # The provider's guarantee matures. Nothing in this application knows what the duration
    # was; the fake's own clock is what moved.
    provider.clock = await _matured(session, attempt_id)

    resolved = await service.reconcile(attempt_id)

    assert resolved.changed
    assert resolved.attempt.status is PaymentAttemptStatus.FAILED
    assert resolved.attempt.failure_code == PROVIDER_NEVER_EXECUTED
    assert resolved.attempt.outcome_source is OutcomeSource.RECONCILIATION
    assert resolved.attempt.resolved_at is not None

    released = await InventoryReservationRepository(session).get(reservation_id)
    assert released is not None
    assert released.status is ReservationStatus.RELEASED
    # The stock came back rather than being sold. A payment that never happened sells nothing.
    assert await stock(session, shop.black) == before
    still_open = await CheckoutRepository(session).get(
        checkout.id, merchant_id=checkout.merchant_id
    )
    assert still_open is not None
    assert still_open.status is CheckoutStatus.OPEN

    # The mandate is no longer held by a non terminal attempt, which is what was stuck.
    attempts = PaymentAttemptRepository(session)
    assert await attempts.get_open_for_mandate(shop.mandate.id) is None

    # And the checkout can be paid for again, through the ordinary path: a fresh hold and a
    # new identity. Nothing about the recovery re-sent the old one.
    provider.set_outcome(OTHER_KEY, FakeOutcome.SUCCESS)
    readiness = await CheckoutExecutionService(session).prepare_execution(
        checkout.id, merchant_id=checkout.merchant_id, at=NOW
    )
    assert readiness.ready
    retry = await PaymentAdmissionService(session).admit_payment(
        checkout.id, merchant_id=checkout.merchant_id, idempotency_key=OTHER_KEY, at=NOW
    )
    assert retry.admitted
    assert retry.attempt is not None
    paid = await service.dispatch(retry.attempt.id)

    assert paid.attempt.status is PaymentAttemptStatus.SUCCEEDED
    # The identity that was abandoned by the crash was never sent to a provider, not once, and
    # the payment that eventually succeeded is a different one.
    assert provider.executions_for(KEY) == 0
    assert provider.executions_for(OTHER_KEY) == 1
    assert provider.charges == 1


async def test_temporary_absence_never_becomes_a_failure_on_its_own(
    session: AsyncSession, shop: Shop
) -> None:
    """A provider that can never prove absence leaves the payment unresolved, forever if need be.

    This is the case with no visibility guarantee at all. However long the clock runs and
    however many times it is asked, the answer stays "no record right now" and nothing in this
    application is allowed to promote that into a decline. The way out is
    `agentrank_api.payments.recovery` and a person, not a timer.
    """
    provider = FakePaymentProvider(default=FakeOutcome.AMBIGUOUS, clock=NOW)
    attempt = await admitted(session, shop)
    attempt_id, reservation_id = attempt.id, attempt.reservation_id
    before = await stock(session, shop.black)
    service = PaymentExecutionService(session, provider)
    await service.dispatch(attempt_id)

    provider.clock = NOW + 100 * HOUR
    for _ in range(3):
        outcome = await service.reconcile(attempt_id)
        assert outcome.attempt.status is PaymentAttemptStatus.UNKNOWN
        assert outcome.changed is False

    reservation = await InventoryReservationRepository(session).get(reservation_id)
    assert reservation is not None
    assert reservation.status is ReservationStatus.COMMITTED
    assert await stock(session, shop.black) == before
    assert provider.charges == 0


async def test_a_provider_with_a_record_is_never_treated_as_absent(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider
) -> None:
    """The window elapsing must not overwrite a charge the provider actually made.

    The lost response, with a visibility guarantee in play. The money moved, so no amount of
    time passing may turn this into a released reservation: the record exists and the query
    finds it.
    """
    provider.default = FakeOutcome.LOST_RESPONSE
    attempt = await admitted(session, shop)
    attempt_id = attempt.id
    before = await stock(session, shop.black)
    service = PaymentExecutionService(session, provider)
    await service.dispatch(attempt_id)
    provider.clock = NOW + 100 * HOUR

    resolved = await service.reconcile(attempt_id)

    assert resolved.attempt.status is PaymentAttemptStatus.SUCCEEDED
    assert resolved.attempt.failure_code is None
    assert await stock(session, shop.black) == before - 1
    assert provider.charges == 1


async def test_final_absence_is_recorded_as_something_other_than_a_decline(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider
) -> None:
    """The trail has to say which of the two definitive failures this was.

    A card that said no and an operation that never existed both release the stock and only
    one of them is a refusal. A payment trail that called them the same thing would be
    describing a decline that never happened.
    """
    attempt = await admitted(session, shop)
    attempt_id, reservation_id = attempt.id, attempt.reservation_id
    service = PaymentExecutionService(session, provider)
    await service.dispatch(attempt_id)
    provider.clock = await _matured(session, attempt_id)

    await service.reconcile(attempt_id)

    assert await events(session, PAYMENT_RESOURCE, attempt_id) == [
        "payment.admitted",
        "payment.unknown",
        "payment.failed",
        "payment.reconciled",
    ]
    recorded = await AuditRepository(session).list_for_resource(
        resource_type=PAYMENT_RESOURCE, resource_id=attempt_id
    )
    failed = next(event for event in recorded if event.event_type == "payment.failed")
    assert failed.payload["failure_code"] == PROVIDER_NEVER_EXECUTED
    reconciled = next(event for event in recorded if event.event_type == "payment.reconciled")
    assert reconciled.payload["provider_record"] == "NEVER_EXECUTED"

    assert await events(session, RESERVATION_RESOURCE, reservation_id) == [
        "inventory.reserved",
        "inventory.released",
    ]
    holds = await AuditRepository(session).list_for_resource(
        resource_type=RESERVATION_RESOURCE, resource_id=reservation_id
    )
    release = next(event for event in holds if event.event_type == "inventory.released")
    assert release.payload["reason"] == "payment_not_executed"


async def test_the_application_holds_no_visibility_duration_of_its_own(
    session: AsyncSession, shop: Shop
) -> None:
    """Two providers with different guarantees, one application, two different answers.

    If any duration lived above the provider interface, both of these would resolve at the
    same moment. The one with no guarantee never resolves at all.
    """
    patient = FakePaymentProvider(
        default=FakeOutcome.AMBIGUOUS, visibility_window=100 * HOUR, clock=NOW
    )
    prompt = FakePaymentProvider(default=FakeOutcome.AMBIGUOUS, visibility_window=MINUTE, clock=NOW)
    attempt = await admitted(session, shop)
    attempt_id = attempt.id
    await PaymentExecutionService(session, patient).dispatch(attempt_id)

    patient.clock = NOW + HOUR
    unresolved = await PaymentExecutionService(session, patient).reconcile(attempt_id)
    assert unresolved.attempt.status is PaymentAttemptStatus.UNKNOWN

    prompt.clock = NOW + HOUR
    resolved = await PaymentExecutionService(session, prompt).reconcile(attempt_id)
    assert resolved.attempt.status is PaymentAttemptStatus.FAILED
    assert resolved.attempt.failure_code == PROVIDER_NEVER_EXECUTED


async def test_the_provider_is_told_when_the_dispatch_began(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider
) -> None:
    """The one fact a provider needs from us to evaluate a window, and no more than that.

    `dispatched_at` is committed with the IN_FLIGHT transition, so it is the instant this
    attempt stopped being certainly unsent, which is exactly the instant a visibility guarantee
    is measured from.
    """
    attempt = await admitted(session, shop)
    attempt_id = attempt.id
    service = PaymentExecutionService(session, provider)
    await service.dispatch(attempt_id)

    dispatched = await PaymentAttemptRepository(session).get(attempt_id)
    assert dispatched is not None
    assert dispatched.dispatched_at is not None
    asked = _WatchfulProvider(provider)

    await PaymentExecutionService(session, asked).reconcile(attempt_id)

    assert asked.asked is not None
    assert asked.asked.idempotency_key == KEY
    assert asked.asked.dispatched_at == dispatched.dispatched_at


class _WatchfulProvider:
    """A provider that records the question it was asked, and answers it normally."""

    def __init__(self, provider: FakePaymentProvider) -> None:
        self._provider = provider
        self.asked: PaymentQuery | None = None

    async def execute(self, instruction: PaymentInstruction) -> ProviderResult:
        raise AssertionError("reconciliation must never execute")

    async def query(self, query: PaymentQuery) -> ProviderQueryResult:
        self.asked = query
        return await self._provider.query(query)


async def _force_in_flight(session: AsyncSession, attempt_id: uuid.UUID) -> None:
    """Leave an attempt where a crash between the dispatch commit and the wire would leave it.

    Written as SQL rather than through the service, because the service would call the
    provider. The point of the scenario is that nothing ever did.
    """
    await session.execute(
        text(
            "UPDATE payment_attempt SET status = 'IN_FLIGHT', dispatched_at = now() WHERE id = :id"
        ),
        {"id": attempt_id},
    )
    await session.commit()
    # The session keeps objects readable after a commit, so a row changed behind its back stays
    # stale in the identity map until something expires it. A restarted process would have a
    # new session; this is the equivalent.
    session.expire_all()


async def _matured(session: AsyncSession, attempt_id: uuid.UUID) -> datetime:
    """The instant a provider's visibility window has certainly passed for this attempt.

    Read from `dispatched_at` rather than from the test's own constant, because the dispatch
    instant is the database clock and the constant is a Python one, and a window measured
    between the two would be off by however long the setup took.
    """
    attempt = await PaymentAttemptRepository(session).get(attempt_id)
    assert attempt is not None
    assert attempt.dispatched_at is not None
    return attempt.dispatched_at + VISIBILITY_WINDOW
