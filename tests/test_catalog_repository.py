"""Catalog repository behavior."""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.commerce.repository import CatalogRepository, MerchantRepository

pytestmark = pytest.mark.anyio


async def test_a_product_is_stored_with_its_variants_and_read_back_loaded(
    session: AsyncSession,
) -> None:
    merchant = await MerchantRepository(session).create(slug="ampere-supply", name="Ampere Supply")
    catalog = CatalogRepository(session)
    product = await catalog.create_product(
        merchant_id=merchant.id,
        external_id="AMP-CHG-100",
        title="100W USB-C Charger",
        description="Gallium nitride wall charger",
        category="chargers",
    )
    await catalog.create_variant(
        product=product,
        sku="AMP-CHG-100-BLK",
        price_amount_minor=499900,
        currency="INR",
        label="Black",
        attributes={"color": "black", "wattage": 100},
        inventory_quantity=12,
    )
    await session.commit()
    session.expunge_all()

    loaded = await catalog.get_product(product.id)

    assert loaded is not None
    assert loaded.title == "100W USB-C Charger"
    assert loaded.merchant.slug == "ampere-supply"
    assert [variant.sku for variant in loaded.variants] == ["AMP-CHG-100-BLK"]
    assert loaded.variants[0].attributes == {"color": "black", "wattage": 100}


async def test_get_product_returns_nothing_for_an_unknown_id(session: AsyncSession) -> None:
    assert await CatalogRepository(session).get_product(uuid.uuid7()) is None


async def test_a_variant_takes_its_merchant_from_its_product(session: AsyncSession) -> None:
    """The caller never supplies a merchant, so a variant cannot be mis-attributed."""
    merchants = MerchantRepository(session)
    owner = await merchants.create(slug="ampere-supply", name="Ampere Supply")
    await merchants.create(slug="voltline-parts", name="Voltline Parts")
    catalog = CatalogRepository(session)
    product = await catalog.create_product(
        merchant_id=owner.id, external_id="AMP-CHG-100", title="100W USB-C Charger"
    )

    variant = await catalog.create_variant(
        product=product, sku="AMP-CHG-100-BLK", price_amount_minor=499900, currency="INR"
    )
    await session.commit()

    assert variant.merchant_id == owner.id


async def test_variants_are_returned_in_a_deterministic_order(session: AsyncSession) -> None:
    merchant = await MerchantRepository(session).create(slug="ampere-supply", name="Ampere Supply")
    catalog = CatalogRepository(session)
    product = await catalog.create_product(
        merchant_id=merchant.id, external_id="AMP-CHG-100", title="100W USB-C Charger"
    )
    for sku, price in [("AMP-C", 300000), ("AMP-A", 100000), ("AMP-B", 100000)]:
        await catalog.create_variant(
            product=product, sku=sku, price_amount_minor=price, currency="INR"
        )
    await session.commit()
    session.expunge_all()

    loaded = await catalog.get_product(product.id)

    assert loaded is not None
    assert [variant.sku for variant in loaded.variants] == ["AMP-A", "AMP-B", "AMP-C"]
