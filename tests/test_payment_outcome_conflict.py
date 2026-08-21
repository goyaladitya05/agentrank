"""Two writers resolving one payment, and being told two different things.

An execution whose response finally arrives and a reconciliation that queried while it was
still on its way can both be holding a definitive answer about the same attempt, and the two
answers can disagree. One of them wins the lock and commits. The other arrives holding a
contradiction of a settled payment.

The committed state was never at risk: a terminal attempt cannot be transitioned, by the
repository or by the database trigger. What was at risk was the response. The losing writer
used to raise a `ValueError` out of a repository, which reaches a caller as a 500 for an
operation that in fact did exactly the right thing by changing nothing.

Every test here forces the interleaving rather than hoping for it, using the gate the other
concurrency tests use: a third transaction holds the mandate lock while both writers are
started, so both are provably past their provider call and queued on the same lock before
either can decide anything.
"""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentrank_api.audit.repository import AuditRepository
from agentrank_api.checkout.execution import CheckoutExecutionService
from agentrank_api.checkout.models import CheckoutStatus
from agentrank_api.checkout.quote import QuotedLine
from agentrank_api.checkout.repository import CheckoutRepository
from agentrank_api.commerce.models import Variant
from agentrank_api.commerce.repository import CatalogRepository, MerchantRepository
from agentrank_api.constraints.repository import IntentConstraintRepository
from agentrank_api.constraints.rules import ConstraintOperator, IntentConstraintSpec
from agentrank_api.inventory.models import ReservationStatus
from agentrank_api.inventory.repository import InventoryReservationRepository
from agentrank_api.mandates.models import SpendingMandate
from agentrank_api.mandates.repository import MandateRepository
from agentrank_api.payments.admission import PAYMENT_RESOURCE, PaymentAdmissionService
from agentrank_api.payments.execution import PaymentExecutionService, PaymentOutcome
from agentrank_api.payments.fake import FakeOutcome, FakePaymentProvider
from agentrank_api.payments.models import OutcomeSource, PaymentAttempt, PaymentAttemptStatus
from agentrank_api.payments.provider import (
    PaymentInstruction,
    PaymentQuery,
    ProviderOutcome,
    ProviderQueryResult,
    ProviderRecord,
    ProviderResult,
)
from agentrank_api.payments.repository import PaymentAttemptRepository

pytestmark = pytest.mark.anyio

NOW = datetime.now(UTC)
HOUR = timedelta(hours=1)
PRICE = 499900
STOCK = 5
KEY = "pay-ampere-0001"
BLACK = IntentConstraintSpec.required_attribute("color", ConstraintOperator.EQ, "black")

# A concurrent test that goes wrong blocks on a row lock rather than failing, so every gather
# is bounded. Generous enough never to fire on a healthy database.
CONCURRENCY_TIMEOUT = 30

# How long both writers are watched before concluding they are genuinely queued on the gate.
LOCK_WAIT = 0.5


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


async def unresolved(session: AsyncSession, shop: Shop) -> PaymentAttempt:
    """A committed UNKNOWN attempt, reached the way one really is reached."""
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
    readiness = await CheckoutExecutionService(session).prepare_execution(checkout.id, at=NOW)
    assert readiness.ready
    admission = await PaymentAdmissionService(session).admit_payment(
        checkout.id, idempotency_key=KEY, at=NOW
    )
    assert admission.attempt is not None
    ambiguous = FakePaymentProvider(default=FakeOutcome.AMBIGUOUS)
    await PaymentExecutionService(session, ambiguous).dispatch(admission.attempt.id)
    attempt = await PaymentAttemptRepository(session).get(admission.attempt.id)
    assert attempt is not None
    assert attempt.status is PaymentAttemptStatus.UNKNOWN
    return attempt


@pytest.fixture
async def shop(session: AsyncSession) -> Shop:
    return await build_shop(session)


class _FixedProvider:
    """A provider that always answers the same definitive thing and refuses to execute.

    Two of these with different answers are what a disagreement is made of. Refusing `execute`
    outright is stronger than counting calls: a reconciliation that charged anybody would fail
    here rather than be noticed afterwards.
    """

    def __init__(self, outcome: ProviderOutcome) -> None:
        self.outcome = outcome
        self.queries: list[str] = []

    async def execute(self, instruction: PaymentInstruction) -> ProviderResult:
        raise AssertionError("reconciliation must never execute")

    async def query(self, query: PaymentQuery) -> ProviderQueryResult:
        self.queries.append(query.idempotency_key)
        if self.outcome is ProviderOutcome.SUCCEEDED:
            return ProviderQueryResult(
                outcome=ProviderOutcome.SUCCEEDED,
                record=ProviderRecord.PRESENT,
                reference="provider-said-yes",
            )
        return ProviderQueryResult(
            outcome=ProviderOutcome.FAILED,
            record=ProviderRecord.PRESENT,
            failure_code="PROVIDER_SAID_NO",
        )


