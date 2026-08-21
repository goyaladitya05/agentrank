"""A bounded sweep, and two operators running one at the same time.

A sweep is the one operation in this system that touches several payments, so it is the one
place where a mistake is a mistake about many of them at once. Three properties are worth the
whole file:

```text
bounded          it cannot walk the table, and it says when the bound bit
skips ADMITTED   it queries and never charges, whatever is in the work list
survives one     a payment the kernel refuses is a row in the report, not the end of it
```

Beside those is the question that only appears once more than one person is on call. Two
operators sweeping at the same time, and one reconciling while another abandons, both have to
leave exactly one terminal truth and exactly one inventory movement. Nothing here coordinates
them; what makes it safe is the row locks and the state machine underneath, and these tests
force the interleaving rather than hoping for it.
"""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
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
from agentrank_api.inventory.models import InventoryReservation, ReservationStatus
from agentrank_api.inventory.repository import InventoryReservationRepository
from agentrank_api.mandates.repository import MandateRepository
from agentrank_api.payments.admission import PAYMENT_RESOURCE, PaymentAdmissionService
from agentrank_api.payments.execution import PROVIDER_NEVER_EXECUTED, PaymentExecutionService
from agentrank_api.payments.fake import FakeOutcome, FakePaymentProvider
from agentrank_api.payments.models import OutcomeSource, PaymentAttempt, PaymentAttemptStatus
from agentrank_api.payments.operations import PaymentOperationResult, PaymentSweep
from agentrank_api.payments.provider import PaymentQuery, ProviderQueryResult
from agentrank_api.payments.recovery import (
    OPERATOR_ABANDONED,
    AbandonmentReason,
    PaymentRecoveryService,
)
from agentrank_api.payments.repository import PaymentAttemptRepository
from agentrank_api.payments.service import PaymentService

pytestmark = pytest.mark.anyio

NOW = datetime.now(UTC)
HOUR = timedelta(hours=1)
PRICE = 499900
STOCK = 20
BLACK = IntentConstraintSpec.required_attribute("color", ConstraintOperator.EQ, "black")

# A concurrent test that goes wrong blocks on a row lock rather than failing, so every gather
# is bounded. Generous enough never to fire on a healthy database.
CONCURRENCY_TIMEOUT = 30

# How long both operations are watched before concluding they are genuinely queued on the gate.
LOCK_WAIT = 1.5


class RefusingProvider(FakePaymentProvider):
    """A provider that refuses to answer about one identity.

    A real integration can fail in ways that are neither a decline nor a timeout: credentials
    revoked, an account suspended, an operation the processor will not discuss. Those reach this
    application as a deliberate error rather than as an outcome, and a sweep has to survive one.

    Configured per key so that a batch can contain exactly one payment the provider will not
    talk about, which is the case worth asserting: the other payments in the batch must still be
    processed and must still be reported.
    """

    def __init__(self, refuse: str, **fields: object) -> None:
        super().__init__(**fields)  # type: ignore[arg-type]
        self.refuse = refuse

    async def query(self, query: PaymentQuery) -> ProviderQueryResult:
        if query.idempotency_key == self.refuse:
            raise ConflictError(
                "provider_unavailable",
                f"the provider will not answer about {query.idempotency_key}",
                resource=PAYMENT_RESOURCE,
            )
        return await super().query(query)


@dataclass(frozen=True, slots=True)
class Shop:
    merchant_id: uuid.UUID
    black: uuid.UUID


@dataclass(frozen=True, slots=True)
class Payment:
    """One payment as plain values.

    Deliberately not a `PaymentAttempt`. Half the reads in this file expire the session so that
    they see what another connection committed, and a mapped object held across one of those
    tries to refresh itself the moment an attribute is touched, which in an async session is an
    error rather than a query. Identifiers do not go stale.
    """

    attempt_id: uuid.UUID
    checkout_id: uuid.UUID
    reservation_id: uuid.UUID


@pytest.fixture
async def shop(session: AsyncSession) -> Shop:
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


