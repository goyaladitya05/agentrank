"""Deterministic catalog search behavior."""

import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.commerce.repository import CatalogRepository, MerchantRepository
from agentrank_api.commerce.search import ProductSearchCriteria
from agentrank_api.commerce.service import CatalogService
from agentrank_api.errors import NotFoundError

pytestmark = pytest.mark.anyio


@dataclass(frozen=True)
class Catalog:
    """Identifiers for the small fixed catalog the search tests query."""

    ampere: uuid.UUID
    voltline: uuid.UUID


@pytest.fixture
async def catalog(session: AsyncSession) -> Catalog:
    """Two merchants, so that isolation is testable, and a handful of products.

    Deliberately varied: two currencies, an inactive product, an inactive variant, and two
    merchants using the same product title.
    """
    merchants = MerchantRepository(session)
    repository = CatalogRepository(session)

    ampere = await merchants.create(slug="ampere-supply", name="Ampere Supply")
    voltline = await merchants.create(slug="voltline-parts", name="Voltline Parts")

    charger = await repository.create_product(
        merchant_id=ampere.id,
        external_id="AMP-CHG-100",
        title="100W USB-C Charger",
        description="Gallium nitride wall charger",
        category="chargers",
    )
    await repository.create_variant(
        product=charger,
        sku="AMP-CHG-100-BLK",
        price_amount_minor=499900,
        currency="INR",
        attributes={"color": "black", "wattage": 100},
        inventory_quantity=12,
    )
    await repository.create_variant(
        product=charger,
        sku="AMP-CHG-100-WHT",
        price_amount_minor=549900,
        currency="INR",
        attributes={"color": "white", "wattage": 100},
        inventory_quantity=3,
    )

    cable = await repository.create_product(
        merchant_id=ampere.id,
        external_id="AMP-CBL-2M",
        title="Braided USB-C Cable",
        category="cables",
    )
    await repository.create_variant(
        product=cable, sku="AMP-CBL-2M-BLK", price_amount_minor=99900, currency="INR"
    )
    await repository.create_variant(
        product=cable,
        sku="AMP-CBL-1M-BLK",
        price_amount_minor=79900,
        currency="INR",
        is_active=False,
    )

    euro = await repository.create_product(
        merchant_id=ampere.id,
        external_id="AMP-CHG-EU",
        title="65W Travel Charger",
        category="chargers",
    )
    await repository.create_variant(
        product=euro, sku="AMP-CHG-EU-BLK", price_amount_minor=4999, currency="EUR"
    )

    retired = await repository.create_product(
        merchant_id=ampere.id,
        external_id="AMP-DCK-OLD",
        title="Legacy Dock",
        category="docks",
        is_active=False,
    )
    await repository.create_variant(
        product=retired, sku="AMP-DCK-OLD-1", price_amount_minor=199900, currency="INR"
    )

    rival = await repository.create_product(
        merchant_id=voltline.id,
        external_id="VLT-CHG-100",
        title="100W USB-C Charger",
        category="chargers",
    )
    await repository.create_variant(
        product=rival, sku="VLT-CHG-100-BLK", price_amount_minor=459900, currency="INR"
    )

    await session.commit()
    session.expunge_all()
    return Catalog(ampere=ampere.id, voltline=voltline.id)


async def test_text_search_finds_the_expected_product(
    session: AsyncSession, catalog: Catalog
) -> None:
    matches = await CatalogService(session).search_products(
        ProductSearchCriteria(merchant_id=catalog.ampere, query="100W charger")
    )

    assert [match.product.title for match in matches] == ["100W USB-C Charger"]


async def test_every_query_token_must_match_somewhere(
    session: AsyncSession, catalog: Catalog
) -> None:
    """Tokens are ANDed across title, description and category."""
    service = CatalogService(session)

    by_description = await service.search_products(
        ProductSearchCriteria(merchant_id=catalog.ampere, query="gallium chargers")
    )
    unmatched = await service.search_products(
        ProductSearchCriteria(merchant_id=catalog.ampere, query="gallium cables")
    )

    assert [match.product.external_id for match in by_description] == ["AMP-CHG-100"]
    assert unmatched == []


