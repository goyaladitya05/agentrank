"""Persistence access for the commerce catalog.

Repositories own SQLAlchemy and nothing else. They know no HTTP, and they do not commit:
the caller decides transaction boundaries, so several repository calls can form one unit
of work.
"""

import uuid
from collections import defaultdict
from collections.abc import Sequence
from typing import Any

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from agentrank_api.commerce.models import Merchant, Product, Variant
from agentrank_api.commerce.search import (
    MAX_SEARCH_LIMIT,
    ProductMatch,
    ProductSearchCriteria,
)


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


LIKE_ESCAPE = "\\"


def _escape_like(term: str) -> str:
    """Neutralise LIKE wildcards in a user supplied term.

    Without this a query of "%" matches every product, and "_" matches any character,
    which turns a search box into a way to dump a catalog.
    """
    return (
        term.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("%", f"{LIKE_ESCAPE}%")
        .replace("_", f"{LIKE_ESCAPE}_")
    )


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

    async def get_product_by_external_id(
        self, merchant_id: uuid.UUID, external_id: str
    ) -> Product | None:
        """Look a product up by the merchant's own identifier for it."""
        statement = select(Product).where(
            Product.merchant_id == merchant_id, Product.external_id == external_id
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get_variant_by_sku(self, merchant_id: uuid.UUID, sku: str) -> Variant | None:
        """Look a variant up by SKU, which is unique within a merchant."""
        statement = select(Variant).where(Variant.merchant_id == merchant_id, Variant.sku == sku)
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get_variants(
        self, *, merchant_id: uuid.UUID, variant_ids: Sequence[uuid.UUID]
    ) -> list[Variant]:
        """Fetch several variants belonging to one merchant, with their products loaded.

        Merchant scope is in the query rather than checked afterwards, so a variant owned
        by someone else is simply absent from the result. The caller decides what a
        missing variant means; from this merchant's point of view there is no difference
        between one that does not exist and one that is not theirs.

        The product comes with it because whether a variant may be quoted depends on
        whether its product is still active, and reading that lazily would raise.
        """
        if not variant_ids:
            return []
        statement = (
            select(Variant)
            .options(joinedload(Variant.product))
            .where(Variant.merchant_id == merchant_id, Variant.id.in_(variant_ids))
        )
        return list((await self._session.execute(statement)).unique().scalars().all())

    async def get_product(self, product_id: uuid.UUID, *, merchant_id: uuid.UUID) -> Product | None:
        """Fetch one merchant's product with its merchant and every variant loaded.

        Merchant scoped, like `get_variants` beside it and like every other resource read in
        this application. Another merchant's product is absent rather than refused, so a
        product identifier is worth nothing to anybody who is not its merchant.

        `selectinload(Product.variants)` loads every variant, active or not, which is one of
        the reasons this read is not public. See docs/architecture.md.
        """
        statement = (
            select(Product)
            .options(joinedload(Product.merchant), selectinload(Product.variants))
            .where(Product.id == product_id, Product.merchant_id == merchant_id)
        )
        result = await self._session.execute(statement)
        return result.unique().scalar_one_or_none()

    def _variant_conditions(self, criteria: ProductSearchCriteria) -> list[ColumnElement[bool]]:
        """Conditions a variant must satisfy to be eligible.

        Used twice: once inside the EXISTS that decides whether a product matches at all,
        and once to select the variants that are returned. Sharing them is what keeps a
        product from being reported as matching on one variant while a different variant
        is shown.
        """
        conditions: list[ColumnElement[bool]] = []
        if not criteria.include_inactive:
            conditions.append(Variant.is_active.is_(True))
        if criteria.currency is not None:
            conditions.append(Variant.currency == criteria.currency)
        if criteria.max_price_amount_minor is not None:
            conditions.append(Variant.price_amount_minor <= criteria.max_price_amount_minor)
        return conditions

    def _product_conditions(self, criteria: ProductSearchCriteria) -> list[ColumnElement[bool]]:
        conditions: list[ColumnElement[bool]] = [Product.merchant_id == criteria.merchant_id]
        if not criteria.include_inactive:
            conditions.append(Product.is_active.is_(True))
        for token in criteria.tokens():
            pattern = f"%{_escape_like(token)}%"
            conditions.append(
                or_(
                    Product.title.ilike(pattern, escape=LIKE_ESCAPE),
                    Product.description.ilike(pattern, escape=LIKE_ESCAPE),
                    Product.category.ilike(pattern, escape=LIKE_ESCAPE),
                )
            )
        return conditions

    async def search_products(self, criteria: ProductSearchCriteria) -> list[ProductMatch]:
        """Find products with at least one variant satisfying every variant level filter.

        Two statements rather than a filtered eager load, so that no half populated
        relationship collection ever escapes this method. `Product.variants` is left
        unloaded on purpose: reading it raises, which is what should happen when a caller
        wants the eligible variants and reaches for the full set by mistake.
        """
        variant_conditions = self._variant_conditions(criteria)
        eligible = (
            select(Variant.id).where(Variant.product_id == Product.id, *variant_conditions).exists()
        )
        limit = min(max(criteria.limit, 1), MAX_SEARCH_LIMIT)

        products = (
            (
                await self._session.execute(
                    select(Product)
                    .options(joinedload(Product.merchant))
                    .where(*self._product_conditions(criteria), eligible)
                    .order_by(Product.title, Product.id)
                    .limit(limit)
                )
            )
            .unique()
            .scalars()
            .all()
        )
        if not products:
            return []

        variants = (
            (
                await self._session.execute(
                    select(Variant)
                    .where(
                        Variant.product_id.in_([product.id for product in products]),
                        *variant_conditions,
                    )
                    .order_by(Variant.price_amount_minor, Variant.sku)
                )
            )
            .scalars()
            .all()
        )

        by_product: dict[uuid.UUID, list[Variant]] = defaultdict(list)
        for variant in variants:
            by_product[variant.product_id].append(variant)

        return [
            ProductMatch(product=product, eligible_variants=tuple(by_product[product.id]))
            for product in products
        ]
