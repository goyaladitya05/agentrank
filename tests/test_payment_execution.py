"""Dispatching a payment, and the three answers a provider can give.

Three outcomes, three transactions, and one rule that shapes all of them: the provider is
called with no database transaction open, and everything that follows from what it said is
recorded in one atomic write afterwards.

The tests that matter most are not the happy path. They are the ones where the answer is
ambiguous, where the response was lost, where the quote expired while the provider was
thinking, and where a caller retries something that must not be sent twice. Every one of them
asserts a provider call count, because "the database ended up in the right state" is not the
same property as "the provider was asked exactly once" and only the second one is about money.
"""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentrank_api.audit.repository import AuditRepository
from agentrank_api.checkout.authorization import CheckoutAuthorizationViolation
from agentrank_api.checkout.execution import CheckoutExecutionService
from agentrank_api.checkout.models import CheckoutSession, CheckoutStatus
from agentrank_api.checkout.quote import QuotedLine
from agentrank_api.checkout.repository import CheckoutRepository
from agentrank_api.checkout.service import CheckoutService
from agentrank_api.commerce.models import Variant
from agentrank_api.commerce.repository import CatalogRepository, MerchantRepository
from agentrank_api.constraints.repository import IntentConstraintRepository
from agentrank_api.constraints.rules import ConstraintOperator, IntentConstraintSpec
from agentrank_api.errors import ConflictError
from agentrank_api.inventory.models import InventoryReservation, ReservationStatus
from agentrank_api.inventory.repository import InventoryReservationRepository
from agentrank_api.inventory.service import RESERVATION_RESOURCE
from agentrank_api.locking import respects_lock_order
from agentrank_api.mandates.models import SpendingMandate
from agentrank_api.mandates.repository import MandateRepository
from agentrank_api.mandates.service import MandateService
from agentrank_api.payments.admission import PAYMENT_RESOURCE, PaymentAdmissionService
from agentrank_api.payments.execution import PaymentExecutionService
from agentrank_api.payments.fake import FakeOutcome, FakePaymentProvider
from agentrank_api.payments.models import OutcomeSource, PaymentAttempt, PaymentAttemptStatus
from agentrank_api.payments.provider import (
    PaymentInstruction,
    PaymentQuery,
    ProviderQueryResult,
    ProviderResult,
)

pytestmark = pytest.mark.anyio

NOW = datetime.now(UTC)
HOUR = timedelta(hours=1)
PRICE = 499900
STOCK = 5
KEY = "pay-ampere-0001"
OTHER_KEY = "pay-ampere-0002"
BLACK = IntentConstraintSpec.required_attribute("color", ConstraintOperator.EQ, "black")

# How long the deliberately short lived quote and mandate in the expiry test are good for, and
# how long that test waits before dispatching. The wait is longer than the window, so the lapse
# is certain rather than likely, and the window is long enough that admission has seconds of
# headroom on a slow machine. Nothing else in this file waits for anything.
EXPIRY_WINDOW = timedelta(seconds=2)
EXPIRY_WAIT = 2.3


@dataclass(frozen=True, slots=True)
class Shop:
    merchant_id: uuid.UUID
    mandate: SpendingMandate
    black: uuid.UUID


async def build_shop(session: AsyncSession, slug: str = "ampere-supply") -> Shop:
    merchant = await MerchantRepository(session).create(slug=slug, name=slug.title())
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
        merchant_id=merchant.id, external_id=f"{slug}-1", title="Charger", category="chargers"
    )
    black = await catalog.create_variant(
        product=product,
        sku=f"{slug}-black",
        price_amount_minor=PRICE,
        currency="INR",
        inventory_quantity=STOCK,
        attributes={"color": "black"},
    )
    await session.commit()
    return Shop(merchant_id=merchant.id, mandate=mandate, black=black.id)


async def prepared(
    session: AsyncSession, shop: Shop, *, expires_at: datetime | None = None
) -> CheckoutSession:
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
        expires_at=expires_at or NOW + HOUR,
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
    """Configured per test. The default is deliberately not set here."""
    return FakePaymentProvider()


async def stock(session: AsyncSession, variant_id: uuid.UUID) -> int:
    """The variant total, read as a column rather than through the identity map.

    A column only select does not consult the identity map, so this is always what the database
    holds rather than what an ORM object was loaded with.
    """
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