async def prepared(session: AsyncSession, shop: Shop) -> CheckoutSession:
    """A quote with its own mandate, authorized and holding stock."""
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
    checkout = await CheckoutRepository(session).create(
        merchant_id=shop.merchant_id,
        mandate_id=mandate.id,
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


async def admitted(session: AsyncSession, shop: Shop, *, key: str) -> Payment:
    """A payment that has provably never reached a provider."""
    checkout = await prepared(session, shop)
    admission = await PaymentAdmissionService(session).admit_payment(
        checkout.id, idempotency_key=key, at=NOW
    )
    assert admission.attempt is not None
    written = Payment(
        attempt_id=admission.attempt.id,
        checkout_id=admission.attempt.checkout_id,
        reservation_id=admission.attempt.reservation_id,
    )
    await session.commit()
    return written


async def unresolved(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider, *, key: str
) -> Payment:
    """A payment in UNKNOWN, reached by a dispatch whose answer was ambiguous."""
    payment = await admitted(session, shop, key=key)
    await PaymentExecutionService(session, provider).dispatch(payment.attempt_id)
    resolved = await PaymentAttemptRepository(session).get(payment.attempt_id)
    assert resolved is not None
    assert resolved.status is PaymentAttemptStatus.UNKNOWN
    await session.commit()
    return payment


async def stock(session: AsyncSession, variant_id: uuid.UUID) -> int:
    """The shelf, read fresh. PostgreSQL reads committed, so a new statement sees a new value.

    The variant is expired rather than the whole session, so a mapped object a test is holding
    elsewhere is not invalidated as a side effect of asking about stock.
    """
    variant = await session.get(Variant, variant_id)
    if variant is not None:
        session.expire(variant)
    return int(
        await session.scalar(select(Variant.inventory_quantity).where(Variant.id == variant_id))
        or 0
    )


async def reread(session: AsyncSession, attempt_id: uuid.UUID) -> PaymentAttempt:
    """One attempt as another connection left it, rather than as this session last saw it."""
    stale = await session.get(PaymentAttempt, attempt_id)
    if stale is not None:
        session.expire(stale)
    found = await PaymentAttemptRepository(session).get(attempt_id)
    assert found is not None
    return found


async def sweep_in_new_session(
    factory: async_sessionmaker[AsyncSession], provider: FakePaymentProvider, *, limit: int = 50
) -> PaymentSweep:
    """One sweep on its own connection, exactly as a second operator would run it."""
    async with factory() as session:
        return await PaymentService(session, provider).reconcile_unresolved(limit=limit)


async def abandon_in_new_session(
    factory: async_sessionmaker[AsyncSession], attempt_id: uuid.UUID
) -> str:
    """One abandonment on its own connection, reporting what it was allowed to do."""
    async with factory() as session:
        try:
            outcome = await PaymentRecoveryService(session).abandon_payment_attempt(
                attempt_id, reason=AbandonmentReason.PROVIDER_UNREACHABLE
            )
        except ConflictError as refused:
            return refused.reason
        return "abandoned" if outcome.changed else "unchanged"


async def still_waiting(*running: asyncio.Task[object]) -> bool:
    """Whether every operation is still blocked, watched for a bounded window."""
    done, _ = await asyncio.wait(set(running), timeout=LOCK_WAIT)
    return not done


async def held(session: AsyncSession, reservation_id: uuid.UUID) -> ReservationStatus:
    """A reservation as another connection left it, rather than as this session last saw it."""
    stale = await session.get(InventoryReservation, reservation_id)
    if stale is not None:
        session.expire(stale)
    reservation = await InventoryReservationRepository(session).get(reservation_id)
    assert reservation is not None
    return reservation.status


async def events(session: AsyncSession, attempt_id: uuid.UUID) -> list[str]:
    return [
        event.event_type
        for event in await AuditRepository(session).list_for_resource(
            resource_type=PAYMENT_RESOURCE, resource_id=attempt_id
        )
    ]


async def test_a_sweep_reconciles_every_unresolved_payment_and_reports_each_one(
    session: AsyncSession, shop: Shop
) -> None:
    """The ordinary pass: several stuck payments, one command, one report.

    Two of the three answers a provider can give are here, and they are the two that end a
    payment. One charge went through and only the answer was lost, so the query finds it and
    the sale completes. Two never reached the provider at all, and once its visibility window
    has passed it says so definitively, so both end as never executed and both give their stock
    back. The third answer, an absence that is not final, has its own test below, because it
    needs a provider that promises nothing and this one promises a window.
    """
    provider = FakePaymentProvider(clock=NOW, visibility_window=HOUR)
    provider.set_outcome("pay-lost-000001", FakeOutcome.LOST_RESPONSE)
    provider.set_outcome("pay-absent-00001", FakeOutcome.AMBIGUOUS)
    provider.set_outcome("pay-nothing-0001", FakeOutcome.AMBIGUOUS)

    lost = await unresolved(session, shop, provider, key="pay-lost-000001")
    absent = await unresolved(session, shop, provider, key="pay-absent-00001")
    nothing = await unresolved(session, shop, provider, key="pay-nothing-0001")
    before = await stock(session, shop.black)

    # The window has passed for every dispatch above, so an empty answer is now final. The
    # payment whose charge went through is unaffected: the fake has a record for that one.
    provider.clock = NOW + 2 * HOUR
    swept = await PaymentService(session, provider).reconcile_unresolved()

    results = {item.attempt_id: item for item in swept.items}
    assert results[lost.attempt_id].result is PaymentOperationResult.RESOLVED_SUCCESS
    assert results[lost.attempt_id].status_after is PaymentAttemptStatus.SUCCEEDED
    assert results[nothing.attempt_id].result is PaymentOperationResult.PROVIDER_NEVER_EXECUTED
    assert results[absent.attempt_id].result is PaymentOperationResult.PROVIDER_NEVER_EXECUTED
    assert swept.resolved == 3
    assert swept.still_unresolved == 0

    # One success, one unit sold, and the two that never happened gave their stock back.
    assert await stock(session, shop.black) == before - 1
    assert (await reread(session, lost.attempt_id)).status is PaymentAttemptStatus.SUCCEEDED
    ended = await reread(session, nothing.attempt_id)
    assert ended.status is PaymentAttemptStatus.FAILED
    assert ended.failure_code == PROVIDER_NEVER_EXECUTED
    assert provider.charges == 1


async def test_a_sweep_leaves_a_payment_with_no_provider_record_exactly_where_it_was(
    session: AsyncSession, shop: Shop
) -> None:
    """A provider that promises nothing resolves nothing, and the sweep says so.

    This is the state abandonment exists for, and the sweep must not be tempted to end it. The
    hold stays committed, no `resolved_at` is stamped and the report counts it as still
    unresolved rather than as work done.
    """
    provider = FakePaymentProvider(default=FakeOutcome.AMBIGUOUS, clock=NOW)
    attempt = await unresolved(session, shop, provider, key="pay-absent-00001")
    before = await stock(session, shop.black)

    swept = await PaymentService(session, provider).reconcile_unresolved()

    [item] = swept.items
    assert item.result is PaymentOperationResult.PROVIDER_ABSENT
    assert item.status_after is PaymentAttemptStatus.UNKNOWN
    assert item.still_unresolved
    assert swept.resolved == 0
    assert swept.still_unresolved == 1

    still = await reread(session, attempt.attempt_id)
    assert still.status is PaymentAttemptStatus.UNKNOWN
    assert still.resolved_at is None
    assert await stock(session, shop.black) == before
    reservation = await InventoryReservationRepository(session).get(attempt.reservation_id)
    assert reservation is not None
    assert reservation.status is ReservationStatus.COMMITTED


async def test_a_sweep_never_dispatches_an_admitted_payment(
    session: AsyncSession, shop: Shop
) -> None:
    """The safety property of the whole command, and the reason resume is separate.

    An ADMITTED payment is in the work list, because it is stuck and somebody has to finish it.
    The sweep reports it and does nothing to it. No execute, no query, no state change. An
    operator running this across a work list must never discover afterwards that some of it was
    charged.
    """
    provider = FakePaymentProvider(default=FakeOutcome.SUCCESS, clock=NOW)
    stuck = await admitted(session, shop, key="pay-admitted-0001")
    before = await stock(session, shop.black)

    swept = await PaymentService(session, provider).reconcile_unresolved()

    [item] = swept.items
    assert item.attempt_id == stuck.attempt_id
    assert item.result is PaymentOperationResult.SKIPPED_NOT_DISPATCHED
    assert item.status_after is PaymentAttemptStatus.ADMITTED
    assert item.still_unresolved
    assert swept.resolved == 0

    # Nothing reached the provider at all. Not an execute, and not even a query.
    assert provider.executions == []
    assert provider.queries == []
    assert provider.charges == 0
    assert (await reread(session, stuck.attempt_id)).status is PaymentAttemptStatus.ADMITTED
    assert await stock(session, shop.black) == before


async def test_a_sweep_is_bounded_and_reports_the_bound_it_used(
    session: AsyncSession, shop: Shop
) -> None:
    """A batch is exactly its listing, so it cannot walk a table and cannot grow while it runs."""
    provider = FakePaymentProvider(default=FakeOutcome.AMBIGUOUS, clock=NOW)
    for index in range(3):
        await unresolved(session, shop, provider, key=f"pay-bounded-{index:04d}")

    swept = await PaymentService(session, provider).reconcile_unresolved(limit=2)

    assert swept.limit == 2
    assert len(swept.items) == 2
    assert swept.truncated
    # Two queried, and specifically not three. The third belongs to the next sweep.
    assert len(provider.queries) == 2


async def test_one_payment_the_provider_refuses_does_not_cost_the_report_on_the_others(
    session: AsyncSession, shop: Shop
) -> None:
    """A refusal is a row in the report rather than the end of it.

    The middle payment is one the provider will not discuss, which reaches this application as a
    deliberate error rather than an outcome. The sweep records it by its stable code and carries
    on, so the operator still sees what happened to the other two. That is safe because each
    item's outcome was already committed by the reconciliation that produced it: there is no
    batch transaction for one bad item to roll back.
    """
    provider = RefusingProvider("pay-refused-0002", clock=NOW)
    provider.set_outcome("pay-refused-0001", FakeOutcome.LOST_RESPONSE)
    provider.set_outcome("pay-refused-0002", FakeOutcome.AMBIGUOUS)
    provider.set_outcome("pay-refused-0003", FakeOutcome.LOST_RESPONSE)

    first = await unresolved(session, shop, provider, key="pay-refused-0001")
    refused = await unresolved(session, shop, provider, key="pay-refused-0002")
    third = await unresolved(session, shop, provider, key="pay-refused-0003")
    before = await stock(session, shop.black)

    swept = await PaymentService(session, provider).reconcile_unresolved()

    assert [item.attempt_id for item in swept.items] == [
        first.attempt_id,
        refused.attempt_id,
        third.attempt_id,
    ]
    results = {item.attempt_id: item for item in swept.items}
    assert results[first.attempt_id].result is PaymentOperationResult.RESOLVED_SUCCESS
    assert results[refused.attempt_id].result is PaymentOperationResult.REFUSED
    assert results[refused.attempt_id].detail == "provider_unavailable"
    # Deliberately unknown rather than the snapshot. Nothing was read back for this one.
    assert results[refused.attempt_id].status_after is None
    assert results[third.attempt_id].result is PaymentOperationResult.RESOLVED_SUCCESS
    assert swept.resolved == 2
    assert swept.still_unresolved == 1

    # The two payments either side of the refusal really were settled, and the refused one was
    # left exactly as it was.
    assert (await reread(session, first.attempt_id)).status is PaymentAttemptStatus.SUCCEEDED
    assert (await reread(session, third.attempt_id)).status is PaymentAttemptStatus.SUCCEEDED
    assert (await reread(session, refused.attempt_id)).status is PaymentAttemptStatus.UNKNOWN
    assert await stock(session, shop.black) == before - 2


async def test_two_operators_sweeping_at_once_apply_one_outcome_and_one_decrement(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession], shop: Shop
) -> None:
    """Two people on call, both running the sweep, and one payment between them.

    The charge went through and the answer was lost, so both sweeps query the provider and both
    are told the same definitive success. That is the dangerous shape: two writers holding the
    same true answer about one payment. Whichever takes the attempt lock first records it and
    the other finds it already recorded, writes nothing, consumes no second unit and appends no
    second event.

    The gate forces the interleaving. It holds the row the outcome transaction has to lock, so
    both sweeps are provably past their queries and queued on the same lock before either can
    decide anything. Nothing in the application coordinates them; what makes this safe is the
    lock and the state machine underneath it.
    """
    provider = FakePaymentProvider(default=FakeOutcome.LOST_RESPONSE, clock=NOW)
    attempt = await unresolved(session, shop, provider, key="pay-racing-00001")
    attempt_id = attempt.attempt_id
    before = await stock(session, shop.black)
    await session.commit()

    async with asyncio.timeout(CONCURRENCY_TIMEOUT):
        async with factory() as gate:
            # The lock the outcome transaction takes last, before it writes anything.
            await PaymentAttemptRepository(gate).get_for_update(attempt_id)
            sweeps: list[asyncio.Task[PaymentSweep]] = [
                asyncio.create_task(sweep_in_new_session(factory, provider)),
                asyncio.create_task(sweep_in_new_session(factory, provider)),
            ]
            assert await still_waiting(*sweeps)
            await gate.rollback()

        first, second = await asyncio.gather(*sweeps)

    outcomes = sorted(item.result.value for swept in (first, second) for item in swept.items)
    # One of them resolved it and the other found it already resolved. Never two resolutions.
    assert outcomes == ["already_terminal", "resolved_success"]

    settled = await reread(session, attempt_id)
    assert settled.status is PaymentAttemptStatus.SUCCEEDED
    assert settled.outcome_source is OutcomeSource.RECONCILIATION
    # The assertions the whole test is for. One unit, one charge, one success event.
    assert await stock(session, shop.black) == before - 1
    assert provider.charges == 1
    assert (await events(session, attempt_id)).count("payment.succeeded") == 1
    async with factory() as reader:
        succeeded = await reader.scalar(
            select(func.count())
            .select_from(PaymentAttempt)
            .where(PaymentAttempt.status == PaymentAttemptStatus.SUCCEEDED)
        )
        assert succeeded == 1


