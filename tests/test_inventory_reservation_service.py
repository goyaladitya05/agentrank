"""Holding stock, including while something else is trying to hold the same stock.

The concurrency tests are the reason this file exists. Everything else here could be
asserted with one session; two transactions racing for the last unit cannot, and that race
is the correctness problem this phase was authorized to solve. They run against the real
PostgreSQL instance, on independent sessions, genuinely at the same time.

Time is injected everywhere. No test sleeps, because a test that waits for a reservation to
expire is slow and flaky at once.
"""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agentrank_api.checkout.models import CheckoutSession
from agentrank_api.checkout.quote import QuotedLine
from agentrank_api.checkout.repository import CheckoutRepository
from agentrank_api.commerce.models import Variant
from agentrank_api.commerce.repository import CatalogRepository, MerchantRepository
from agentrank_api.database import create_session_factory
from agentrank_api.inventory.repository import (
    InventoryReservationRepository,
    variant_lock_statement,
)
from agentrank_api.inventory.service import (
    InventoryReservationService,
    InventoryViolationCode,
    ReleaseReason,
    ReservationOutcome,
    total_reserved,
)
from agentrank_api.mandates.repository import MandateRepository

pytestmark = pytest.mark.anyio

NOW = datetime.now(UTC)
HOUR = timedelta(hours=1)
TICK = timedelta(microseconds=1)
PRICE = 499900

# A concurrent test that goes wrong blocks on a row lock rather than failing, so every
# gather is bounded. Generous enough never to fire on a healthy database.
CONCURRENCY_TIMEOUT = 30
CANCELLED = ReleaseReason.CHECKOUT_CANCELLED

# How long an attempt is watched before concluding it is genuinely waiting on a lock. This
# is a detection window, not a semantic one: nothing about a reservation depends on it. An
# attempt that is really blocked never completes, so a longer window cannot make these
# tests flaky, and an implementation that took no lock would finish here in milliseconds.
LOCK_WAIT = 1.0


@dataclass(frozen=True, slots=True)
class Shop:
    """One merchant, one mandate, and a catalog with deliberately scarce variants."""

    merchant_id: uuid.UUID
    mandate_id: uuid.UUID
    charger: uuid.UUID
    cable: uuid.UUID


async def build_shop(session: AsyncSession, *, stock: int = 1) -> Shop:
    merchant = await MerchantRepository(session).create(slug="ampere-supply", name="Ampere")
    mandate = await MandateRepository(session).create(
        merchant_id=merchant.id,
        max_total_amount_minor=10_000_000,
        currency="INR",
        valid_from=NOW - HOUR,
        valid_until=NOW + HOUR,
    )
    catalog = CatalogRepository(session)
    product = await catalog.create_product(
        merchant_id=merchant.id, external_id="amp-1", title="Charger", category="chargers"
    )
    charger = await catalog.create_variant(
        product=product,
        sku="AMP-CHG",
        price_amount_minor=PRICE,
        currency="INR",
        inventory_quantity=stock,
    )
    cable = await catalog.create_variant(
        product=product,
        sku="AMP-CBL",
        price_amount_minor=PRICE,
        currency="INR",
        inventory_quantity=stock,
    )
    await session.commit()
    return Shop(merchant_id=merchant.id, mandate_id=mandate.id, charger=charger.id, cable=cable.id)


async def quote(
    session: AsyncSession, shop: Shop, *variants: uuid.UUID, quantity: int = 1
) -> CheckoutSession:
    checkout = await CheckoutRepository(session).create(
        merchant_id=shop.merchant_id,
        mandate_id=shop.mandate_id,
        currency="INR",
        lines=[
            QuotedLine(variant_id=variant, quantity=quantity, unit_price_amount_minor=PRICE)
            for variant in (variants or (shop.charger,))
        ],
        expires_at=NOW + HOUR,
    )
    await session.commit()
    return checkout


@pytest.fixture
async def shop(session: AsyncSession) -> Shop:
    return await build_shop(session)


