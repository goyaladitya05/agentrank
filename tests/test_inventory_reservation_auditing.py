"""Holding and releasing stock leave a trail, and neither happens without one.

What is under test is the transaction boundary as much as the payloads. Stock held with no
record of it being held is exactly what an audit trail exists to prevent, so the atomicity
tests here make the audit append fail and then look for rows that should not exist.

The cancellation test is the integration one. A withdrawn quote that still held inventory
would keep it off the shelf until it expired, for a purchase that can no longer happen.
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from agentrank_api.audit.models import ActorType, AuditEvent
from agentrank_api.audit.repository import AuditRepository
from agentrank_api.checkout.models import CheckoutSession, CheckoutStatus
from agentrank_api.checkout.quote import QuotedLine
from agentrank_api.checkout.repository import CheckoutRepository
from agentrank_api.checkout.service import CHECKOUT_RESOURCE, CheckoutService
from agentrank_api.commerce.repository import CatalogRepository, MerchantRepository
from agentrank_api.database import create_session_factory
from agentrank_api.inventory.models import InventoryReservation, InventoryReservationLine
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
CANCELLED = ReleaseReason.CHECKOUT_CANCELLED


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
    """A quote written straight through the repository.

    Deliberately not through `CheckoutService`, so the only events these tests see are the
    ones the code under test appends. A checkout created this way has no `checkout.created`
    event, which is what makes the event lists below readable.
    """
    checkout = await CheckoutRepository(session).create(
        merchant_id=shop.merchant_id,
        mandate_id=shop.mandate_id,
        currency="INR",
        lines=[QuotedLine(variant_id=shop.variant_id, quantity=1, unit_price_amount_minor=PRICE)],
        expires_at=NOW + HOUR,
    )
    await session.commit()
    return checkout


async def events_for(session: AsyncSession, reservation_id: uuid.UUID) -> list[AuditEvent]:
    return list(
        await AuditRepository(session).list_for_resource(
            resource_type=RESERVATION_RESOURCE, resource_id=reservation_id
        )
    )


async def test_holding_stock_records_what_was_held(
    session: AsyncSession, committed: AsyncSession, shop: Shop
) -> None:
    checkout = await quote(session, shop)
    outcome = await InventoryReservationService(session).reserve(
        checkout, expires_at=NOW + HOUR, at=NOW
    )
    await session.commit()
    assert outcome.reservation is not None

    events = await events_for(committed, outcome.reservation.id)
    assert [event.event_type for event in events] == ["inventory.reserved"]
    assert events[0].actor_type is ActorType.BUYER
    assert events[0].merchant_id == shop.merchant_id
    assert events[0].payload["checkout_id"] == str(checkout.id)
    assert events[0].payload["total_quantity"] == 1
    assert events[0].payload["line_count"] == 1
    assert events[0].payload["lines"] == [{"variant_id": str(shop.variant_id), "quantity": 1}]


async def test_nothing_is_held_when_the_audit_append_fails(
    session: AsyncSession,
    committed: AsyncSession,
    shop: Shop,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The transaction boundary, asserted rather than assumed.

    The reservation and its lines are written and flushed before the event, so if the two
    were not one transaction the rows would survive this.
    """
    checkout = await quote(session, shop)
    # Read before the rollback below, which expires every loaded object.
    checkout_id = checkout.id

    async def refuse(*args: object, **kwargs: object) -> None:
        raise RuntimeError("audit is unavailable")

    monkeypatch.setattr(AuditRepository, "append", refuse)

    with pytest.raises(RuntimeError, match="audit is unavailable"):
        await InventoryReservationService(session).reserve(checkout, expires_at=NOW + HOUR, at=NOW)
    await session.rollback()

    # The reader can see committed rows, so an empty count below means nothing was
    # committed rather than that this session cannot see anything.
    assert await committed.get(CheckoutSession, checkout_id) is not None
    assert await committed.scalar(select(func.count()).select_from(InventoryReservation)) == 0
    assert await committed.scalar(select(func.count()).select_from(InventoryReservationLine)) == 0


async def test_holding_the_same_stock_twice_records_one_event(
    session: AsyncSession, committed: AsyncSession, shop: Shop
) -> None:
    """Idempotent preparation is idempotent in the trail too."""
    checkout = await quote(session, shop)
    service = InventoryReservationService(session)

    first = await service.reserve(checkout, expires_at=NOW + HOUR, at=NOW)
    await service.reserve(checkout, expires_at=NOW + HOUR, at=NOW)
    await session.commit()
    assert first.reservation is not None

    assert [event.event_type for event in await events_for(committed, first.reservation.id)] == [
        "inventory.reserved"
    ]


