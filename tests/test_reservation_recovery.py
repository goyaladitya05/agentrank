"""Giving stock back on purpose, including from a state that should be unreachable.

Preparation is the only thing that takes a hold on inventory. Until now the only thing that
gave one back was cancelling the quote, which is a decision about the quote rather than about
the stock, so a reservation that should not be held could only be waited out. A claim on a
merchant's inventory that nothing can deliberately end is a claim the system has lost track
of.

Two paths are covered here. `CheckoutExecutionService.release_reservation` is the deliberate
one, with a required machine readable reason. Cancelling an already cancelled checkout is the
healing one, and the state it heals is manufactured here through the repositories, because
the locking added in Phase 1E-R is supposed to make it unreachable through the services.
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from agentrank_api.audit.models import AuditEvent
from agentrank_api.audit.repository import AuditRepository
from agentrank_api.checkout.execution import CheckoutExecutionService
from agentrank_api.checkout.models import CheckoutSession, CheckoutStatus
from agentrank_api.checkout.quote import QuotedLine
from agentrank_api.checkout.repository import CheckoutRepository
from agentrank_api.checkout.service import CHECKOUT_RESOURCE, CheckoutService
from agentrank_api.commerce.repository import CatalogRepository, MerchantRepository
from agentrank_api.database import create_session_factory
from agentrank_api.errors import NotFoundError
from agentrank_api.inventory.models import InventoryReservation, ReservationStatus
from agentrank_api.inventory.repository import InventoryReservationRepository
from agentrank_api.inventory.service import (
    RESERVATION_RESOURCE,
    InventoryReservationService,
    ReleaseReason,
    total_reserved,
)
from agentrank_api.mandates.repository import MandateRepository

pytestmark = pytest.mark.anyio

NOW = datetime.now(UTC)
HOUR = timedelta(hours=1)
PRICE = 499900
RECOVERED = ReleaseReason.RESERVATION_RECOVERED


@dataclass(frozen=True, slots=True)
class Shop:
    merchant_id: uuid.UUID
    mandate_id: uuid.UUID
    variant_id: uuid.UUID


@pytest.fixture
async def shop(session: AsyncSession) -> Shop:
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
    variant = await catalog.create_variant(
        product=product,
        sku="AMP-CHG",
        price_amount_minor=PRICE,
        currency="INR",
        inventory_quantity=1,
    )
    await session.commit()
    return Shop(merchant_id=merchant.id, mandate_id=mandate.id, variant_id=variant.id)


@pytest.fixture
async def committed(catalog_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A second session, for reading what actually reached the database."""
    factory = create_session_factory(catalog_engine)
    async with factory() as other:
        yield other


async def quote(session: AsyncSession, shop: Shop) -> CheckoutSession:
    """A quote written straight through the repository, so it carries no creation event."""
    checkout = await CheckoutRepository(session).create(
        merchant_id=shop.merchant_id,
        mandate_id=shop.mandate_id,
        currency="INR",
        lines=[QuotedLine(variant_id=shop.variant_id, quantity=1, unit_price_amount_minor=PRICE)],
        expires_at=NOW + HOUR,
    )
    await session.commit()
    return checkout


async def hold(session: AsyncSession, checkout: CheckoutSession) -> InventoryReservation:
    outcome = await InventoryReservationService(session).reserve(
        checkout, expires_at=NOW + HOUR, at=NOW
    )
    await session.commit()
    assert outcome.reservation is not None
    return outcome.reservation


async def events_for(session: AsyncSession, reservation_id: uuid.UUID) -> list[AuditEvent]:
    return list(
        await AuditRepository(session).list_for_resource(
            resource_type=RESERVATION_RESOURCE, resource_id=reservation_id
        )
    )


async def strand(session: AsyncSession, shop: Shop) -> tuple[CheckoutSession, InventoryReservation]:
    """A cancelled checkout still holding stock, built through the repositories.

    Unreachable through the services, which is the point of the repair. It is manufactured
    here so that the recovery path has something real to recover, because a recovery that
    only ever runs against a healthy system is a recovery nobody has tested.
    """
    checkout = await quote(session, shop)
    reservation = await hold(session, checkout)
    assert await CheckoutRepository(session).cancel(checkout) is True
    await session.commit()
    return checkout, reservation