async def reconcile_in_new_session(
    factory: async_sessionmaker[AsyncSession], attempt_id: uuid.UUID, provider: _FixedProvider
) -> PaymentOutcome:
    async with factory() as session:
        return await PaymentExecutionService(session, provider).reconcile(attempt_id)


async def still_waiting(*attempts: asyncio.Task[PaymentOutcome]) -> bool:
    """Whether every writer is still blocked, watched for a bounded window."""
    done, _ = await asyncio.wait(set(attempts), timeout=LOCK_WAIT)
    return not done


async def stock(session: AsyncSession, variant_id: uuid.UUID) -> int:
    return int(
        await session.scalar(select(Variant.inventory_quantity).where(Variant.id == variant_id))
        or 0
    )


async def test_two_writers_disagreeing_settle_one_outcome_and_neither_raises(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession], shop: Shop
) -> None:
    """The contradiction, forced. One answer wins and the other is told so in a typed result.

    Both writers query before either can take a lock, so both are holding a definitive answer
    and the answers differ. Which one wins is not determined and does not matter. What matters
    is that exactly one changed the payment, the other returns rather than raising, the
    inventory and the checkout move at most once, and the terminal state that stands is the
    winner's rather than the last writer's.
    """
    attempt = await unresolved(session, shop)
    attempt_id = attempt.id
    before = await stock(session, shop.black)
    says_yes = _FixedProvider(ProviderOutcome.SUCCEEDED)
    says_no = _FixedProvider(ProviderOutcome.FAILED)

    async with asyncio.timeout(CONCURRENCY_TIMEOUT):
        async with factory() as gate:
            # The first lock every outcome transaction takes, so holding it queues both
            # writers after their provider call and before any decision.
            await MandateRepository(gate).get_for_update(shop.mandate.id)
            writers: list[asyncio.Task[PaymentOutcome]] = [
                asyncio.create_task(reconcile_in_new_session(factory, attempt_id, says_yes)),
                asyncio.create_task(reconcile_in_new_session(factory, attempt_id, says_no)),
            ]
            assert await still_waiting(*writers)
            await gate.rollback()

        outcomes = await asyncio.gather(*writers)

    # Both were asked. Neither raised.
    assert says_yes.queries == [KEY]
    assert says_no.queries == [KEY]
    changed = [outcome for outcome in outcomes if outcome.changed]
    conflicted = [outcome for outcome in outcomes if outcome.conflict is not None]
    assert len(changed) == 1
    assert len(conflicted) == 1
    assert changed[0].conflict is None
    assert conflicted[0].changed is False

    loser = conflicted[0]
    assert loser.conflict is not None
    winner_status = changed[0].attempt.status
    # The loser reports what stands, not what it saw. The two are different fields.
    assert loser.conflict.authoritative is winner_status
    assert loser.conflict.observed is not None
    assert loser.attempt.status is winner_status

    async with factory() as reader:
        settled = await PaymentAttemptRepository(reader).get(attempt_id)
        assert settled is not None
        assert settled.status is winner_status
        reservation = await InventoryReservationRepository(reader).get(attempt.reservation_id)
        assert reservation is not None
        checkout = await CheckoutRepository(reader).get(attempt.checkout_id)
        assert checkout is not None
        after = await stock(reader, shop.black)

        if winner_status is PaymentAttemptStatus.SUCCEEDED:
            assert reservation.status is ReservationStatus.CONSUMED
            assert checkout.status is CheckoutStatus.PAID
            # Once, by the winner. The loser observed a failure and released nothing.
            assert after == before - 1
        else:
            assert reservation.status is ReservationStatus.RELEASED
            assert checkout.status is CheckoutStatus.OPEN
            # The expensive direction: a success observation arriving after a recorded failure
            # must not consume stock for money nobody has confirmed moved.
            assert after == before

        recorded = [
            event.event_type
            for event in await AuditRepository(reader).list_for_resource(
                resource_type=PAYMENT_RESOURCE, resource_id=attempt_id
            )
        ]
        # One outcome, one conflict. Not two outcomes, and not a silent second one.
        assert recorded.count("payment.outcome_conflict") == 1
        assert recorded.count("payment.succeeded") + recorded.count("payment.failed") == 1