async def test_a_reconciled_success_refuses_a_later_abandonment(
    session: AsyncSession, shop: Shop
) -> None:
    """Success wins, and abandonment does not release stock that has already been sold.

    The expensive direction of the race, made deterministic. Reconciliation resolved the payment
    and the unit is gone. An abandonment arriving afterwards is refused by name and touches
    nothing: releasing here would put a sold unit back on the shelf for money that really moved.
    """
    provider = FakePaymentProvider(default=FakeOutcome.LOST_RESPONSE, clock=NOW)
    attempt = await unresolved(session, shop, provider, key="pay-racing-00001")
    before = await stock(session, shop.black)

    await PaymentService(session, provider).reconcile(attempt.attempt_id)
    assert (await reread(session, attempt.attempt_id)).status is PaymentAttemptStatus.SUCCEEDED

    with pytest.raises(ConflictError) as refused:
        await PaymentRecoveryService(session).abandon_payment_attempt(
            attempt.attempt_id, reason=AbandonmentReason.PROVIDER_UNREACHABLE
        )

    assert refused.value.reason == "payment_already_succeeded"
    settled = await reread(session, attempt.attempt_id)
    assert settled.status is PaymentAttemptStatus.SUCCEEDED
    assert settled.failure_code is None
    reservation = await InventoryReservationRepository(session).get(attempt.reservation_id)
    assert reservation is not None
    # Consumed and left consumed. The abandonment released nothing.
    assert reservation.status is ReservationStatus.CONSUMED
    assert await stock(session, shop.black) == before - 1