async def test_a_success_pays_the_checkout_and_consumes_the_stock(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider
) -> None:
    """Five things happen or none of them do."""
    provider.default = FakeOutcome.SUCCESS
    attempt = await admitted(session, shop)
    before = await stock(session, shop.black)

    outcome = await PaymentExecutionService(session, provider).dispatch(attempt.id)

    assert outcome.changed
    assert outcome.provider_called
    assert outcome.attempt.status is PaymentAttemptStatus.SUCCEEDED
    assert outcome.attempt.provider_reference is not None
    assert outcome.attempt.outcome_source is OutcomeSource.EXECUTION
    assert outcome.attempt.resolved_at is not None
    assert provider.executions_for(KEY) == 1
    assert provider.charges == 1

    checkout = await CheckoutRepository(session).get(attempt.checkout_id)
    assert checkout is not None
    assert checkout.status is CheckoutStatus.PAID
    assert checkout.paid_at is not None

    reservation = await InventoryReservationRepository(session).get(attempt.reservation_id)
    assert reservation is not None
    assert reservation.status is ReservationStatus.CONSUMED
    assert reservation.consumed_at is not None

    assert await stock(session, shop.black) == before - 1


async def test_a_success_consumes_inventory_exactly_once(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider
) -> None:
    """The whole of the double subtraction answer, asserted as arithmetic.

    Before the sale the units are counted as a hold and the variant total is untouched. After
    it the total is smaller and the hold is gone. Available quantity is the same number either
    side, and one purchase is subtracted exactly once.
    """
    provider.default = FakeOutcome.SUCCESS
    attempt = await admitted(session, shop)
    repository = InventoryReservationRepository(session)

    held_before = await repository.effective_reserved_quantities(variant_ids=[shop.black], at=NOW)
    total_before = await stock(session, shop.black)
    assert held_before == {shop.black: 1}
    available_before = total_before - held_before[shop.black]

    await PaymentExecutionService(session, provider).dispatch(attempt.id)

    held_after = await repository.effective_reserved_quantities(variant_ids=[shop.black], at=NOW)
    total_after = await stock(session, shop.black)
    assert held_after == {}
    assert total_after == total_before - 1
    assert total_after - 0 == available_before


async def test_a_success_records_the_payment_and_the_consumption(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider
) -> None:
    provider.default = FakeOutcome.SUCCESS
    attempt = await admitted(session, shop)

    await PaymentExecutionService(session, provider).dispatch(attempt.id)

    assert await events(session, attempt.id) == ["payment.admitted", "payment.succeeded"]
    reservation_events = [
        event.event_type
        for event in await AuditRepository(session).list_for_resource(
            resource_type=RESERVATION_RESOURCE, resource_id=attempt.reservation_id
        )
    ]
    assert reservation_events == ["inventory.reserved", "inventory.consumed"]


async def test_a_decline_releases_the_stock_and_leaves_the_quote_open(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider
) -> None:
    """A declined payment is a fact about an attempt, not about the quote."""
    provider.default = FakeOutcome.DECLINE
    attempt = await admitted(session, shop)
    before = await stock(session, shop.black)

    outcome = await PaymentExecutionService(session, provider).dispatch(attempt.id)

    assert outcome.attempt.status is PaymentAttemptStatus.FAILED
    assert outcome.attempt.failure_code == "CARD_DECLINED"
    assert outcome.attempt.resolved_at is not None
    assert provider.executions_for(KEY) == 1
    assert provider.charges == 0

    checkout = await CheckoutRepository(session).get(attempt.checkout_id)
    assert checkout is not None
    # Not FAILED, and there is no such status. The price is still good.
    assert checkout.status is CheckoutStatus.OPEN

    reservation = await InventoryReservationRepository(session).get(attempt.reservation_id)
    assert reservation is not None
    assert reservation.status is ReservationStatus.RELEASED
    # Released, not consumed. Nothing was bought, so the total does not move.
    assert await stock(session, shop.black) == before

    assert await events(session, attempt.id) == ["payment.admitted", "payment.failed"]
    reservation_events = [
        (event.event_type, event.payload.get("reason"))
        for event in await AuditRepository(session).list_for_resource(
            resource_type=RESERVATION_RESOURCE, resource_id=attempt.reservation_id
        )
    ]
    assert reservation_events == [
        ("inventory.reserved", None),
        ("inventory.released", "payment_declined"),
    ]


