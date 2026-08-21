"""Inventory reservation invariants, asserted against the real schema.

These tests reach the database through the repository and the ORM, but what is under test
is the database. A reservation is the only thing that will stop two buyers from being
prepared against the same last unit, so every rule protecting it has a test that tries to
break it.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.checkout.models import CheckoutSession
from agentrank_api.checkout.quote import QuotedLine
from agentrank_api.checkout.repository import CheckoutRepository
from agentrank_api.commerce.models import Variant
from agentrank_api.commerce.repository import CatalogRepository, MerchantRepository
from agentrank_api.inventory.models import (
    InventoryReservation,
    InventoryReservationLine,
    ReservationStatus,
)
from agentrank_api.inventory.repository import InventoryReservationRepository
from agentrank_api.inventory.rules import is_effective, reservation_expires_at
from agentrank_api.mandates.repository import MandateRepository

pytestmark = pytest.mark.anyio

NOW = datetime.now(UTC)
HOUR = timedelta(hours=1)
PRICE = 499900


@dataclass(frozen=True, slots=True)
class Seller:
    """One merchant with a checkout quoting one variant."""

    merchant_id: uuid.UUID
    variant: Variant
    checkout: CheckoutSession


async def build_seller(session: AsyncSession, slug: str) -> Seller:
    merchant = await MerchantRepository(session).create(slug=slug, name=slug.title())
    mandate = await MandateRepository(session).create(
        merchant_id=merchant.id,
        max_total_amount_minor=1_000_000,
        currency="INR",
        valid_from=NOW,
        valid_until=NOW + HOUR,
    )
    catalog = CatalogRepository(session)
    product = await catalog.create_product(
        merchant_id=merchant.id, external_id=f"{slug}-1", title="Charger"
    )
    variant = await catalog.create_variant(
        product=product,
        sku=f"{slug}-sku",
        price_amount_minor=PRICE,
        currency="INR",
        inventory_quantity=10,
    )
    checkout = await CheckoutRepository(session).create(
        merchant_id=merchant.id,
        mandate_id=mandate.id,
        currency="INR",
        lines=[QuotedLine(variant_id=variant.id, quantity=2, unit_price_amount_minor=PRICE)],
        expires_at=NOW + HOUR,
    )
    await session.commit()
    return Seller(merchant_id=merchant.id, variant=variant, checkout=checkout)


@pytest.fixture
async def seller(session: AsyncSession) -> Seller:
    return await build_seller(session, "ampere-supply")


async def reserve(
    session: AsyncSession, seller: Seller, **overrides: object
) -> InventoryReservation:
    fields: dict[str, object] = {
        "merchant_id": seller.merchant_id,
        "checkout_id": seller.checkout.id,
        "expires_at": NOW + HOUR,
        "quantities": {seller.variant.id: 2},
    }
    return await InventoryReservationRepository(session).create(**(fields | overrides))  # type: ignore[arg-type]


async def test_a_reservation_persists_with_its_lines(session: AsyncSession, seller: Seller) -> None:
    created = await reserve(session, seller)
    await session.commit()
    session.expunge_all()

    found = await InventoryReservationRepository(session).get(created.id)
    assert found is not None
    assert found.merchant_id == seller.merchant_id
    assert found.checkout_id == seller.checkout.id
    assert found.status is ReservationStatus.ACTIVE
    assert found.released_at is None
    assert found.expires_at > found.created_at
    assert found.total_quantity == 2

    assert len(found.lines) == 1
    assert found.lines[0].variant_id == seller.variant.id
    assert found.lines[0].quantity == 2


async def test_reserving_does_not_move_the_variants_stock(
    session: AsyncSession, seller: Seller
) -> None:
    """Inventory is accounted for, never decremented. The variant stays authoritative."""
    await reserve(session, seller)
    await session.commit()

    stored = await session.get(Variant, seller.variant.id)
    assert stored is not None
    assert stored.inventory_quantity == 10


async def test_a_reservation_cannot_be_created_already_expired(
    session: AsyncSession, seller: Seller
) -> None:
    with pytest.raises(IntegrityError):
        await reserve(session, seller, expires_at=NOW - HOUR)


async def test_a_checkout_cannot_hold_two_active_reservations(
    session: AsyncSession, seller: Seller
) -> None:
    """The partial unique index. Reserved stock cannot be counted twice for one quote."""
    await reserve(session, seller)
    await session.commit()

    with pytest.raises(IntegrityError):
        await reserve(session, seller)


async def test_releasing_frees_the_slot_for_a_later_reservation(
    session: AsyncSession, seller: Seller
) -> None:
    """History is kept rather than rewritten: the released row stays, a new row is written."""
    repository = InventoryReservationRepository(session)
    first = await reserve(session, seller)
    await session.commit()

    assert await repository.release(first) is True
    second = await reserve(session, seller)
    await session.commit()

    assert first.status is ReservationStatus.RELEASED
    assert first.released_at is not None
    assert second.id != first.id
    assert [row.id for row in await repository.list_for_checkout(seller.checkout.id)] == [
        first.id,
        second.id,
    ]


async def test_releasing_twice_moves_nothing(session: AsyncSession, seller: Seller) -> None:
    repository = InventoryReservationRepository(session)
    reservation = await reserve(session, seller)
    await session.commit()

    assert await repository.release(reservation) is True
    released_at = reservation.released_at

    assert await repository.release(reservation) is False
    assert reservation.released_at == released_at


async def test_a_released_reservation_cannot_be_reactivated(
    session: AsyncSession, seller: Seller
) -> None:
    reservation = await reserve(session, seller)
    await InventoryReservationRepository(session).release(reservation)
    await session.commit()

    reservation.status = ReservationStatus.ACTIVE
    reservation.released_at = None
    with pytest.raises(DBAPIError, match="released"):
        await session.flush()


async def test_ownership_and_expiry_cannot_be_edited(session: AsyncSession, seller: Seller) -> None:
    """A reservation whose expiry could be pushed out would hold stock indefinitely."""
    reservation = await reserve(session, seller)
    await session.commit()

    reservation.expires_at = NOW + 12 * HOUR
    with pytest.raises(DBAPIError, match="immutable"):
        await session.flush()


async def test_a_reservation_line_cannot_be_edited(session: AsyncSession, seller: Seller) -> None:
    """A line has no lifecycle at all, so the guard refuses every update."""
    reservation = await reserve(session, seller)
    await session.commit()

    reservation.lines[0].quantity = 99
    with pytest.raises(DBAPIError, match="immutable"):
        await session.flush()


async def test_a_reservation_cannot_name_another_merchants_checkout(
    session: AsyncSession, seller: Seller
) -> None:
    """Structural. The composite foreign key has no matching row to point at."""
    other = await build_seller(session, "volt-mart")

    with pytest.raises(IntegrityError):
        await reserve(session, seller, checkout_id=other.checkout.id)


async def test_a_reservation_cannot_hold_another_merchants_stock(
    session: AsyncSession, seller: Seller
) -> None:
    """Structural, for the same reason: the line names one merchant, and both its foreign
    keys have to agree with it."""
    other = await build_seller(session, "volt-mart")

    with pytest.raises(IntegrityError):
        await reserve(session, seller, quantities={other.variant.id: 1})


async def test_a_zero_quantity_line_is_rejected(session: AsyncSession, seller: Seller) -> None:
    reservation = await reserve(session, seller)
    await session.commit()

    session.add(
        InventoryReservationLine(
            reservation_id=reservation.id,
            merchant_id=seller.merchant_id,
            variant_id=seller.variant.id,
            quantity=0,
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_an_unknown_status_is_rejected(session: AsyncSession, seller: Seller) -> None:
    """CONSUMED is exactly the value a payment phase might add, so it is the one to try.

    Written as SQL rather than through the ORM. SQLAlchemy refuses an unknown status before
    the statement is sent, which proves the mapping and not the schema, and the schema is
    what has to hold when something else writes the row.
    """
    statement = text(
        "INSERT INTO inventory_reservation (id, merchant_id, checkout_id, status, expires_at)"
        " VALUES (:id, :merchant_id, :checkout_id, 'CONSUMED', :expires_at)"
    )
    with pytest.raises(IntegrityError):
        await session.execute(
            statement,
            {
                "id": uuid.uuid7(),
                "merchant_id": seller.merchant_id,
                "checkout_id": seller.checkout.id,
                "expires_at": NOW + HOUR,
            },
        )


def test_expiry_is_the_earlier_of_the_checkout_and_the_mandate() -> None:
    """Server derived, and never a window of its own."""
    assert reservation_expires_at(NOW + HOUR, NOW + 2 * HOUR) == NOW + HOUR
    assert reservation_expires_at(NOW + 2 * HOUR, NOW + HOUR) == NOW + HOUR

    with pytest.raises(ValueError, match="timezone aware"):
        reservation_expires_at(NOW.replace(tzinfo=None), NOW + HOUR)


def test_a_reservation_stops_being_effective_exactly_at_its_expiry() -> None:
    """Half open, like a mandate window and a checkout expiry."""
    reservation = InventoryReservation(
        merchant_id=uuid.uuid7(),
        checkout_id=uuid.uuid7(),
        status=ReservationStatus.ACTIVE,
        expires_at=NOW + HOUR,
    )

    assert is_effective(reservation, at=NOW + HOUR - timedelta(microseconds=1))
    assert not is_effective(reservation, at=NOW + HOUR)

    reservation.status = ReservationStatus.RELEASED
    assert not is_effective(reservation, at=NOW)