async def test_an_abandoned_payment_meeting_a_later_success_records_a_conflict(
    session: AsyncSession, shop: Shop
) -> None:
    """Abandonment wins, and a later provider success is surfaced rather than applied.

    The other direction, and the one the residual risk is about. The operator gave up, the stock
    went back, and the provider then reveals that the money moved after all. The terminal state
    is not rewritten: it stands, a `payment.outcome_conflict` records the disagreement, and no
    unit is taken off the shelf a second time. That is honest rather than convenient, and it is
    exactly the risk `payment.abandoned` states in its own payload.
    """
    provider = FakePaymentProvider(default=FakeOutcome.LOST_RESPONSE, clock=NOW)
    attempt = await unresolved(session, shop, provider, key="pay-racing-00001")
    before = await stock(session, shop.black)

    given_up = await PaymentRecoveryService(session).abandon_payment_attempt(
        attempt.attempt_id, reason=AbandonmentReason.PROVIDER_UNREACHABLE
    )
    assert given_up.changed

    swept = await PaymentService(session, provider).reconcile_unresolved()
    # The work list no longer holds it, so a sweep has nothing to do about it at all.
    assert swept.items == ()

    # Asked directly, the way an operator checking an abandoned payment would.
    outcome = await PaymentService(session, provider).reconcile(attempt.attempt_id)
    assert outcome.conflict is None
    assert outcome.provider_called is False

    ended = await reread(session, attempt.attempt_id)
    assert ended.status is PaymentAttemptStatus.FAILED
    assert ended.failure_code == OPERATOR_ABANDONED
    assert ended.outcome_source is OutcomeSource.OPERATOR
    reservation = await InventoryReservationRepository(session).get(attempt.reservation_id)
    assert reservation is not None
    assert reservation.status is ReservationStatus.RELEASED
    # Nothing was consumed, and nothing was consumed twice either.
    assert await stock(session, shop.black) == before
    checkout = await CheckoutRepository(session).get(attempt.checkout_id)
    assert checkout is not None
    assert checkout.status is CheckoutStatus.OPEN


