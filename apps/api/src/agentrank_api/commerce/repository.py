"""Persistence access for the commerce catalog.

Repositories own SQLAlchemy and nothing else. They know no HTTP, and they do not commit:
the caller decides transaction boundaries, so several repository calls can form one unit
of work.
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from agentrank_api.commerce.models import Merchant, Product, Variant


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


class CatalogRepository:
    """Products and their variants.

    Relationships are declared `lazy="raise_on_sql"`, so every query here states what it
    loads. That is deliberate: a missing loader option fails loudly instead of turning
    into one extra query per row at serialization time.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_product(
        self,
        *,
        merchant_id: uuid.UUID,
        external_id: str,
        title: str,
        description: str | None = None,
        category: str | None = None,
        is_active: bool = True,
    ) -> Product:
        product = Product(
            merchant_id=merchant_id,
            external_id=external_id,
            title=title,
            description=description,
            category=category,
            is_active=is_active,
        )
        self._session.add(product)
        await self._session.flush()
        return product

    async def create_variant(
        self,
        *,
        product: Product,
        sku: str,
        price_amount_minor: int,
        currency: str,
        label: str | None = None,
        attributes: dict[str, Any] | None = None,
        inventory_quantity: int = 0,
        is_active: bool = True,
    ) -> Variant:
        """Add a variant to a product.

        The product is passed rather than its id so that the merchant is derived from it.
        A caller cannot supply a merchant, and therefore cannot mis-attribute a variant.
        """
        variant = Variant(
            product_id=product.id,
            merchant_id=product.merchant_id,
            sku=sku,
            label=label,
            attributes=attributes if attributes is not None else {},
            price_amount_minor=price_amount_minor,
            currency=currency,
            inventory_quantity=inventory_quantity,
            is_active=is_active,
        )
        self._session.add(variant)
        await self._session.flush()
        return variant

    async def get_product(self, product_id: uuid.UUID) -> Product | None:
        """Fetch one product with its merchant and every variant loaded."""
        statement = (
            select(Product)
            .options(joinedload(Product.merchant), selectinload(Product.variants))
            .where(Product.id == product_id)
        )
        result = await self._session.execute(statement)
        return result.unique().scalar_one_or_none()
