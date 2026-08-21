"""Resolving a payment nobody knew the answer to.

The lost response is the case this whole phase is shaped around, and this is where it is
tested end to end. The provider charges the card, the answer never comes back, the attempt is
UNKNOWN, the stock stays held, the client retries and reaches no provider at all, and then a
query finds the charge and the purchase completes. Exactly one charge, exactly one unit of
stock, exactly one paid checkout.

Everything else here is about not doing damage while not knowing. Reconciliation never
charges, never releases stock on an indefinite answer, and can be run as many times as anybody
likes.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentrank_api.audit.repository import AuditRepository
from agentrank_api.checkout.execution import CheckoutExecutionService
from agentrank_api.checkout.models import CheckoutSession, CheckoutStatus
from agentrank_api.checkout.quote import QuotedLine
from agentrank_api.checkout.repository import CheckoutRepository
from agentrank_api.commerce.models import Variant
from agentrank_api.commerce.repository import CatalogRepository, MerchantRepository
from agentrank_api.constraints.repository import IntentConstraintRepository
from agentrank_api.constraints.rules import ConstraintOperator, IntentConstraintSpec
from agentrank_api.errors import ConflictError
from agentrank_api.inventory.models import ReservationStatus
from agentrank_api.inventory.repository import InventoryReservationRepository
from agentrank_api.mandates.models import SpendingMandate
from agentrank_api.mandates.repository import MandateRepository
from agentrank_api.payments.admission import PAYMENT_RESOURCE, PaymentAdmissionService
from agentrank_api.payments.execution import PaymentExecutionService
from agentrank_api.payments.fake import FakeOutcome, FakePaymentProvider
from agentrank_api.payments.models import OutcomeSource, PaymentAttempt, PaymentAttemptStatus
from agentrank_api.payments.provider import PaymentInstruction

pytestmark = pytest.mark.anyio

NOW = datetime.now(UTC)
HOUR = timedelta(hours=1)
PRICE = 499900
STOCK = 5
KEY = "pay-ampere-0001"
OTHER_KEY = "pay-ampere-0002"
BLACK = IntentConstraintSpec.required_attribute("color", ConstraintOperator.EQ, "black")


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
    readiness = await CheckoutExecutionService(session).prepare_execution(checkout.id, at=NOW)
    assert readiness.ready
    return checkout


async def admitted(session: AsyncSession, shop: Shop, *, key: str = KEY) -> PaymentAttempt:
    checkout = await prepared(session, shop)
    admission = await PaymentAdmissionService(session).admit_payment(
        checkout.id, idempotency_key=key, at=NOW
    )
    assert admission.attempt is not None
    return admission.attempt


@pytest.fixture
async def shop(session: AsyncSession) -> Shop:
    return await build_shop(session)


@pytest.fixture
def provider() -> FakePaymentProvider:
    return FakePaymentProvider()


async def stock(session: AsyncSession, variant_id: uuid.UUID) -> int:
    return int(
        await session.scalar(select(Variant.inventory_quantity).where(Variant.id == variant_id))
        or 0
    )


async def events(session: AsyncSession, attempt_id: uuid.UUID) -> list[str]:
    return [
        event.event_type
        for event in await AuditRepository(session).list_for_resource(
            resource_type=PAYMENT_RESOURCE, resource_id=attempt_id
        )
    ]


async def test_a_lost_response_is_resolved_by_reconciliation(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider
) -> None:
    """The whole timeline, in one test, because the point is that it composes.

    The provider charges the card and the answer is lost. The attempt goes UNKNOWN and the
    stock stays committed. The client retries the same payment request and reaches no provider
    at all. Reconciliation queries, finds the charge, and the purchase completes: one charge,
    one unit, one paid checkout.
    """
    provider.default = FakeOutcome.LOST_RESPONSE
    checkout = await prepared(session, shop)
    admission_service = PaymentAdmissionService(session)
    admission = await admission_service.admit_payment(checkout.id, idempotency_key=KEY, at=NOW)
    assert admission.attempt is not None
    attempt_id = admission.attempt.id
    reservation_id = admission.attempt.reservation_id
    before = await stock(session, shop.black)

    service = PaymentExecutionService(session, provider)
    lost = await service.dispatch(attempt_id)

    assert lost.attempt.status is PaymentAttemptStatus.UNKNOWN
    assert lost.attempt.resolved_at is None
    reservation = await InventoryReservationRepository(session).get(reservation_id)
    assert reservation is not None
    assert reservation.status is ReservationStatus.COMMITTED
    assert await stock(session, shop.black) == before
    # The provider disagrees with us, which is exactly the state that needs resolving.
    assert provider.charges == 1

    # The client retries. It reaches the same attempt and no provider.
    retry = await admission_service.admit_payment(checkout.id, idempotency_key=KEY, at=NOW)
    assert retry.attempt is not None
    assert retry.attempt.id == attempt_id
    assert retry.created is False
    assert provider.executions_for(KEY) == 1

    resolved = await service.reconcile(attempt_id)

    assert resolved.changed
    assert resolved.provider_called
    assert resolved.attempt.status is PaymentAttemptStatus.SUCCEEDED
    assert resolved.attempt.outcome_source is OutcomeSource.RECONCILIATION
    assert resolved.attempt.provider_reference is not None
    assert resolved.attempt.resolved_at is not None

    paid = await CheckoutRepository(session).get(checkout.id)
    assert paid is not None
    assert paid.status is CheckoutStatus.PAID
    consumed = await InventoryReservationRepository(session).get(reservation_id)
    assert consumed is not None
    assert consumed.status is ReservationStatus.CONSUMED
    # Decremented once, by the reconciliation rather than by the dispatch, and once in total.
    assert await stock(session, shop.black) == before - 1
    # One execute, one query, one charge. The numbers that matter.
    assert provider.executions_for(KEY) == 1
    assert provider.queries == [KEY]
    assert provider.charges == 1


async def test_reconciliation_resolves_a_definitive_decline(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider
) -> None:
    """An ambiguous answer that turns out to have been a decline releases the stock.

    The same outcome logic a dispatch would have used, an hour later.
    """
    provider.default = FakeOutcome.AMBIGUOUS
    attempt = await admitted(session, shop)
    attempt_id, reservation_id = attempt.id, attempt.reservation_id
    before = await stock(session, shop.black)
    service = PaymentExecutionService(session, provider)
    await service.dispatch(attempt_id)

    # The provider now has an answer it did not have before.
    provider.set_outcome(KEY, FakeOutcome.DECLINE)
    await provider.execute(_replayed(attempt))
    provider.executions.pop()

    resolved = await service.reconcile(attempt_id)

    assert resolved.attempt.status is PaymentAttemptStatus.FAILED
    assert resolved.attempt.failure_code == "CARD_DECLINED"
    assert resolved.attempt.outcome_source is OutcomeSource.RECONCILIATION
    reservation = await InventoryReservationRepository(session).get(reservation_id)
    assert reservation is not None
    assert reservation.status is ReservationStatus.RELEASED
    assert await stock(session, shop.black) == before
    checkout = await CheckoutRepository(session).get(attempt.checkout_id)
    assert checkout is not None
    assert checkout.status is CheckoutStatus.OPEN


async def test_an_indefinite_answer_leaves_the_attempt_exactly_where_it_was(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider
) -> None:
    """The provider still does not know, so neither do we, and nothing moves.

    Specifically the stock does not move. A provider with no record of an identity has not said
    the payment failed, and releasing here would be releasing under a charge that may still be
    in flight.
    """
    provider.default = FakeOutcome.AMBIGUOUS
    attempt = await admitted(session, shop)
    attempt_id, reservation_id = attempt.id, attempt.reservation_id
    before = await stock(session, shop.black)
    service = PaymentExecutionService(session, provider)
    await service.dispatch(attempt_id)

    unresolved = await service.reconcile(attempt_id)

    assert unresolved.attempt.status is PaymentAttemptStatus.UNKNOWN
    assert unresolved.changed is False
    assert unresolved.provider_called
    assert unresolved.attempt.resolved_at is None
    reservation = await InventoryReservationRepository(session).get(reservation_id)
    assert reservation is not None
    assert reservation.status is ReservationStatus.COMMITTED
    assert await stock(session, shop.black) == before
    assert provider.queries == [KEY]
    # The query is not a charge, and the fake would have recorded one.
    assert provider.charges == 0


async def test_reconciliation_never_calls_execute(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider
) -> None:
    """A provider that created a payment because somebody asked about one would be a disaster."""
    provider.default = FakeOutcome.AMBIGUOUS
    attempt = await admitted(session, shop)
    service = PaymentExecutionService(session, provider)
    await service.dispatch(attempt.id)
    executions_after_dispatch = len(provider.executions)

    await service.reconcile(attempt.id)
    await service.reconcile(attempt.id)

    assert len(provider.executions) == executions_after_dispatch
    assert provider.queries == [KEY, KEY]


async def test_reconciling_twice_records_one_outcome(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider
) -> None:
    """Idempotent, and observably so. A sweep that runs twice must not sell twice.

    The second call finds a settled attempt and stops without asking the provider anything,
    which is the cheap half of idempotency. The expensive half, that the outcome writers
    themselves record one outcome, is what makes the cheap half safe rather than load bearing.
    """
    provider.default = FakeOutcome.LOST_RESPONSE
    attempt = await admitted(session, shop)
    attempt_id = attempt.id
    before = await stock(session, shop.black)
    service = PaymentExecutionService(session, provider)
    await service.dispatch(attempt_id)

    first = await service.reconcile(attempt_id)
    second = await service.reconcile(attempt_id)

    assert first.changed is True
    assert second.changed is False
    assert second.provider_called is False
    assert first.attempt.status is PaymentAttemptStatus.SUCCEEDED
    assert second.attempt.status is PaymentAttemptStatus.SUCCEEDED
    assert first.attempt.resolved_at == second.attempt.resolved_at
    # One unit, not two, which is the property a repeated sweep could otherwise break.
    assert await stock(session, shop.black) == before - 1
    assert provider.charges == 1
    assert provider.queries == [KEY]
    assert await events(session, attempt_id) == [
        "payment.admitted",
        "payment.unknown",
        "payment.succeeded",
        "payment.reconciled",
    ]


async def test_reconciling_a_settled_attempt_asks_the_provider_nothing(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider
) -> None:
    """Nothing left to learn, and deliberately not an error.

    A sweep that meets an attempt somebody else already resolved has done its job.
    """
    provider.default = FakeOutcome.SUCCESS
    attempt = await admitted(session, shop)
    service = PaymentExecutionService(session, provider)
    await service.dispatch(attempt.id)

    outcome = await service.reconcile(attempt.id)

    assert outcome.attempt.status is PaymentAttemptStatus.SUCCEEDED
    assert outcome.changed is False
    assert outcome.provider_called is False
    assert provider.queries == []


async def test_reconciling_an_admitted_attempt_is_refused(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider
) -> None:
    """ADMITTED means the provider has never heard of this identity.

    What it needs is a dispatch, and saying so is more useful than a query that reports no
    record.
    """
    attempt = await admitted(session, shop)

    with pytest.raises(ConflictError) as refused:
        await PaymentExecutionService(session, provider).reconcile(attempt.id)

    assert refused.value.reason == "payment_not_dispatched"
    assert provider.queries == []
    assert provider.executions == []


async def test_an_in_flight_attempt_after_a_restart_is_reconciled_not_re_sent(
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    shop: Shop,
    provider: FakePaymentProvider,
) -> None:
    """The crash between the dispatch commit and the outcome commit.

    The attempt is IN_FLIGHT, the provider has already charged, and nothing local knows it. A
    new session and a new service must not re-send: they must ask. The provider is kept across
    the restart, because a payment processor does not forget a charge when our process dies.
    """
    provider.default = FakeOutcome.SUCCESS
    attempt = await admitted(session, shop)
    attempt_id = attempt.id
    before = await stock(session, shop.black)
    # The world as a crash between the two commits would leave it: the provider has charged and
    # the attempt is still IN_FLIGHT.
    await _force_in_flight(session, attempt_id)
    await provider.execute(_replayed(attempt))
    provider.executions.clear()

    async with factory() as restarted:
        service = PaymentExecutionService(restarted, provider)
        with pytest.raises(ConflictError) as refused:
            await service.dispatch(attempt_id)
        assert refused.value.reason == "payment_in_progress"

        resolved = await service.reconcile(attempt_id)

    assert resolved.attempt.status is PaymentAttemptStatus.SUCCEEDED
    # Never re-sent. The one thing an IN_FLIGHT attempt must never be.
    assert provider.executions == []
    assert provider.charges == 1
    assert await stock(session, shop.black) == before - 1


async def test_a_reconciled_success_after_expiry_is_honoured(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider
) -> None:
    """Resolution reads no clock either.

    A payment admitted while everything was valid and resolved long afterwards is still the
    payment that was authorized.
    """
    provider.default = FakeOutcome.LOST_RESPONSE
    attempt = await admitted(session, shop)
    attempt_id, checkout_id = attempt.id, attempt.checkout_id
    service = PaymentExecutionService(session, provider)
    await service.dispatch(attempt_id)

    # An accounting instant long past both windows would deny every gate. Reconciliation does
    # not consult one.
    resolved = await service.reconcile(attempt_id)

    assert resolved.attempt.status is PaymentAttemptStatus.SUCCEEDED
    checkout = await CheckoutRepository(session).get(checkout_id)
    assert checkout is not None
    assert checkout.status is CheckoutStatus.PAID
    lapsed = await CheckoutExecutionService(session).execution_authorization(
        checkout_id, at=NOW + HOUR + HOUR
    )
    assert not lapsed.authorized


async def test_an_unknown_payment_blocks_a_new_one_until_it_is_resolved(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider
) -> None:
    """An unresolved payment holds the mandate, which is what stops a second charge.

    Once reconciliation finds the decline, the mandate is free again and a fresh hold plus a
    fresh key is an ordinary payment.
    """
    provider.default = FakeOutcome.AMBIGUOUS
    checkout = await prepared(session, shop)
    admission_service = PaymentAdmissionService(session)
    first = await admission_service.admit_payment(checkout.id, idempotency_key=KEY, at=NOW)
    assert first.attempt is not None
    # Read before the refusal below rolls back and expires the attempt it names.
    first_id = first.attempt.id
    replay = _replayed(first.attempt)
    service = PaymentExecutionService(session, provider)
    await service.dispatch(first_id)

    blocked = await admission_service.admit_payment(checkout.id, idempotency_key=OTHER_KEY, at=NOW)
    assert not blocked.admitted
    assert provider.executions_for(OTHER_KEY) == 0

    # The provider now has an answer it did not have before. Recorded straight into its ledger
    # rather than through this application, which is the point: the two disagree until a query
    # closes the gap.
    provider.set_outcome(KEY, FakeOutcome.DECLINE)
    await provider.execute(replay)
    provider.executions.pop()
    await service.reconcile(first_id)

    readiness = await CheckoutExecutionService(session).prepare_execution(checkout.id, at=NOW)
    assert readiness.ready
    provider.set_outcome(OTHER_KEY, FakeOutcome.SUCCESS)
    retry = await admission_service.admit_payment(checkout.id, idempotency_key=OTHER_KEY, at=NOW)
    assert retry.admitted
    assert retry.attempt is not None
    outcome = await service.dispatch(retry.attempt.id)

    assert outcome.attempt.status is PaymentAttemptStatus.SUCCEEDED
    assert provider.charges == 1


async def test_the_reconciled_event_records_whether_the_provider_knew_anything(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider
) -> None:
    """ "No record" is a different fact from "it failed", and the trail says which."""
    provider.default = FakeOutcome.AMBIGUOUS
    attempt = await admitted(session, shop)
    service = PaymentExecutionService(session, provider)
    await service.dispatch(attempt.id)

    await service.reconcile(attempt.id)

    recorded = await AuditRepository(session).list_for_resource(
        resource_type=PAYMENT_RESOURCE, resource_id=attempt.id
    )
    reconciled = [event for event in recorded if event.event_type == "payment.reconciled"]
    assert len(reconciled) == 1
    assert reconciled[0].payload["provider_known"] is False
    assert reconciled[0].payload["provider_outcome"] == "UNKNOWN"
    assert reconciled[0].payload["status"] == "UNKNOWN"


def _replayed(attempt: PaymentAttempt) -> PaymentInstruction:
    """The instruction a provider would have received for this attempt.

    Used to put the fake into a state a crash would have left it in: charged, with this
    application knowing nothing about it. Built here rather than imported from the service,
    because a test that reused the service's own builder would not notice if it changed.
    """
    return PaymentInstruction(
        attempt_id=attempt.id,
        idempotency_key=attempt.idempotency_key,
        amount_minor=attempt.amount_minor,
        currency=attempt.currency,
        merchant_reference=str(attempt.merchant_id),
        checkout_reference=str(attempt.checkout_id),
    )


async def _force_in_flight(session: AsyncSession, attempt_id: uuid.UUID) -> None:
    """Leave an attempt where a crash between the two commits would leave it."""
    await session.execute(
        text(
            "UPDATE payment_attempt SET status = 'IN_FLIGHT', dispatched_at = now() WHERE id = :id"
        ),
        {"id": attempt_id},
    )
    await session.commit()