async def test_a_reconciliation_racing_an_abandonment_leaves_one_terminal_truth(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession], shop: Shop
) -> None:
    """The real race, forced, with the outcome deliberately not pinned to one winner.

    One operator sweeps while another gives up on the same payment. Which of them commits first
    is not guaranteed and the test does not pretend otherwise: both branches are asserted, and
    what is fixed is that exactly one of the two outcomes stands, that it is never rewritten,
    and that inventory moves at most once whichever way it goes.

    In practice the abandonment wins here every time, and the reason is structural rather than
    lucky. Once the gate lifts, the abandonment is already queued on the attempt row, while the
    sweep still has to finish its provider query and take the mandate, the checkout, the variant
    rows and the hold before it reaches the same lock. That makes this the branch worth having:
    it is the one that produces a `payment.outcome_conflict`, where a provider reveals a success
    for a payment somebody has already given up on. The other direction is pinned deterministically
    by the test above rather than left to a race that does not go that way.

    The gate holds the payment attempt row, which both the outcome transaction and the
    abandonment have to take, so both are queued before either can write.
    """
    provider = FakePaymentProvider(default=FakeOutcome.LOST_RESPONSE, clock=NOW)
    attempt = await unresolved(session, shop, provider, key="pay-racing-00001")
    attempt_id, reservation_id = attempt.attempt_id, attempt.reservation_id
    before = await stock(session, shop.black)
    await session.commit()

    async with asyncio.timeout(CONCURRENCY_TIMEOUT):
        async with factory() as gate:
            await PaymentAttemptRepository(gate).get_for_update(attempt_id)
            racers: list[asyncio.Task[object]] = [
                asyncio.create_task(sweep_in_new_session(factory, provider)),
                asyncio.create_task(abandon_in_new_session(factory, attempt_id)),
            ]
            assert await still_waiting(*racers)
            await gate.rollback()

        swept, abandonment = await asyncio.gather(*racers)

    assert isinstance(swept, PaymentSweep)
    settled = await reread(session, attempt_id)
    reservation = await held(session, reservation_id)
    recorded = await events(session, attempt_id)

    if settled.status is PaymentAttemptStatus.SUCCEEDED:
        # Reconciliation committed first. The abandonment found a settled payment and was
        # refused by name, and the unit it would have released stayed sold.
        assert abandonment == "payment_already_succeeded"
        assert settled.outcome_source is OutcomeSource.RECONCILIATION
        assert reservation is ReservationStatus.CONSUMED
        assert await stock(session, shop.black) == before - 1
        assert "payment.abandoned" not in recorded
    else:
        # The abandonment committed first. The provider's success is a disagreement with a
        # terminal state, so it is recorded and not applied, and the stock stays released. The
        # sweep reports it as a conflict rather than as a resolution, which is the one result
        # in the vocabulary that means a person has to look.
        assert abandonment == "abandoned"
        assert [item.result for item in swept.items] == [PaymentOperationResult.OUTCOME_CONFLICT]
        assert settled.status is PaymentAttemptStatus.FAILED
        assert settled.failure_code == OPERATOR_ABANDONED
        assert settled.outcome_source is OutcomeSource.OPERATOR
        assert reservation is ReservationStatus.RELEASED
        assert await stock(session, shop.black) == before
        assert "payment.outcome_conflict" in recorded

    # Whichever way it went, the terminal state was written once and only once.
    assert recorded.count("payment.succeeded") + recorded.count("payment.abandoned") == 1
    assert provider.charges == 1