async def test_releasing_gives_the_stock_back_and_says_why(
    session: AsyncSession, committed: AsyncSession, shop: Shop
) -> None:
    checkout = await quote(session, shop)
    reservation = await hold(session, checkout)

    released = await CheckoutExecutionService(session).release_reservation(
        checkout.id, reason=RECOVERED
    )

    assert released is True
    repository = InventoryReservationRepository(committed)
    assert await repository.get_holding_for_checkout(checkout.id) is None
    assert total_reserved(await repository.list_for_checkout(checkout.id), at=NOW) == 0

    events = await events_for(committed, reservation.id)
    assert [event.event_type for event in events] == ["inventory.reserved", "inventory.released"]
    assert events[1].payload["reason"] == "reservation_recovered"


async def test_releasing_twice_records_one_event(
    session: AsyncSession, committed: AsyncSession, shop: Shop
) -> None:
    """Idempotent, so a recovery run twice does not double the trail."""
    checkout = await quote(session, shop)
    reservation = await hold(session, checkout)
    service = CheckoutExecutionService(session)

    assert await service.release_reservation(checkout.id, reason=RECOVERED) is True
    released_at = reservation.released_at
    assert await service.release_reservation(checkout.id, reason=RECOVERED) is False

    assert reservation.released_at == released_at
    events = await events_for(committed, reservation.id)
    assert [event.event_type for event in events] == ["inventory.reserved", "inventory.released"]


async def test_releasing_a_checkout_holding_nothing_changes_nothing(
    session: AsyncSession, committed: AsyncSession, shop: Shop
) -> None:
    checkout = await quote(session, shop)

    assert (
        await CheckoutExecutionService(session).release_reservation(checkout.id, reason=RECOVERED)
        is False
    )

    assert await committed.scalar(select(func.count()).select_from(AuditEvent)) == 0


async def test_released_stock_is_available_to_another_checkout(
    session: AsyncSession, shop: Shop
) -> None:
    """The point of recovery, stated as capacity rather than as a status."""
    first, second = await quote(session, shop), await quote(session, shop)
    inventory = InventoryReservationService(session)
    await hold(session, first)
    assert not (await inventory.reserve(second, expires_at=NOW + HOUR, at=NOW)).reserved

    await CheckoutExecutionService(session).release_reservation(first.id, reason=RECOVERED)

    assert (await inventory.reserve(second, expires_at=NOW + HOUR, at=NOW)).reserved
    await session.commit()


async def test_releasing_an_unknown_checkout_is_not_found(session: AsyncSession) -> None:
    with pytest.raises(NotFoundError) as unknown:
        await CheckoutExecutionService(session).release_reservation(uuid.uuid7(), reason=RECOVERED)
    assert unknown.value.resource == "checkout"


async def test_cancelling_again_heals_a_stranded_reservation(
    session: AsyncSession, committed: AsyncSession, shop: Shop
) -> None:
    """The repeat is not a no-op when the state it finds is wrong.

    A cancelled checkout holding stock should not exist. If one does, waiting for it to lapse
    keeps a merchant's inventory off the shelf for a purchase that can no longer happen, so
    the cancellation that is already terminal gives the stock back instead.
    """
    checkout, reservation = await strand(session, shop)
    cancelled_at = checkout.cancelled_at
    assert cancelled_at is not None

    again = await CheckoutService(session).cancel_checkout(checkout.id)

    assert again.status is CheckoutStatus.CANCELLED
    # Terminal stays terminal. Healing is not a second cancellation.
    assert again.cancelled_at == cancelled_at

    healed = await InventoryReservationRepository(committed).get(reservation.id)
    assert healed is not None
    assert healed.status is ReservationStatus.RELEASED

    events = await events_for(committed, reservation.id)
    assert [event.event_type for event in events] == ["inventory.reserved", "inventory.released"]
    # Named as the recovery it is, not as the ordinary lifecycle event it is not.
    assert events[1].payload["reason"] == "reservation_recovered"

    checkout_events = await AuditRepository(committed).list_for_resource(
        resource_type=CHECKOUT_RESOURCE, resource_id=checkout.id
    )
    # The cancellation went through the repository, so there was never a first one to repeat.
    assert checkout_events == []


async def test_healing_twice_heals_once(
    session: AsyncSession, committed: AsyncSession, shop: Shop
) -> None:
    checkout, reservation = await strand(session, shop)
    service = CheckoutService(session)

    await service.cancel_checkout(checkout.id)
    await service.cancel_checkout(checkout.id)

    events = await events_for(committed, reservation.id)
    assert [event.event_type for event in events] == ["inventory.reserved", "inventory.released"]
