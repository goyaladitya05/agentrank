"""Checkout invariants, asserted against the real schema.

These tests reach the database through the repository and the ORM, but what is under test
is the database. A checkout is the quote a payment will one day be made against, so every
rule protecting it has a test that tries to break it.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.checkout.models import CheckoutLine, CheckoutSession, CheckoutStatus
from agentrank_api.checkout.quote import QuotedLine
from agentrank_api.checkout.repository import CheckoutRepository
from agentrank_api.commerce.models import Variant
from agentrank_api.commerce.repository import CatalogRepository, MerchantRepository
from agentrank_api.mandates.models import SpendingMandate
from agentrank_api.mandates.repository import MandateRepository

pytestmark = pytest.mark.anyio

NOW = datetime.now(UTC)
HOUR = timedelta(hours=1)
PRICE = 499900


@dataclass(frozen=True, slots=True)
class Seller:
    """One merchant with a mandate and a purchasable variant."""

    merchant_id: uuid.UUID
    mandate: SpendingMandate
    variant: Variant


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
    await session.commit()
    return Seller(merchant_id=merchant.id, mandate=mandate, variant=variant)


@pytest.fixture
async def seller(session: AsyncSession) -> Seller:
    return await build_seller(session, "ampere-supply")


async def create_checkout(
    session: AsyncSession, seller: Seller, **overrides: object
) -> CheckoutSession:
    fields: dict[str, object] = {
        "merchant_id": seller.merchant_id,
        "mandate_id": seller.mandate.id,
        "currency": "INR",
        "lines": [
            QuotedLine(variant_id=seller.variant.id, quantity=1, unit_price_amount_minor=PRICE)
        ],
        "expires_at": NOW + HOUR,
    }
    return await CheckoutRepository(session).create(**(fields | overrides))  # type: ignore[arg-type]


async def test_a_checkout_persists_with_its_lines(session: AsyncSession, seller: Seller) -> None:
    created = await create_checkout(
        session,
        seller,
        lines=[QuotedLine(variant_id=seller.variant.id, quantity=3, unit_price_amount_minor=PRICE)],
        shipping_amount_minor=39900,
    )
    await session.commit()
    session.expunge_all()

    found = await CheckoutRepository(session).get(created.id, merchant_id=created.merchant_id)
    assert found is not None
    assert found.merchant_id == seller.merchant_id
    assert found.mandate_id == seller.mandate.id
    assert found.currency == "INR"
    assert found.subtotal_amount_minor == 3 * PRICE
    assert found.shipping_amount_minor == 39900
    assert found.discount_amount_minor == 0
    assert found.total_amount_minor == 3 * PRICE + 39900
    assert found.status is CheckoutStatus.OPEN
    assert found.cancelled_at is None
    assert found.expires_at > found.created_at

    assert len(found.lines) == 1
    line = found.lines[0]
    assert line.variant_id == seller.variant.id
    assert line.quantity == 3
    assert line.unit_price_amount_minor == PRICE
    assert line.currency == "INR"


async def test_a_line_quantity_of_zero_is_rejected(session: AsyncSession, seller: Seller) -> None:
    """Refused by the domain before it is reached, so the constraint is tried directly."""
    checkout = await create_checkout(session, seller)
    await session.commit()

    session.add(
        CheckoutLine(
            checkout_id=checkout.id,
            merchant_id=seller.merchant_id,
            variant_id=seller.variant.id,
            quantity=0,
            unit_price_amount_minor=PRICE,
            currency="INR",
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_a_negative_amount_is_rejected(session: AsyncSession, seller: Seller) -> None:
    session.add(
        CheckoutSession(
            merchant_id=seller.merchant_id,
            mandate_id=seller.mandate.id,
            currency="INR",
            subtotal_amount_minor=-1,
            shipping_amount_minor=0,
            discount_amount_minor=0,
            total_amount_minor=-1,
            status=CheckoutStatus.OPEN,
            expires_at=NOW + HOUR,
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_a_total_that_does_not_match_its_parts_is_rejected(
    session: AsyncSession, seller: Seller
) -> None:
    """total = subtotal + shipping - discount, checked on the row rather than trusted."""
    session.add(
        CheckoutSession(
            merchant_id=seller.merchant_id,
            mandate_id=seller.mandate.id,
            currency="INR",
            subtotal_amount_minor=PRICE,
            shipping_amount_minor=39900,
            discount_amount_minor=0,
            total_amount_minor=PRICE,
            status=CheckoutStatus.OPEN,
            expires_at=NOW + HOUR,
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_an_unknown_status_is_rejected(session: AsyncSession, seller: Seller) -> None:
    """PAID is exactly the value that does not exist yet, so it is the one worth trying.

    The insert is written out rather than made through the ORM. SQLAlchemy refuses an
    unknown status before the statement is sent, which proves the mapping and not the
    schema, and the schema is what has to hold when something else writes the row.
    """
    statement = text(
        "INSERT INTO checkout_session (id, merchant_id, mandate_id, currency,"
        " subtotal_amount_minor, shipping_amount_minor, discount_amount_minor,"
        " total_amount_minor, status, expires_at)"
        " VALUES (:id, :merchant_id, :mandate_id, 'INR', 0, 0, 0, 0, 'PAID', :expires_at)"
    )
    with pytest.raises(IntegrityError):
        await session.execute(
            statement,
            {
                "id": uuid.uuid7(),
                "merchant_id": seller.merchant_id,
                "mandate_id": seller.mandate.id,
                "expires_at": NOW + HOUR,
            },
        )


async def test_a_checkout_cannot_be_created_already_expired(
    session: AsyncSession, seller: Seller
) -> None:
    with pytest.raises(IntegrityError):
        await create_checkout(session, seller, expires_at=NOW - HOUR)


async def test_a_checkout_cannot_name_another_merchants_mandate(
    session: AsyncSession, seller: Seller
) -> None:
    """Structural. The composite foreign key has no matching row to point at."""
    other = await build_seller(session, "volt-mart")

    with pytest.raises(IntegrityError):
        await create_checkout(session, seller, mandate_id=other.mandate.id)


async def test_a_checkout_cannot_contain_another_merchants_variant(
    session: AsyncSession, seller: Seller
) -> None:
    """Structural, for the same reason: the line names one merchant, and both its foreign
    keys have to agree with it."""
    other = await build_seller(session, "volt-mart")

    with pytest.raises(IntegrityError):
        await create_checkout(
            session,
            seller,
            lines=[
                QuotedLine(variant_id=other.variant.id, quantity=1, unit_price_amount_minor=PRICE)
            ],
        )


async def test_a_line_cannot_carry_a_currency_the_checkout_does_not_name(
    session: AsyncSession, seller: Seller
) -> None:
    """The currency is part of the composite foreign key, so the two cannot disagree."""
    checkout = await create_checkout(session, seller)
    await session.commit()

    session.add(
        CheckoutLine(
            checkout_id=checkout.id,
            merchant_id=seller.merchant_id,
            variant_id=seller.variant.id,
            quantity=1,
            unit_price_amount_minor=PRICE,
            currency="EUR",
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_quote_fields_cannot_be_edited(session: AsyncSession, seller: Seller) -> None:
    """Repricing an existing quote is not an edit, it is a new quote."""
    checkout = await create_checkout(session, seller)
    await session.commit()

    checkout.total_amount_minor = 1
    with pytest.raises(DBAPIError, match="immutable"):
        await session.flush()


async def test_a_cancelled_checkout_cannot_be_reopened(
    session: AsyncSession, seller: Seller
) -> None:
    repository = CheckoutRepository(session)
    checkout = await create_checkout(session, seller)
    await repository.cancel(checkout)
    await session.commit()

    checkout.status = CheckoutStatus.OPEN
    checkout.cancelled_at = None
    with pytest.raises(DBAPIError, match="cancelled"):
        await session.flush()


async def test_a_line_cannot_be_edited(session: AsyncSession, seller: Seller) -> None:
    """A line has no lifecycle at all, so the guard refuses every update."""
    checkout = await create_checkout(session, seller)
    await session.commit()

    checkout.lines[0].quantity = 99
    with pytest.raises(DBAPIError, match="immutable"):
        await session.flush()


async def test_one_variant_cannot_appear_on_a_checkout_twice(
    session: AsyncSession, seller: Seller
) -> None:
    """Two lines for one variant are one line with a larger quantity."""
    checkout = await create_checkout(session, seller)
    await session.commit()

    session.add(
        CheckoutLine(
            checkout_id=checkout.id,
            merchant_id=seller.merchant_id,
            variant_id=seller.variant.id,
            quantity=1,
            unit_price_amount_minor=PRICE,
            currency="INR",
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()