async def test_the_sweep_is_reachable_from_the_command_line_and_not_over_http(
    session: AsyncSession, shop: Shop
) -> None:
    """A sweep is an operator action, so it lives where operator actions live.

    An HTTP endpoint that swept payments would let anybody who can reach the process drive the
    recovery path, and there is still nothing that authenticates a caller. It is also the one
    command a scheduler would be most tempted to point at, which is the second reason it is not
    a route: nothing should be able to run this without a person asking.
    """
    from agentrank_api.cli import build_parser
    from agentrank_api.main import create_app

    parsed = build_parser().parse_args(["payments", "reconcile-unresolved", "--limit", "5"])
    assert parsed.limit == 5

    paths = set(create_app().openapi()["paths"])
    assert not any("sweep" in path or "unresolved" in path for path in paths)
    # The positive half, so an empty set can never satisfy the negative one.
    assert "/api/v1/commerce/payments/{attempt_id}/reconcile" in paths


def test_nothing_schedules_a_sweep() -> None:
    """No cron, no worker, no timer and no background task anywhere in the application.

    Asserted rather than assumed, because this is the property that would be easiest to lose by
    accident and the most expensive to lose: an ambiguous payment queried on a timer is a
    payment that eventually gets charged twice by something nobody is watching. A sweep happens
    because a person ran a command.
    """
    from pathlib import Path

    source = Path(__file__).resolve().parent.parent / "apps" / "api" / "src"
    banned = ("apscheduler", "celery", "create_task(", "add_periodic", "BackgroundTasks")
    offences = [
        f"{path}: {word}"
        for path in source.rglob("*.py")
        for word in banned
        if word in path.read_text()
    ]
    assert offences == []
    # And the one place a background loop would most plausibly be started.
    assert "reconcile" not in (source / "agentrank_api" / "main.py").read_text()