async def test_an_ambiguous_result_keeps_the_stock_committed(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider
) -> None:
    """Nobody knows, so as little as possible changes.

    Releasing here would be the expensive mistake: the charge may have gone through, and the
    unit would be sold again while the buyer had already paid for it.
    """
    provider.default = FakeOutcome.AMBIGUOUS
    attempt = await admitted(session, shop)
    before = await stock(session, shop.black)

    outcome = await PaymentExecutionService(session, provider).dispatch(attempt.id)

    assert outcome.attempt.status is PaymentAttemptStatus.UNKNOWN
    # Not resolved, and nothing pretends otherwise.
    assert outcome.attempt.resolved_at is None
    assert outcome.attempt.failure_code is None
    assert outcome.attempt.provider_reference is None
    assert provider.executions_for(KEY) == 1

    checkout = await CheckoutRepository(session).get(attempt.checkout_id)
    assert checkout is not None
    assert checkout.status is CheckoutStatus.OPEN

    reservation = await InventoryReservationRepository(session).get(attempt.reservation_id)
    assert reservation is not None
    assert reservation.status is ReservationStatus.COMMITTED
    assert await stock(session, shop.black) == before

    assert await events(session, attempt.id) == ["payment.admitted", "payment.unknown"]


async def test_an_unknown_attempt_is_never_automatically_retried(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider
) -> None:
    """Nothing re-sends an ambiguous payment. The only way out is a query."""
    provider.default = FakeOutcome.AMBIGUOUS
    attempt = await admitted(session, shop)
    service = PaymentExecutionService(session, provider)
    await service.dispatch(attempt.id)

    with pytest.raises(ConflictError) as refused:
        await service.dispatch(attempt.id)

    assert refused.value.reason == "payment_unresolved"
    # Zero automatic second calls, which is the number that matters.
    assert provider.executions_for(KEY) == 1


async def test_a_client_retry_of_the_same_payment_calls_the_provider_once(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider
) -> None:
    """The duplicate request case, end to end.

    The same key is admitted once, so the second request resolves to the same attempt, and the
    attempt is not ADMITTED any more so it is not dispatched again. Provider idempotency is
    not what saves this; nothing sends the second call at all.
    """
    provider.default = FakeOutcome.SUCCESS
    checkout = await prepared(session, shop)
    admission_service = PaymentAdmissionService(session)
    first = await admission_service.admit_payment(checkout.id, idempotency_key=KEY, at=NOW)
    assert first.attempt is not None
    await PaymentExecutionService(session, provider).dispatch(first.attempt.id)

    second = await admission_service.admit_payment(checkout.id, idempotency_key=KEY, at=NOW)

    assert second.attempt is not None
    assert second.attempt.id == first.attempt.id
    assert second.created is False
    assert second.attempt.status is PaymentAttemptStatus.SUCCEEDED
    assert provider.executions_for(KEY) == 1
    assert provider.charges == 1


async def test_a_settled_attempt_cannot_be_dispatched_again(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider
) -> None:
    provider.default = FakeOutcome.SUCCESS
    attempt = await admitted(session, shop)
    service = PaymentExecutionService(session, provider)
    await service.dispatch(attempt.id)

    with pytest.raises(ConflictError) as refused:
        await service.dispatch(attempt.id)

    assert refused.value.reason == "payment_already_succeeded"
    assert provider.executions_for(KEY) == 1


async def test_a_declined_attempt_cannot_be_dispatched_again(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider
) -> None:
    provider.default = FakeOutcome.DECLINE
    attempt = await admitted(session, shop)
    service = PaymentExecutionService(session, provider)
    await service.dispatch(attempt.id)

    with pytest.raises(ConflictError) as refused:
        await service.dispatch(attempt.id)

    assert refused.value.reason == "payment_already_failed"
    assert provider.executions_for(KEY) == 1


