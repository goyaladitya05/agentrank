"""Preparing a checkout while something else withdraws the authority to prepare it.

Every test here is one interleaving that used to produce a wrong answer. Preparation read a
mandate and a checkout without holding either, decided both gates allowed, blocked on
inventory, and woke up in a world where the mandate had been revoked, the checkout had been
cancelled, or the quote had simply expired while it waited. It then wrote a reservation and
reported ready.

The repair is a documented lock order and a second clock reading. These tests are what say
so: each one forces the interleaving rather than hoping for it, and each one fails against
the implementation that lacked the lock or the reading it covers.

The technique is the one the inventory tests already use. A third transaction holds a lock
while the racing operations start, so they are provably in flight and queued before any of
them can decide anything. Two coroutines gathered without a gate would take their turns by
accident, and a test that passes by accident would pass with no locking at all.
"""

import asyncio
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import groupby

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agentrank_api.audit.repository import AuditRepository
from agentrank_api.checkout.authorization import CheckoutAuthorizationViolation
from agentrank_api.checkout.execution import (
    CheckoutExecutionReadiness,
    CheckoutExecutionService,
)
from agentrank_api.checkout.models import CheckoutSession, CheckoutStatus
from agentrank_api.checkout.quote import QuotedLine
from agentrank_api.checkout.repository import CheckoutRepository
from agentrank_api.checkout.service import CHECKOUT_RESOURCE, CheckoutService
from agentrank_api.commerce.repository import CatalogRepository, MerchantRepository
from agentrank_api.constraints.repository import IntentConstraintRepository
from agentrank_api.constraints.rules import ConstraintOperator, IntentConstraintSpec
from agentrank_api.database import create_session_factory
from agentrank_api.inventory.models import InventoryReservation, ReservationStatus
from agentrank_api.inventory.repository import InventoryReservationRepository
from agentrank_api.locking import LOCK_ORDER, respects_lock_order
from agentrank_api.mandates.models import MandateStatus, SpendingMandate
from agentrank_api.mandates.repository import MandateRepository
from agentrank_api.mandates.service import MANDATE_RESOURCE, MandateService

pytestmark = pytest.mark.anyio

NOW = datetime.now(UTC)
HOUR = timedelta(hours=1)
PRICE = 499900
BLACK = IntentConstraintSpec.required_attribute("color", ConstraintOperator.EQ, "black")

# A concurrent test that goes wrong blocks on a row lock rather than failing, so every
# gather is bounded. Generous enough never to fire on a healthy database.
CONCURRENCY_TIMEOUT = 30

# How long an attempt is watched before concluding it is genuinely waiting on a lock. An
# attempt that is really blocked never completes, so a longer window cannot make these tests
# flaky, and an implementation that took no lock would finish here in milliseconds.
LOCK_WAIT = 1.5

# How long the deliberately short lived quote has left. Shorter than `LOCK_WAIT`, so a
# preparation held at the variant lock for one window is certain to resume after the quote it
# was preparing has expired. Nothing random about it: the attempt cannot proceed until the
# gate lets go, and the gate does not let go until the window has closed.
EXPIRY_WINDOW = timedelta(milliseconds=500)


@dataclass(frozen=True, slots=True)
class Shop:
    """A merchant whose mandate and constraints both permit exactly one black charger."""

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
        inventory_quantity=1,
        attributes={"color": "black"},
    )
    await session.commit()
    return Shop(merchant_id=merchant.id, mandate=mandate, black=black.id)


async def quote(
    session: AsyncSession, shop: Shop, *, expires_at: datetime | None = None
) -> CheckoutSession:
    """A quote written straight through the repository.

    Deliberately not through `CheckoutService`, so the only events these tests see are the
    ones the code under test appends.
    """
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
    return checkout


@pytest.fixture
async def shop(session: AsyncSession) -> Shop:
    return await build_shop(session)


