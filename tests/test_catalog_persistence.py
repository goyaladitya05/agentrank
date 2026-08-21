"""Schema level guarantees for the catalog, asserted against real PostgreSQL DDL.

These tests use the ORM directly rather than the repository. What is under test is the
database: the constraints have to hold even if application code is wrong or bypassed.
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from agentrank_api.commerce.models import Merchant, Product, Variant

pytestmark = pytest.mark.anyio


async def make_merchant(session: AsyncSession, slug: str, name: str) -> Merchant:
    merchant = Merchant(slug=slug, name=name)
    session.add(merchant)
    await session.flush()
    return merchant


async def make_product(session: AsyncSession, merchant: Merchant, external_id: str) -> Product:
    product = Product(
        merchant_id=merchant.id,
        external_id=external_id,
        title=f"Product {external_id}",
    )
    session.add(product)
    await session.flush()
    return product


def build_variant(product: Product, sku: str, **overrides: object) -> Variant:
    values: dict[str, object] = {
        "product_id": product.id,
        "merchant_id": product.merchant_id,
        "sku": sku,
        "price_amount_minor": 499900,
        "currency": "INR",
        "inventory_quantity": 5,
    }
    values.update(overrides)
    return Variant(**values)


async def test_product_and_variants_belong_to_their_merchant(session: AsyncSession) -> None:
    merchant = await make_merchant(session, "ampere-supply", "Ampere Supply")
    product = await make_product(session, merchant, "AMP-CHG-100")
    session.add(build_variant(product, "AMP-CHG-100-BLK"))
    await session.commit()

    loaded = (
        await session.execute(
            select(Product).options(selectinload(Product.variants)).where(Product.id == product.id)
        )
    ).scalar_one()

    assert loaded.merchant_id == merchant.id
    assert [variant.merchant_id for variant in loaded.variants] == [merchant.id]


async def test_deleting_a_merchant_removes_its_catalog(session: AsyncSession) -> None:
    merchant = await make_merchant(session, "ampere-supply", "Ampere Supply")
    product = await make_product(session, merchant, "AMP-CHG-100")
    session.add(build_variant(product, "AMP-CHG-100-BLK"))
    await session.commit()

    await session.delete(merchant)
    await session.commit()

    remaining = (await session.execute(select(Variant))).scalars().all()
    assert remaining == []


async def test_external_id_is_unique_within_a_merchant(session: AsyncSession) -> None:
    merchant = await make_merchant(session, "ampere-supply", "Ampere Supply")
    await make_product(session, merchant, "AMP-CHG-100")
    await session.commit()

    with pytest.raises(IntegrityError):
        await make_product(session, merchant, "AMP-CHG-100")
        await session.commit()


async def test_two_merchants_may_use_the_same_external_id(session: AsyncSession) -> None:
    """Merchants number their own catalogs. Collisions across merchants are expected."""
    first = await make_merchant(session, "ampere-supply", "Ampere Supply")
    second = await make_merchant(session, "voltline-parts", "Voltline Parts")

    await make_product(session, first, "CHG-100")
    await make_product(session, second, "CHG-100")
    await session.commit()

    products = (await session.execute(select(Product))).scalars().all()
    assert len(products) == 2


async def test_sku_is_unique_within_a_merchant(session: AsyncSession) -> None:
    merchant = await make_merchant(session, "ampere-supply", "Ampere Supply")
    first = await make_product(session, merchant, "AMP-CHG-100")
    second = await make_product(session, merchant, "AMP-CBL-200")
    session.add(build_variant(first, "AMP-SHARED"))
    await session.commit()

    with pytest.raises(IntegrityError):
        session.add(build_variant(second, "AMP-SHARED"))
        await session.commit()


async def test_a_variant_cannot_be_attributed_to_another_merchant(session: AsyncSession) -> None:
    """Merchant isolation is structural, not a convention the application must remember."""
    owner = await make_merchant(session, "ampere-supply", "Ampere Supply")
    outsider = await make_merchant(session, "voltline-parts", "Voltline Parts")
    product = await make_product(session, owner, "AMP-CHG-100")
    await session.commit()

    with pytest.raises(IntegrityError):
        session.add(build_variant(product, "AMP-CHG-100-BLK", merchant_id=outsider.id))
        await session.commit()


async def test_a_negative_price_is_rejected_by_the_database(session: AsyncSession) -> None:
    merchant = await make_merchant(session, "ampere-supply", "Ampere Supply")
    product = await make_product(session, merchant, "AMP-CHG-100")
    await session.commit()

    with pytest.raises(IntegrityError):
        session.add(build_variant(product, "AMP-NEG", price_amount_minor=-1))
        await session.commit()


async def test_negative_inventory_is_rejected_by_the_database(session: AsyncSession) -> None:
    merchant = await make_merchant(session, "ampere-supply", "Ampere Supply")
    product = await make_product(session, merchant, "AMP-CHG-100")
    await session.commit()

    with pytest.raises(IntegrityError):
        session.add(build_variant(product, "AMP-NEG", inventory_quantity=-1))
        await session.commit()


async def test_currency_must_be_an_uppercase_iso_code(session: AsyncSession) -> None:
    """An amount is meaningless without a currency, so the currency cannot be junk.

    Lowercase is the interesting case: it is the right length, so only the check
    constraint rejects it.
    """
    merchant = await make_merchant(session, "ampere-supply", "Ampere Supply")
    product = await make_product(session, merchant, "AMP-CHG-100")
    await session.commit()

    with pytest.raises(IntegrityError):
        session.add(build_variant(product, "AMP-BAD", currency="inr"))
        await session.commit()


async def test_variant_attributes_round_trip_through_jsonb(session: AsyncSession) -> None:
    merchant = await make_merchant(session, "ampere-supply", "Ampere Supply")
    product = await make_product(session, merchant, "AMP-CHG-100")
    attributes = {
        "color": "black",
        "wattage": 100,
        "connector": "USB-C",
        "fast_charge": True,
        "ports": ["USB-C", "USB-A"],
        "dimensions": {"width_mm": 62, "height_mm": 30},
    }
    session.add(build_variant(product, "AMP-CHG-100-BLK", attributes=attributes))
    await session.commit()

    stored = (
        await session.execute(select(Variant.attributes).where(Variant.sku == "AMP-CHG-100-BLK"))
    ).scalar_one()

    assert stored == attributes


async def test_variant_attributes_must_be_a_json_object(session: AsyncSession) -> None:
    merchant = await make_merchant(session, "ampere-supply", "Ampere Supply")
    product = await make_product(session, merchant, "AMP-CHG-100")
    await session.commit()

    with pytest.raises(IntegrityError):
        session.add(build_variant(product, "AMP-LIST", attributes=["not", "an", "object"]))
        await session.commit()


async def test_a_variant_needs_an_existing_product(session: AsyncSession) -> None:
    merchant = await make_merchant(session, "ampere-supply", "Ampere Supply")
    await session.commit()

    orphan = Variant(
        product_id=uuid.uuid7(),
        merchant_id=merchant.id,
        sku="AMP-ORPHAN",
        price_amount_minor=1000,
        currency="INR",
    )
    with pytest.raises(IntegrityError):
        session.add(orphan)
        await session.commit()
