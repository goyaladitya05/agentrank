"""The development catalog must be safe to run repeatedly."""

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.commerce.dev_catalog import seed_dev_catalog
from agentrank_api.commerce.models import Variant
from agentrank_api.commerce.search import ProductSearchCriteria
from agentrank_api.commerce.service import CatalogService

pytestmark = pytest.mark.anyio


async def variant_count(session: AsyncSession) -> int:
    return (await session.execute(select(func.count()).select_from(Variant))).scalar_one()


async def test_seeding_twice_creates_nothing_the_second_time(session: AsyncSession) -> None:
    first = await seed_dev_catalog(session)
    await session.commit()
    after_first = await variant_count(session)

    second = await seed_dev_catalog(session)
    await session.commit()

    assert first.created > 0
    assert second.created == 0
    assert second.merchant_id == first.merchant_id
    assert await variant_count(session) == after_first


async def test_the_seeded_catalog_is_searchable(session: AsyncSession) -> None:
    """A sanity check that the fixture and the search semantics agree."""
    summary = await seed_dev_catalog(session)
    await session.commit()
    session.expunge_all()

    matches = await CatalogService(session).search_products(
        ProductSearchCriteria(
            merchant_id=summary.merchant_id,
            query="charger",
            max_price_amount_minor=500000,
            currency="INR",
        )
    )

    # Ordered by title, so "100W GaN USB-C Charger" precedes "65W Travel Charger".
    assert [match.product.external_id for match in matches] == ["AMP-CHG-100", "AMP-CHG-065"]
    # The inactive refurbished variant is under the ceiling but must not be offered.
    offered = {variant.sku for match in matches for variant in match.eligible_variants}
    assert "AMP-CHG-100-REF" not in offered
    assert "AMP-CHG-100-BLK" in offered