@pytest.fixture
def factory(catalog_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Independent sessions, so that two reservations can genuinely race."""
    return create_session_factory(catalog_engine)


async def reserve_in_new_session(
    factory: async_sessionmaker[AsyncSession], checkout_id: uuid.UUID
) -> ReservationOutcome:
    """One reservation attempt on its own connection, committed before it returns.

    Committing is what releases the variant locks, so a caller racing two of these observes
    the real handover rather than one transaction sitting on a lock forever.
    """
    async with factory() as session:
        checkout = await CheckoutRepository(session).get(checkout_id)
        assert checkout is not None
        outcome = await InventoryReservationService(session).reserve(
            checkout, expires_at=NOW + HOUR, at=NOW
        )
        await session.commit()
        return outcome


async def test_a_reservation_holds_exactly_what_the_checkout_quoted(
    session: AsyncSession, shop: Shop
) -> None:
    """Quantities come from the immutable quote, never from a caller."""
    checkout = await quote(session, shop)

    outcome = await InventoryReservationService(session).reserve(
        checkout, expires_at=NOW + HOUR, at=NOW
    )
    await session.commit()

    assert outcome.reserved
    assert outcome.created
    assert outcome.reservation is not None
    assert outcome.reservation.checkout_id == checkout.id
    assert {line.variant_id: line.quantity for line in outcome.reservation.lines} == {
        shop.charger: 1
    }


async def test_stock_is_never_decremented(session: AsyncSession, shop: Shop) -> None:
    checkout = await quote(session, shop)
    await InventoryReservationService(session).reserve(checkout, expires_at=NOW + HOUR, at=NOW)
    await session.commit()

    variant = await session.get(Variant, shop.charger)
    assert variant is not None
    assert variant.inventory_quantity == 1


async def test_the_second_checkout_for_the_last_unit_is_refused(
    session: AsyncSession, shop: Shop
) -> None:
    first, second = await quote(session, shop), await quote(session, shop)
    service = InventoryReservationService(session)

    assert (await service.reserve(first, expires_at=NOW + HOUR, at=NOW)).reserved
    outcome = await service.reserve(second, expires_at=NOW + HOUR, at=NOW)
    await session.commit()

    assert not outcome.reserved
    assert outcome.reservation is None
    violation = outcome.violations[0]
    assert violation.code is InventoryViolationCode.INSUFFICIENT_INVENTORY
    assert violation.variant_id == shop.charger
    assert violation.requested_quantity == 1
    assert violation.available_quantity == 0


async def test_a_multi_line_reservation_is_all_or_nothing(
    session: AsyncSession, shop: Shop
) -> None:
    """A charger available and a cable already taken must hold neither."""
    service = InventoryReservationService(session)
    taken = await quote(session, shop, shop.cable)
    assert (await service.reserve(taken, expires_at=NOW + HOUR, at=NOW)).reserved

    both = await quote(session, shop, shop.charger, shop.cable)
    outcome = await service.reserve(both, expires_at=NOW + HOUR, at=NOW)
    await session.commit()

    assert not outcome.reserved
    assert [violation.variant_id for violation in outcome.violations] == [shop.cable]

    repository = InventoryReservationRepository(session)
    assert await repository.get_active_for_checkout(both.id) is None
    # The charger was available and stays available: nothing was held for this checkout.
    assert await repository.effective_reserved_quantities(variant_ids=[shop.charger], at=NOW) == {}


async def test_reserving_the_same_checkout_twice_holds_one_claim(
    session: AsyncSession, shop: Shop
) -> None:
    """Idempotent, and the stock it holds does not double."""
    checkout = await quote(session, shop)
    service = InventoryReservationService(session)

    first = await service.reserve(checkout, expires_at=NOW + HOUR, at=NOW)
    second = await service.reserve(checkout, expires_at=NOW + HOUR, at=NOW)
    await session.commit()

    assert first.created
    assert not second.created
    assert second.reserved
    assert first.reservation is not None
    assert second.reservation is not None
    assert second.reservation.id == first.reservation.id

    held = await InventoryReservationRepository(session).list_for_checkout(checkout.id)
    assert len(held) == 1
    assert total_reserved(held, at=NOW) == 1


async def test_an_expired_reservation_stops_holding_stock(
    session: AsyncSession, shop: Shop
) -> None:
    """The capacity boundary, without a sleep and without a sweeper job.

    The first reservation expires at T. One microsecond before T the last unit is still
    held; at exactly T it is free, because effectiveness is half open in the same direction
    as every other window in this system.
    """
    expiry = NOW + HOUR
    first, second = await quote(session, shop), await quote(session, shop)
    service = InventoryReservationService(session)

    assert (await service.reserve(first, expires_at=expiry, at=NOW)).reserved
    await session.commit()

    just_before = await service.reserve(second, expires_at=expiry + HOUR, at=expiry - TICK)
    assert not just_before.reserved
    assert just_before.violations[0].code is InventoryViolationCode.INSUFFICIENT_INVENTORY

    exactly_at = await service.reserve(second, expires_at=expiry + HOUR, at=expiry)
    await session.commit()
    assert exactly_at.reserved

    # The lapsed row is still there. It stopped counting; it was not deleted.
    held = await InventoryReservationRepository(session).list_for_checkout(first.id)
    assert len(held) == 1
    assert total_reserved(held, at=expiry) == 0


async def test_releasing_gives_the_stock_back(session: AsyncSession, shop: Shop) -> None:
    first, second = await quote(session, shop), await quote(session, shop)
    service = InventoryReservationService(session)

    held = await service.reserve(first, expires_at=NOW + HOUR, at=NOW)
    assert held.reservation is not None
    assert not (await service.reserve(second, expires_at=NOW + HOUR, at=NOW)).reserved

    assert await service.release(held.reservation, reason=CANCELLED) is True
    assert (await service.reserve(second, expires_at=NOW + HOUR, at=NOW)).reserved
    await session.commit()

    # Releasing twice changes nothing and reports that it changed nothing.
    assert await service.release(held.reservation, reason=CANCELLED) is False


async def still_waiting(*attempts: asyncio.Task[ReservationOutcome]) -> bool:
    """Whether every attempt is still blocked, watched for a bounded window.

    Not a sleep that a result depends on. Nothing about a reservation is decided by this
    number: a genuinely blocked attempt never finishes, and an implementation that took no
    lock would be done long before the window closes. It is how a test observes that
    PostgreSQL, rather than luck, is what serialises two writers.
    """
    done, _ = await asyncio.wait(set(attempts), timeout=LOCK_WAIT)
    return not done


async def test_two_concurrent_attempts_on_the_last_unit_resolve_to_one(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession], shop: Shop
) -> None:
    """The race Phase 1C left open, run for real.

    Two checkouts, one unit, two connections. A third transaction holds the variant lock
    while both attempts start, so both are genuinely in flight and queued on the same row
    before either can look at availability. Without that the two would very likely take
    their turns by accident, and a test that passes by accident would pass with no locking
    at all.

    When the gate lets go, PostgreSQL decides it: one attempt takes the row, counts, writes
    and commits, and the other is granted the lock afterwards, reads the reservation that
    now exists and refuses.
    """
    first, second = await quote(session, shop), await quote(session, shop)

    async with asyncio.timeout(CONCURRENCY_TIMEOUT):
        async with factory() as gate:
            await InventoryReservationRepository(gate).lock_variants(
                merchant_id=shop.merchant_id, variant_ids=[shop.charger]
            )
            attempts = [
                asyncio.create_task(reserve_in_new_session(factory, first.id)),
                asyncio.create_task(reserve_in_new_session(factory, second.id)),
            ]
            # Both are inside their transactions and waiting on the row this one holds.
            assert await still_waiting(*attempts)
            await gate.rollback()

        outcomes = await asyncio.gather(*attempts)

    assert sorted(outcome.reserved for outcome in outcomes) == [False, True]
    refused = next(outcome for outcome in outcomes if not outcome.reserved)
    assert refused.violations[0].code is InventoryViolationCode.INSUFFICIENT_INVENTORY
    assert refused.violations[0].available_quantity == 0

    async with factory() as reader:
        repository = InventoryReservationRepository(reader)
        every = [
            *await repository.list_for_checkout(first.id),
            *await repository.list_for_checkout(second.id),
        ]
        assert len(every) == 1
        assert total_reserved(every, at=NOW) == 1


async def test_two_concurrent_multi_variant_attempts_do_not_deadlock(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Deterministic lock order, exercised from both directions at once.

    Each checkout wants both variants and the two quote them in opposite order, and the same
    gate makes both attempts queue before either holds anything. Locks are taken by variant
    identifier rather than by quote order, so when the gate releases they proceed one after
    the other. An implementation that locked in quote order would have each attempt holding
    the row the other needs next, and PostgreSQL would abort one of them as a deadlock
    rather than either of them succeeding.
    """
    shop = await build_shop(session, stock=2)
    forward = await quote(session, shop, shop.charger, shop.cable)
    backward = await quote(session, shop, shop.cable, shop.charger)

    async with asyncio.timeout(CONCURRENCY_TIMEOUT):
        async with factory() as gate:
            await InventoryReservationRepository(gate).lock_variants(
                merchant_id=shop.merchant_id, variant_ids=[shop.charger, shop.cable]
            )
            attempts = [
                asyncio.create_task(reserve_in_new_session(factory, forward.id)),
                asyncio.create_task(reserve_in_new_session(factory, backward.id)),
            ]
            assert await still_waiting(*attempts)
            await gate.rollback()

        outcomes = await asyncio.gather(*attempts)

    assert all(outcome.reserved for outcome in outcomes)


def test_variant_rows_are_locked_in_identifier_order() -> None:
    """The ordering and the lock are in the statement, where the planner has to honour them.

    A Python side sort alone would not survive the planner returning rows in another order,
    and locks are taken in the order rows come back.
    """
    statement = str(
        variant_lock_statement(merchant_id=uuid.uuid7(), variant_ids=[uuid.uuid7(), uuid.uuid7()])
    )

    assert "ORDER BY variant.id" in statement
    assert statement.rstrip().endswith("FOR UPDATE")