@pytest.fixture
def factory(catalog_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Independent sessions, so that operations can genuinely race."""
    return create_session_factory(catalog_engine)


async def prepare_in_new_session(
    factory: async_sessionmaker[AsyncSession], checkout_id: uuid.UUID, *, at: datetime
) -> CheckoutExecutionReadiness:
    """One preparation on its own connection. It commits or rolls back before returning."""
    async with factory() as session:
        return await CheckoutExecutionService(session).prepare_execution(checkout_id, at=at)


async def cancel_in_new_session(
    factory: async_sessionmaker[AsyncSession], checkout_id: uuid.UUID
) -> CheckoutSession:
    async with factory() as session:
        return await CheckoutService(session).cancel_checkout(checkout_id)


async def revoke_in_new_session(
    factory: async_sessionmaker[AsyncSession], mandate_id: uuid.UUID
) -> SpendingMandate:
    async with factory() as session:
        return await MandateService(session).revoke_mandate(mandate_id)


async def still_waiting(*attempts: asyncio.Task[object]) -> bool:
    """Whether every attempt is still blocked, watched for a bounded window.

    Not a sleep that a result depends on. A genuinely blocked attempt never finishes, and an
    implementation that took no lock would be done long before the window closes. It is how
    a test observes that PostgreSQL, rather than luck, is what serialises these operations.
    """
    done, _ = await asyncio.wait(set(attempts), timeout=LOCK_WAIT)
    return not done


async def reservation_count(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(InventoryReservation)) or 0)


async def active_reservation(
    session: AsyncSession, checkout_id: uuid.UUID
) -> InventoryReservation | None:
    return await InventoryReservationRepository(session).get_holding_for_checkout(checkout_id)


async def test_a_cancellation_in_flight_blocks_and_then_refuses_preparation(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession], shop: Shop
) -> None:
    """The cancellation race, with cancellation winning.

    A transaction cancels the quote and holds it uncommitted. A preparation starts and must
    not be able to read past it: an unlocked read would see the last committed row, which
    still says OPEN, and would go on to hold stock for a quote that is about to be withdrawn.
    The reservation's own foreign key does not stop that, because writing one takes
    `FOR KEY SHARE` on the checkout and cancelling takes `FOR NO KEY UPDATE`, and those two
    modes do not conflict.

    Under the repair the preparation waits, and when it is finally granted the row it reads
    the cancellation rather than the state it would have found on the way in.
    """
    checkout = await quote(session, shop)

    async with asyncio.timeout(CONCURRENCY_TIMEOUT):
        async with factory() as canceller:
            withdrawn = await CheckoutRepository(canceller).get_for_update(checkout.id)
            assert withdrawn is not None
            assert await CheckoutRepository(canceller).cancel(withdrawn) is True

            attempt = asyncio.create_task(prepare_in_new_session(factory, checkout.id, at=NOW))
            # Blocked on the checkout row rather than reading around it.
            assert await still_waiting(attempt)
            await canceller.commit()

        readiness = await attempt

    assert not readiness.ready
    assert (
        CheckoutAuthorizationViolation.CHECKOUT_NOT_OPEN
        in readiness.authorization.financial.violations
    )
    assert readiness.reservation is None
    # The outcome that must never exist: a cancelled checkout holding stock it was granted
    # after the cancellation committed.
    async with factory() as reader:
        assert await reservation_count(reader) == 0


async def test_a_revocation_in_flight_blocks_and_then_refuses_preparation(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession], shop: Shop
) -> None:
    """The revocation race, with revocation winning.

    Same shape as the cancellation above, one level up the lock order. A preparation that
    read the mandate without holding it would see the last committed row, which still says
    ACTIVE, and would hold stock under an authorization being withdrawn as it read it.
    """
    checkout = await quote(session, shop)

    async with asyncio.timeout(CONCURRENCY_TIMEOUT):
        async with factory() as revoker:
            withdrawn = await MandateRepository(revoker).get_for_update(shop.mandate.id)
            assert withdrawn is not None
            assert await MandateRepository(revoker).revoke(withdrawn) is True

            attempt = asyncio.create_task(prepare_in_new_session(factory, checkout.id, at=NOW))
            assert await still_waiting(attempt)
            await revoker.commit()

        readiness = await attempt

    assert not readiness.ready
    assert (
        CheckoutAuthorizationViolation.MANDATE_NOT_ACTIVE
        in readiness.authorization.financial.violations
    )
    assert readiness.reservation is None
    async with factory() as reader:
        assert await reservation_count(reader) == 0


async def test_preparation_holds_the_mandate_and_the_checkout_against_withdrawal(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession], shop: Shop
) -> None:
    """The same two races from the other side, with preparation winning.

    A gate holds the variant rows, so the preparation is provably past both gates and
    holding the mandate and the checkout while it waits for stock. A cancellation and a
    revocation started at that point must both wait for it rather than deciding underneath
    it, which is what makes the serialisation mutual rather than one sided.

    Preparation then succeeds, and the withdrawals apply to the world it left behind. The
    cancellation gives the stock straight back, which is why the shelf ends up clear even
    though a reservation was written.
    """
    checkout = await quote(session, shop)

    async with asyncio.timeout(CONCURRENCY_TIMEOUT):
        async with factory() as gate:
            await InventoryReservationRepository(gate).lock_variants(
                merchant_id=shop.merchant_id, variant_ids=[shop.black]
            )
            attempt = asyncio.create_task(prepare_in_new_session(factory, checkout.id, at=NOW))
            assert await still_waiting(attempt)

            withdrawals: list[asyncio.Task[object]] = [
                asyncio.create_task(cancel_in_new_session(factory, checkout.id)),
                asyncio.create_task(revoke_in_new_session(factory, shop.mandate.id)),
            ]
            assert await still_waiting(*withdrawals)
            await gate.rollback()

        readiness = await attempt
        cancelled, revoked = await asyncio.gather(*withdrawals)

    assert readiness.ready
    assert readiness.reservation is not None
    assert isinstance(cancelled, CheckoutSession)
    assert cancelled.status is CheckoutStatus.CANCELLED
    assert isinstance(revoked, SpendingMandate)
    assert revoked.status is MandateStatus.REVOKED

    async with factory() as reader:
        # The reservation exists as history and holds nothing: the cancellation released it.
        assert await reservation_count(reader) == 1
        assert await active_reservation(reader, checkout.id) is None
        held = await InventoryReservationRepository(reader).get(readiness.reservation.id)
        assert held is not None
        assert held.status is ReservationStatus.RELEASED


async def test_expiry_crossed_while_waiting_for_stock_refuses(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession], shop: Shop
) -> None:
    """Locks hold state still. They do not hold the clock still.

    The quote has half a second left when the preparation authorizes it, and the gate holds
    the variant rows for longer than that. A preparation that pinned one instant before it
    blocked would resume and write a reservation whose expiry had already passed, and report
    ready with it.

    The accounting instant is injected, so the first authorization is not a matter of
    scheduling. The admission instant is not injected, because the whole point is that it is
    read after the waiting is over.
    """
    moment = datetime.now(UTC)
    checkout = await quote(session, shop, expires_at=moment + EXPIRY_WINDOW)

    async with asyncio.timeout(CONCURRENCY_TIMEOUT):
        async with factory() as gate:
            await InventoryReservationRepository(gate).lock_variants(
                merchant_id=shop.merchant_id, variant_ids=[shop.black]
            )
            attempt = asyncio.create_task(prepare_in_new_session(factory, checkout.id, at=moment))
            # Held past the expiry, so the quote lapses while the attempt is blocked.
            assert await still_waiting(attempt)
            await gate.rollback()

        readiness = await attempt

    assert not readiness.ready
    assert (
        CheckoutAuthorizationViolation.CHECKOUT_EXPIRED
        in readiness.authorization.financial.violations
    )
    assert readiness.reservation is None
    assert readiness.admitted_at is not None
    assert readiness.admitted_at >= checkout.expires_at
    async with factory() as reader:
        assert await reservation_count(reader) == 0


async def test_two_concurrent_cancellations_transition_once(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession], shop: Shop
) -> None:
    """Idempotent under contention, not merely on a retry.

    Cancellation decides whether it is the transition from the status it read. Two that both
    read an open checkout would both take it, and the second would move `cancelled_at` and
    append a second event.
    """
    checkout = await quote(session, shop)

    async with asyncio.timeout(CONCURRENCY_TIMEOUT):
        async with factory() as gate:
            await CheckoutRepository(gate).get_for_update(checkout.id)
            attempts: list[asyncio.Task[object]] = [
                asyncio.create_task(cancel_in_new_session(factory, checkout.id)),
                asyncio.create_task(cancel_in_new_session(factory, checkout.id)),
            ]
            assert await still_waiting(*attempts)
            await gate.rollback()

        outcomes = await asyncio.gather(*attempts)

    stamps = {outcome.cancelled_at for outcome in outcomes if isinstance(outcome, CheckoutSession)}
    assert len(stamps) == 1

    async with factory() as reader:
        events = await AuditRepository(reader).list_for_resource(
            resource_type=CHECKOUT_RESOURCE, resource_id=checkout.id
        )
        assert [event.event_type for event in events] == ["checkout.cancelled"]


async def test_two_concurrent_revocations_transition_once(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession], shop: Shop
) -> None:
    """The same contention one level up, for the same reason."""
    async with asyncio.timeout(CONCURRENCY_TIMEOUT):
        async with factory() as gate:
            await MandateRepository(gate).get_for_update(shop.mandate.id)
            attempts: list[asyncio.Task[object]] = [
                asyncio.create_task(revoke_in_new_session(factory, shop.mandate.id)),
                asyncio.create_task(revoke_in_new_session(factory, shop.mandate.id)),
            ]
            assert await still_waiting(*attempts)
            await gate.rollback()

        outcomes = await asyncio.gather(*attempts)

    stamps = {outcome.revoked_at for outcome in outcomes if isinstance(outcome, SpendingMandate)}
    assert len(stamps) == 1

    async with factory() as reader:
        events = await AuditRepository(reader).list_for_resource(
            resource_type=MANDATE_RESOURCE, resource_id=shop.mandate.id
        )
        assert [event.event_type for event in events] == ["mandate.revoked"]


@pytest.fixture
def row_locks(catalog_engine: AsyncEngine) -> Iterator[list[str]]:
    """Every row lock taken on this engine, as the table each one targeted."""
    taken: list[str] = []

    def record(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: object,
    ) -> None:
        if "FOR UPDATE" not in statement:
            return
        target = re.search(r"\bFROM\s+(\w+)", statement)
        if target is not None:
            taken.append(target.group(1))

    event.listen(catalog_engine.sync_engine, "before_cursor_execute", record)
    try:
        yield taken
    finally:
        event.remove(catalog_engine.sync_engine, "before_cursor_execute", record)


def test_the_lock_order_is_one_rule_in_one_place() -> None:
    """The rule the test below asserts against is the one the code documents."""
    assert LOCK_ORDER == (
        "spending_mandate",
        "checkout_session",
        "variant",
        "inventory_reservation",
        "payment_attempt",
    )


def test_a_reversal_is_not_the_documented_order() -> None:
    """The checker has to be capable of failing, or asserting with it proves nothing."""
    assert not respects_lock_order(["checkout_session", "spending_mandate"])
    assert not respects_lock_order(["payment_attempt", "variant"])


async def test_preparation_takes_its_locks_in_the_documented_order(
    session: AsyncSession, shop: Shop, row_locks: list[str]
) -> None:
    """Deadlock freedom is a property of the order, so the order is what is asserted.

    Preparation needs three of the five classes, which makes it one of the operations that
    can put them in the wrong order. What is asserted is that nothing was taken out of order,
    not that everything was taken: no operation needs every class, and requiring that would
    make the rule impossible to satisfy rather than easy to keep.

    Repeats are ignored because the variant rows are locked twice on purpose, once to
    establish the wait and once inside the reservation itself, and taking a lock already held
    is free.
    """
    checkout = await quote(session, shop)
    # Nothing above takes a row lock, but clearing says so rather than assuming it.
    row_locks.clear()

    readiness = await CheckoutExecutionService(session).prepare_execution(checkout.id, at=NOW)

    assert readiness.ready
    assert [table for table, _ in groupby(row_locks)] == [
        "spending_mandate",
        "checkout_session",
        "variant",
    ]
    assert respects_lock_order(row_locks)
