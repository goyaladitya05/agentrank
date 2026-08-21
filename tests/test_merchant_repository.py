"""Merchant persistence behavior against a real PostgreSQL schema."""

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.commerce.repository import MerchantRepository

pytestmark = pytest.mark.anyio


async def test_merchant_persists_and_can_be_retrieved(session: AsyncSession) -> None:
    repository = MerchantRepository(session)

    created = await repository.create(slug="ampere-supply", name="Ampere Supply")
    await session.commit()

    found = await repository.get_by_slug("ampere-supply")
    assert found is not None
    assert found.id == created.id
    assert found.name == "Ampere Supply"
    assert found.created_at is not None
    assert found.created_at.tzinfo is not None


async def test_merchant_slug_is_unique(session: AsyncSession) -> None:
    repository = MerchantRepository(session)
    await repository.create(slug="ampere-supply", name="Ampere Supply")
    await session.commit()

    with pytest.raises(IntegrityError):
        await repository.create(slug="ampere-supply", name="A Different Business")
        await session.commit()


async def test_merchant_updated_at_advances_when_the_row_changes(session: AsyncSession) -> None:
    repository = MerchantRepository(session)
    merchant = await repository.create(slug="ampere-supply", name="Ampere Supply")
    await session.commit()
    original = merchant.updated_at

    merchant.name = "Ampere Supply Company"
    await session.commit()

    assert merchant.updated_at > original
    assert merchant.created_at < merchant.updated_at


async def test_merchant_slug_must_be_url_safe(session: AsyncSession) -> None:
    """The slug reaches URLs and fixture identity, so the database rejects a bad one."""
    repository = MerchantRepository(session)

    with pytest.raises(IntegrityError):
        await repository.create(slug="Ampere Supply", name="Ampere Supply")
        await session.commit()