async def test_success_after_the_quote_and_the_mandate_have_expired_is_honoured(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider
) -> None:
    """Admission was the authorization instant, and nothing retracts it.

    The timeline: the hold is valid, the payment is admitted, the provider call begins, the
    checkout and the mandate both lapse while it is in flight, and the provider says yes. The
    success is honoured, because refusing it would mean taking money and denying the purchase.

    The expiry is real rather than simulated. `expires_at` and `valid_until` are immutable at
    the database, deliberately, so there is no way to push a window into the past and no
    interest in one: what is being tested is a world where time passed, not one where a value
    was edited. The window is short and the wait is longer than it, so the lapse is certain
    rather than likely, and admission has two full seconds of headroom before it.
    """
    provider.default = FakeOutcome.SUCCESS
    moment = datetime.now(UTC)
    short = await MandateRepository(session).create(
        merchant_id=shop.merchant_id,
        max_total_amount_minor=PRICE,
        currency="INR",
        valid_from=moment - HOUR,
        valid_until=moment + EXPIRY_WINDOW,
    )
    await IntentConstraintRepository(session).create(
        merchant_id=shop.merchant_id, mandate_id=short.id, specs=[BLACK]
    )
    checkout = await CheckoutRepository(session).create(
        merchant_id=shop.merchant_id,
        mandate_id=short.id,
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
        expires_at=moment + EXPIRY_WINDOW,
    )
    await session.commit()
    readiness = await CheckoutExecutionService(session).prepare_execution(checkout.id)
    assert readiness.ready
    admission = await PaymentAdmissionService(session).admit_payment(
        checkout.id, idempotency_key=KEY
    )
    assert admission.admitted
    assert admission.attempt is not None
    attempt_id = admission.attempt.id
    before = await stock(session, shop.black)

    # The clock crosses both windows while the provider is notionally thinking.
    await asyncio.sleep(EXPIRY_WAIT)
    lapsed = await CheckoutExecutionService(session).execution_authorization(checkout.id)
    assert not lapsed.authorized
    assert CheckoutAuthorizationViolation.CHECKOUT_EXPIRED in lapsed.financial.violations
    assert CheckoutAuthorizationViolation.MANDATE_EXPIRED in lapsed.financial.violations

    outcome = await PaymentExecutionService(session, provider).dispatch(attempt_id)

    assert outcome.attempt.status is PaymentAttemptStatus.SUCCEEDED
    checkout_now = await CheckoutRepository(session).get(checkout.id)
    assert checkout_now is not None
    assert checkout_now.status is CheckoutStatus.PAID
    # Consumed exactly once, despite everything having lapsed.
    assert await stock(session, shop.black) == before - 1


async def test_a_revocation_after_admission_does_not_stop_the_payment(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider
) -> None:
    """Revocation prevents the next admission. It does not retract one that committed."""
    provider.default = FakeOutcome.SUCCESS
    attempt = await admitted(session, shop)
    await MandateService(session).revoke_mandate(shop.mandate.id)

    outcome = await PaymentExecutionService(session, provider).dispatch(attempt.id)

    assert outcome.attempt.status is PaymentAttemptStatus.SUCCEEDED
    checkout = await CheckoutRepository(session).get(attempt.checkout_id)
    assert checkout is not None
    assert checkout.status is CheckoutStatus.PAID


async def test_a_new_payment_may_be_admitted_after_a_decline(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider
) -> None:
    """A retry needs a fresh hold and a fresh key, and then it is an ordinary payment."""
    provider.default = FakeOutcome.DECLINE
    checkout = await prepared(session, shop)
    admission_service = PaymentAdmissionService(session)
    declined = await admission_service.admit_payment(checkout.id, idempotency_key=KEY, at=NOW)
    assert declined.attempt is not None
    await PaymentExecutionService(session, provider).dispatch(declined.attempt.id)

    readiness = await CheckoutExecutionService(session).prepare_execution(checkout.id, at=NOW)
    assert readiness.ready
    provider.set_outcome(OTHER_KEY, FakeOutcome.SUCCESS)
    retry = await admission_service.admit_payment(checkout.id, idempotency_key=OTHER_KEY, at=NOW)
    assert retry.attempt is not None
    outcome = await PaymentExecutionService(session, provider).dispatch(retry.attempt.id)

    assert outcome.attempt.status is PaymentAttemptStatus.SUCCEEDED
    assert provider.executions_for(KEY) == 1
    assert provider.executions_for(OTHER_KEY) == 1
    assert provider.charges == 1


async def test_a_paid_checkout_refuses_a_second_payment(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider
) -> None:
    """One success per checkout and one per mandate, both refused before a provider is asked."""
    provider.default = FakeOutcome.SUCCESS
    checkout = await prepared(session, shop)
    admission_service = PaymentAdmissionService(session)
    first = await admission_service.admit_payment(checkout.id, idempotency_key=KEY, at=NOW)
    assert first.attempt is not None
    await PaymentExecutionService(session, provider).dispatch(first.attempt.id)

    second = await admission_service.admit_payment(checkout.id, idempotency_key=OTHER_KEY, at=NOW)

    assert not second.admitted
    assert provider.executions_for(OTHER_KEY) == 0
    assert provider.charges == 1