async def force_in_flight(session: AsyncSession, attempt_id: uuid.UUID) -> None:
    """Leave an attempt where a crash between the dispatch commit and the wire would leave it."""
    await session.execute(
        text(
            "UPDATE payment_attempt SET status = 'IN_FLIGHT', dispatched_at = now() WHERE id = :id"
        ),
        {"id": attempt_id},
    )
    await session.commit()
    session.expire_all()


async def test_a_sweep_reconciles_a_payment_left_in_flight_by_a_crash(
    session: AsyncSession, shop: Shop
) -> None:
    """IN_FLIGHT is queried, never re-sent, and the sweep is what finds it.

    A process that died mid dispatch leaves an attempt that may or may not have reached the
    provider. It is in the work list, the sweep asks rather than re-sends, and the provider's
    answer is what resolves it. The execute count is the assertion: one, from the dispatch that
    died, and not two.
    """
    provider = FakePaymentProvider(default=FakeOutcome.SUCCESS, clock=NOW)
    stuck = await admitted(session, shop, key="pay-crashed-0001")
    attempt_id = stuck.attempt_id
    await force_in_flight(session, attempt_id)

    swept = await PaymentService(session, provider).reconcile_unresolved()

    [item] = swept.items
    assert item.status_before is PaymentAttemptStatus.IN_FLIGHT
    # The provider has no record, because the dispatch that died never actually reached it.
    assert item.result is PaymentOperationResult.PROVIDER_ABSENT
    assert item.status_after is PaymentAttemptStatus.UNKNOWN
    # Queried once and executed never. A sweep does not send payments.
    assert provider.queries == ["pay-crashed-0001"]
    assert provider.executions == []
    assert provider.charges == 0