async def test_max_price_is_variant_aware(session: AsyncSession, catalog: Catalog) -> None:
    """A product qualifies on a variant, and only the qualifying variants come back."""
    matches = await CatalogService(session).search_products(
        ProductSearchCriteria(
            merchant_id=catalog.ampere,
            query="charger",
            max_price_amount_minor=500000,
            currency="INR",
        )
    )

    assert len(matches) == 1
    assert matches[0].product.external_id == "AMP-CHG-100"
    # The 549900 variant of the same product is over the ceiling and must not appear.
    assert [variant.sku for variant in matches[0].eligible_variants] == ["AMP-CHG-100-BLK"]


async def test_a_product_disappears_when_no_variant_is_cheap_enough(
    session: AsyncSession, catalog: Catalog
) -> None:
    matches = await CatalogService(session).search_products(
        ProductSearchCriteria(
            merchant_id=catalog.ampere, max_price_amount_minor=50000, currency="INR"
        )
    )

    assert matches == []


async def test_currency_filter_excludes_other_currencies(
    session: AsyncSession, catalog: Catalog
) -> None:
    matches = await CatalogService(session).search_products(
        ProductSearchCriteria(merchant_id=catalog.ampere, query="charger", currency="EUR")
    )

    assert [match.product.external_id for match in matches] == ["AMP-CHG-EU"]


async def test_a_price_ceiling_without_a_currency_is_refused() -> None:
    """Comparing an amount without knowing its currency is meaningless, not a default."""
    with pytest.raises(ValueError, match="requires currency"):
        ProductSearchCriteria(merchant_id=uuid.uuid7(), max_price_amount_minor=500000)


async def test_inactive_products_do_not_appear(session: AsyncSession, catalog: Catalog) -> None:
    service = CatalogService(session)

    default = await service.search_products(
        ProductSearchCriteria(merchant_id=catalog.ampere, query="dock")
    )
    requested = await service.search_products(
        ProductSearchCriteria(merchant_id=catalog.ampere, query="dock", include_inactive=True)
    )

    assert default == []
    assert [match.product.external_id for match in requested] == ["AMP-DCK-OLD"]


async def test_inactive_variants_are_not_returned(session: AsyncSession, catalog: Catalog) -> None:
    matches = await CatalogService(session).search_products(
        ProductSearchCriteria(merchant_id=catalog.ampere, query="cable")
    )

    assert len(matches) == 1
    assert [variant.sku for variant in matches[0].eligible_variants] == ["AMP-CBL-2M-BLK"]


async def test_results_are_bounded(session: AsyncSession, catalog: Catalog) -> None:
    matches = await CatalogService(session).search_products(
        ProductSearchCriteria(merchant_id=catalog.ampere, limit=1)
    )

    assert len(matches) == 1


async def test_a_search_never_returns_another_merchants_catalog(
    session: AsyncSession, catalog: Catalog
) -> None:
    """Both merchants sell a product with the identical title."""
    matches = await CatalogService(session).search_products(
        ProductSearchCriteria(merchant_id=catalog.voltline, query="100W USB-C Charger")
    )

    assert [match.product.external_id for match in matches] == ["VLT-CHG-100"]
    assert all(match.product.merchant_id == catalog.voltline for match in matches)


async def test_like_wildcards_in_a_query_are_not_wildcards(
    session: AsyncSession, catalog: Catalog
) -> None:
    """A query of "%" must match nothing, not dump the catalog."""
    matches = await CatalogService(session).search_products(
        ProductSearchCriteria(merchant_id=catalog.ampere, query="%")
    )

    assert matches == []


async def test_results_are_ordered_deterministically(
    session: AsyncSession, catalog: Catalog
) -> None:
    """Two identical searches must return the same rows in the same order."""
    service = CatalogService(session)
    criteria = ProductSearchCriteria(merchant_id=catalog.ampere)

    first = await service.search_products(criteria)
    second = await service.search_products(criteria)

    titles = [match.product.title for match in first]
    assert titles == sorted(titles)
    assert titles == [match.product.title for match in second]


async def test_getting_an_unknown_product_raises_not_found(session: AsyncSession) -> None:
    with pytest.raises(NotFoundError):
        await CatalogService(session).get_product(uuid.uuid7())