async def test_the_conflict_event_records_what_stands_and_what_was_observed(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession], shop: Shop
) -> None:
    """Forensics, not a decision. The trail has to answer which two answers were in play."""
    attempt = await unresolved(session, shop)
    attempt_id = attempt.id
    says_yes = _FixedProvider(ProviderOutcome.SUCCEEDED)
    says_no = _FixedProvider(ProviderOutcome.FAILED)

    async with asyncio.timeout(CONCURRENCY_TIMEOUT):
        async with factory() as gate:
            await MandateRepository(gate).get_for_update(shop.mandate.id)
            writers: list[asyncio.Task[PaymentOutcome]] = [
                asyncio.create_task(reconcile_in_new_session(factory, attempt_id, says_yes)),
                asyncio.create_task(reconcile_in_new_session(factory, attempt_id, says_no)),
            ]
            assert await still_waiting(*writers)
            await gate.rollback()

        outcomes = await asyncio.gather(*writers)

    loser = next(outcome for outcome in outcomes if outcome.conflict is not None)
    assert loser.conflict is not None

    async with factory() as reader:
        events = await AuditRepository(reader).list_for_resource(
            resource_type=PAYMENT_RESOURCE, resource_id=attempt_id
        )
        conflict = next(event for event in events if event.event_type == "payment.outcome_conflict")

    assert conflict.payload["authoritative_status"] == loser.conflict.authoritative.value
    assert conflict.payload["observed_outcome"] == loser.conflict.observed.value
    assert conflict.payload["authoritative_status"] != conflict.payload["observed_outcome"]
    assert conflict.payload["checkout_id"] == str(attempt.checkout_id)


async def test_a_late_failure_never_rewrites_a_recorded_success(
    session: AsyncSession, shop: Shop
) -> None:
    """The disagreement without the race, so the assertion is about the rule and not the timing.

    A success is recorded. A second writer then arrives with a definitive failure for the same
    attempt, which is what a stale query looks like. The payment stays paid, the stock stays
    sold, and the caller gets a result rather than an error.
    """
    attempt = await unresolved(session, shop)
    attempt_id = attempt.id
    before = await stock(session, shop.black)
    resolved = await PaymentExecutionService(
        session, _FixedProvider(ProviderOutcome.SUCCEEDED)
    ).reconcile(attempt_id)
    assert resolved.attempt.status is PaymentAttemptStatus.SUCCEEDED

    # The terminal short circuit would normally stop this, so the outcome writer is exercised
    # directly. It is the layer that has to be safe; the short circuit is a convenience above
    # it.
    service = PaymentExecutionService(session, _FixedProvider(ProviderOutcome.FAILED))
    late = await service._record_failure(
        attempt_id,
        ProviderResult(outcome=ProviderOutcome.FAILED, failure_code="PROVIDER_SAID_NO"),
        source=OutcomeSource.RECONCILIATION,
    )

    assert late.changed is False
    assert late.conflict is not None
    assert late.conflict.authoritative is PaymentAttemptStatus.SUCCEEDED
    assert late.conflict.observed is ProviderOutcome.FAILED
    assert late.attempt.status is PaymentAttemptStatus.SUCCEEDED
    assert late.attempt.failure_code is None
    reservation = await InventoryReservationRepository(session).get(attempt.reservation_id)
    assert reservation is not None
    assert reservation.status is ReservationStatus.CONSUMED
    assert await stock(session, shop.black) == before - 1


async def test_a_late_success_never_rewrites_a_recorded_failure(
    session: AsyncSession, shop: Shop
) -> None:
    """The other direction, and the one that would sell stock twice if it were allowed."""
    attempt = await unresolved(session, shop)
    attempt_id = attempt.id
    before = await stock(session, shop.black)
    resolved = await PaymentExecutionService(
        session, _FixedProvider(ProviderOutcome.FAILED)
    ).reconcile(attempt_id)
    assert resolved.attempt.status is PaymentAttemptStatus.FAILED

    service = PaymentExecutionService(session, _FixedProvider(ProviderOutcome.SUCCEEDED))
    late = await service._record_success(
        attempt_id,
        ProviderResult(outcome=ProviderOutcome.SUCCEEDED, reference="provider-said-yes"),
        source=OutcomeSource.RECONCILIATION,
    )

    assert late.changed is False
    assert late.conflict is not None
    assert late.conflict.authoritative is PaymentAttemptStatus.FAILED
    assert late.conflict.observed is ProviderOutcome.SUCCEEDED
    assert late.attempt.status is PaymentAttemptStatus.FAILED
    assert late.attempt.provider_reference is None
    reservation = await InventoryReservationRepository(session).get(attempt.reservation_id)
    assert reservation is not None
    assert reservation.status is ReservationStatus.RELEASED
    # Nothing was sold and nothing was decremented, because nothing here is allowed to decide
    # that the failure was wrong.
    assert await stock(session, shop.black) == before