async def test_dispatch_takes_no_lock_across_the_provider_call(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider, row_locks: list[str]
) -> None:
    """The property the whole three transaction shape exists for.

    A provider that observes an open transaction would prove the locks were held across the
    network call. The fake asks the database whether one is open, at the exact moment a real
    provider would be doing its slowest work.
    """
    provider.default = FakeOutcome.SUCCESS
    attempt = await admitted(session, shop)
    watcher = _TransactionWatcher(provider, session, row_locks)
    row_locks.clear()

    await PaymentExecutionService(session, watcher).dispatch(attempt.id)

    assert watcher.saw_open_transaction is False
    # The dispatch transaction locks the attempt and nothing else, which is what lets it commit
    # before the network call without holding anything a provider could delay.
    assert watcher.locks_before == ["payment_attempt"]
    # The outcome transaction is judged on its own. Deadlock freedom is a property of one
    # transaction's ordering, and these are two.
    assert respects_lock_order(row_locks)
    assert row_locks[0] == "spending_mandate"


async def test_the_provider_is_charged_the_frozen_amount(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider
) -> None:
    """What was authorized and what was sent are the same two columns."""
    provider.default = FakeOutcome.SUCCESS
    attempt = await admitted(session, shop)

    await PaymentExecutionService(session, provider).dispatch(attempt.id)

    sent = provider.executions[0]
    assert sent.amount_minor == attempt.amount_minor == PRICE
    assert sent.currency == attempt.currency == "INR"
    assert sent.idempotency_key == KEY
    assert sent.attempt_id == attempt.id
    assert sent.merchant_reference == str(shop.merchant_id)
    assert sent.checkout_reference == str(attempt.checkout_id)


async def test_an_admitted_attempt_survives_a_restart_and_is_still_dispatchable(
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    shop: Shop,
    provider: FakePaymentProvider,
) -> None:
    """The crash recovery case, with the crash between the two commits that can be crashed
    between.

    ADMITTED means the provider was certainly never called, which is the whole reason the
    dispatch mark is committed before the network call rather than after it. A process that
    dies here loses nothing and may safely dispatch.

    The restart is a new session and a new service. The provider is kept, because a payment
    processor does not forget a charge when our process restarts, and a fake that did would
    make this test prove less than it looks like it does.
    """
    provider.default = FakeOutcome.SUCCESS
    attempt = await admitted(session, shop)
    attempt_id = attempt.id
    assert provider.executions == []

    async with factory() as restarted:
        outcome = await PaymentExecutionService(restarted, provider).dispatch(attempt_id)

    assert outcome.attempt.status is PaymentAttemptStatus.SUCCEEDED
    assert provider.executions_for(KEY) == 1
    assert provider.charges == 1


async def test_an_audit_failure_during_success_leaves_nothing_half_recorded(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider
) -> None:
    """Atomicity of the outcome transaction, forced rather than argued.

    The audit append is made to fail after the attempt, the checkout and the stock have all
    been changed in the same transaction. None of it may survive: an attempt that is SUCCEEDED
    without its consumption is a sale with no stock movement, and the system has to stay
    reconcilable rather than be quietly wrong.
    """
    provider.default = FakeOutcome.SUCCESS
    attempt = await admitted(session, shop)
    # Read before the failure rolls back and expires every attribute on these rows.
    attempt_id, checkout_id, reservation_id = (
        attempt.id,
        attempt.checkout_id,
        attempt.reservation_id,
    )
    before = await stock(session, shop.black)
    service = PaymentExecutionService(session, provider)
    _break_audit(service)

    with pytest.raises(RuntimeError, match="audit is unavailable"):
        await service.dispatch(attempt_id)
    await session.rollback()

    reloaded = await session.get(PaymentAttempt, attempt_id)
    assert reloaded is not None
    # The dispatch mark committed on its own and stands. Everything the outcome transaction
    # would have written did not.
    assert reloaded.status is PaymentAttemptStatus.IN_FLIGHT
    assert reloaded.resolved_at is None

    checkout = await CheckoutRepository(session).get(checkout_id)
    assert checkout is not None
    assert checkout.status is CheckoutStatus.OPEN
    assert await stock(session, shop.black) == before
    reservation = await InventoryReservationRepository(session).get(reservation_id)
    assert reservation is not None
    assert reservation.status is ReservationStatus.COMMITTED
    # And the provider did charge, which is why the attempt is left where reconciliation can
    # find it rather than quietly marked failed.
    assert provider.charges == 1


