"""Persistence access for the commerce catalog.

Repositories own SQLAlchemy and nothing else. They know no HTTP, and they do not commit:
the caller decides transaction boundaries, so several repository calls can form one unit
of work.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.commerce.models import Merchant


class MerchantRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, *, slug: str, name: str) -> Merchant:
        """Add a merchant and flush so that its generated columns are populated."""
        merchant = Merchant(slug=slug, name=name)
        self._session.add(merchant)
        await self._session.flush()
        return merchant

    async def get_by_id(self, merchant_id: uuid.UUID) -> Merchant | None:
        return await self._session.get(Merchant, merchant_id)

    async def get_by_slug(self, slug: str) -> Merchant | None:
        result = await self._session.execute(select(Merchant).where(Merchant.slug == slug))
        return result.scalar_one_or_none()