async def test_releasing_records_one_event_and_repeating_records_none(
    session: AsyncSession, committed: AsyncSession, shop: Shop
) -> None:
    checkout = await quote(session, shop)
    service = InventoryReservationService(session)
    held = await service.reserve(checkout, expires_at=NOW + HOUR, at=NOW)
    assert held.reservation is not None

    assert await service.release(held.reservation, reason=CANCELLED) is True
    released_at = held.reservation.released_at
    assert await service.release(held.reservation, reason=CANCELLED) is False
    await session.commit()

    assert held.reservation.released_at == released_at
    events = await events_for(committed, held.reservation.id)
    assert [event.event_type for event in events] == ["inventory.reserved", "inventory.released"]
    assert events[1].payload["reason"] == "checkout_cancelled"
    assert events[1].payload["total_quantity"] == 1
    # The release and the event recording it share the transaction clock.
    assert events[1].occurred_at == released_at


async def test_cancelling_a_checkout_releases_the_stock_it_was_holding(
    session: AsyncSession, committed: AsyncSession, shop: Shop
) -> None:
    """The integration that matters: a withdrawn quote must not keep stock off the shelf."""
    checkout = await quote(session, shop)
    held = await InventoryReservationService(session).reserve(
        checkout, expires_at=NOW + HOUR, at=NOW
    )
    await session.commit()
    assert held.reservation is not None

    cancelled = await CheckoutService(session).cancel_checkout(
        checkout.id, merchant_id=checkout.merchant_id
    )
    assert cancelled.status is CheckoutStatus.CANCELLED

    repository = InventoryReservationRepository(committed)
    assert await repository.get_holding_for_checkout(checkout.id) is None
    assert total_reserved(await repository.list_for_checkout(checkout.id), at=NOW) == 0

    checkout_events = await AuditRepository(committed).list_for_resource(
        resource_type=CHECKOUT_RESOURCE, resource_id=checkout.id
    )
    assert checkout_events
    assert [event.event_type for event in checkout_events] == ["checkout.cancelled"]
    reservation_events = await events_for(committed, held.reservation.id)
    assert [event.event_type for event in reservation_events] == [
        "inventory.reserved",
        "inventory.released",
    ]
    # One transaction: the cancellation and the release carry the same instant.
    assert reservation_events[1].occurred_at == checkout_events[0].occurred_at
    assert reservation_events[1].occurred_at == cancelled.cancelled_at


async def test_cancelling_twice_releases_and_records_once(
    session: AsyncSession, committed: AsyncSession, shop: Shop
) -> None:
    checkout = await quote(session, shop)
    held = await InventoryReservationService(session).reserve(
        checkout, expires_at=NOW + HOUR, at=NOW
    )
    await session.commit()
    assert held.reservation is not None

    service = CheckoutService(session)
    first = await service.cancel_checkout(checkout.id, merchant_id=checkout.merchant_id)
    await service.cancel_checkout(checkout.id, merchant_id=checkout.merchant_id)

    assert first.cancelled_at is not None
    events = await events_for(committed, held.reservation.id)
    assert [event.event_type for event in events] == ["inventory.reserved", "inventory.released"]


async def test_cancelling_a_checkout_holding_nothing_records_nothing_extra(
    session: AsyncSession, committed: AsyncSession, shop: Shop
) -> None:
    checkout = await quote(session, shop)

    await CheckoutService(session).cancel_checkout(checkout.id, merchant_id=checkout.merchant_id)

    assert await committed.scalar(select(func.count()).select_from(InventoryReservation)) == 0
    events = await AuditRepository(committed).list_for_merchant(shop.merchant_id)
    assert [event.event_type for event in events] == ["checkout.cancelled"]


async def test_cancelling_frees_the_unit_for_another_checkout(
    session: AsyncSession, shop: Shop
) -> None:
    """Capacity, end to end: the released unit is really available again."""
    first, second = await quote(session, shop), await quote(session, shop)
    service = InventoryReservationService(session)

    assert (await service.reserve(first, expires_at=NOW + HOUR, at=NOW)).reserved
    assert not (await service.reserve(second, expires_at=NOW + HOUR, at=NOW)).reserved
    await session.commit()

    await CheckoutService(session).cancel_checkout(first.id, merchant_id=first.merchant_id)

    assert (await service.reserve(second, expires_at=NOW + HOUR, at=NOW)).reserved
    await session.commit()