async def test_an_audit_failure_during_a_decline_leaves_nothing_half_recorded(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider
) -> None:
    provider.default = FakeOutcome.DECLINE
    attempt = await admitted(session, shop)
    attempt_id, reservation_id = attempt.id, attempt.reservation_id
    service = PaymentExecutionService(session, provider)
    _break_audit(service)

    with pytest.raises(RuntimeError, match="audit is unavailable"):
        await service.dispatch(attempt_id)
    await session.rollback()

    reloaded = await session.get(PaymentAttempt, attempt_id)
    assert reloaded is not None
    assert reloaded.status is PaymentAttemptStatus.IN_FLIGHT
    reservation = await InventoryReservationRepository(session).get(reservation_id)
    assert reservation is not None
    # The stock was not given back, because the decline was not recorded.
    assert reservation.status is ReservationStatus.COMMITTED


async def test_the_provider_never_sees_an_orm_object(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider
) -> None:
    """The boundary rule, asserted rather than assumed.

    A provider that could navigate to a live checkout could read a number the attempt was
    supposed to have frozen.
    """
    provider.default = FakeOutcome.SUCCESS
    attempt = await admitted(session, shop)

    await PaymentExecutionService(session, provider).dispatch(attempt.id)

    sent = provider.executions[0]
    for value in (
        sent.attempt_id,
        sent.idempotency_key,
        sent.amount_minor,
        sent.currency,
        sent.merchant_reference,
        sent.checkout_reference,
    ):
        assert isinstance(value, uuid.UUID | str | int)
    assert not isinstance(sent.merchant_reference, CheckoutSession | InventoryReservation)


async def test_a_cancellation_is_possible_again_after_a_decline(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider
) -> None:
    provider.default = FakeOutcome.DECLINE
    attempt = await admitted(session, shop)
    await PaymentExecutionService(session, provider).dispatch(attempt.id)

    cancelled = await CheckoutService(session).cancel_checkout(attempt.checkout_id)

    assert cancelled.status is CheckoutStatus.CANCELLED


async def test_a_cancellation_is_still_refused_while_a_payment_is_unknown(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider
) -> None:
    """An unresolved payment holds its quote, exactly as an in flight one does."""
    provider.default = FakeOutcome.AMBIGUOUS
    attempt = await admitted(session, shop)
    await PaymentExecutionService(session, provider).dispatch(attempt.id)

    with pytest.raises(ConflictError) as refused:
        await CheckoutService(session).cancel_checkout(attempt.checkout_id)

    assert refused.value.reason == "payment_in_progress"


async def attempt_count(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(PaymentAttempt)) or 0)


class _TransactionWatcher:
    """A provider that asks whether a database transaction is open while it is being called.

    Wraps the real fake rather than replacing it, so the payment still behaves normally and the
    only added behavior is the observation.
    """

    def __init__(
        self, provider: FakePaymentProvider, session: AsyncSession, locks: list[str]
    ) -> None:
        self._provider = provider
        self._session = session
        self._locks = locks
        self.saw_open_transaction: bool | None = None
        self.locks_before: list[str] = []

    async def execute(self, instruction: PaymentInstruction) -> ProviderResult:
        self.saw_open_transaction = self._session.in_transaction()
        # The seam between the two transactions, which is the only place a test can separate
        # their lock sequences without reaching inside the service.
        self.locks_before = list(self._locks)
        self._locks.clear()
        return await self._provider.execute(instruction)

    async def query(self, query: PaymentQuery) -> ProviderQueryResult:
        return await self._provider.query(query)


def _break_audit(service: PaymentExecutionService) -> None:
    """Make the audit append fail, without touching the code under test.

    The outcome transaction writes the state change and the event together. Failing the event
    is the cheapest way to prove they are one unit of work rather than two that usually happen
    to both succeed.
    """

    class _Broken:
        async def append(self, **_: object) -> None:
            raise RuntimeError("audit is unavailable")

    service._audit = _Broken()  # type: ignore[assignment]
    service._inventory._audit = _Broken()  # type: ignore[assignment]


def test_the_outcome_events_are_stable_names() -> None:
    """Event types are a contract, so they are asserted by value rather than referenced."""
    from agentrank_api.payments import execution

    assert execution.PAYMENT_SUCCEEDED == "payment.succeeded"
    assert execution.PAYMENT_FAILED == "payment.failed"
    assert execution.PAYMENT_UNKNOWN == "payment.unknown"
