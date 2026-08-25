"""Defining a catalog in code, and getting it into the database convergently.

Two callers now: the development catalog a developer seeds to have something real to query,
and the VoltEdge catalog the first benchmark suite is authored against. They are different data
with different purposes and they are the same shape, so the shape lives here rather than being
written twice and drifting.

Seeding is convergent. Running it twice changes nothing, and running it after editing the
definitions updates the existing rows rather than duplicating them. It is never run at
application startup.
"""

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.commerce.repository import CatalogRepository, MerchantRepository


@dataclass(frozen=True, slots=True)
class SeedVariant:
    # `label` is optional because a real merchant catalog has variants nobody named. The column
    # behind it has always been nullable, and this type being narrower than the thing it
    # describes meant an evaluation catalog projected from merchant evidence would have had to
    # invent a label for a variant the merchant left blank.
    sku: str
    label: str | None
    price_amount_minor: int
    currency: str
    inventory_quantity: int
    attributes: dict[str, Any] = field(default_factory=dict)
    is_active: bool = True


@dataclass(frozen=True, slots=True)
class SeedProduct:
    external_id: str
    title: str
    description: str | None
    category: str | None
    variants: tuple[SeedVariant, ...]
    is_active: bool = True


@dataclass(frozen=True, slots=True)
class SeedSummary:
    """What a seeding run did. `created` is zero on every run after the first."""

    merchant_id: uuid.UUID
    products: int
    variants: int
    created: int


async def seed_catalog(
    session: AsyncSession, *, slug: str, name: str, products: tuple[SeedProduct, ...]
) -> SeedSummary:
    """Create or update one merchant and its catalog. Does not commit."""
    merchants = MerchantRepository(session)
    catalog = CatalogRepository(session)
    created = 0

    merchant = await merchants.get_by_slug(slug)
    if merchant is None:
        merchant = await merchants.create(slug=slug, name=name)
        created += 1
    else:
        merchant.name = name

    variant_count = 0
    for definition in products:
        product = await catalog.get_product_by_external_id(merchant.id, definition.external_id)
        if product is None:
            product = await catalog.create_product(
                merchant_id=merchant.id,
                external_id=definition.external_id,
                title=definition.title,
                description=definition.description,
                category=definition.category,
                is_active=definition.is_active,
            )
            created += 1
        else:
            product.title = definition.title
            product.description = definition.description
            product.category = definition.category
            product.is_active = definition.is_active

        for seed_variant in definition.variants:
            variant_count += 1
            variant = await catalog.get_variant_by_sku(merchant.id, seed_variant.sku)
            if variant is None:
                await catalog.create_variant(
                    product=product,
                    sku=seed_variant.sku,
                    label=seed_variant.label,
                    price_amount_minor=seed_variant.price_amount_minor,
                    currency=seed_variant.currency,
                    inventory_quantity=seed_variant.inventory_quantity,
                    attributes=seed_variant.attributes,
                    is_active=seed_variant.is_active,
                )
                created += 1
            else:
                variant.label = seed_variant.label
                variant.price_amount_minor = seed_variant.price_amount_minor
                variant.currency = seed_variant.currency
                variant.inventory_quantity = seed_variant.inventory_quantity
                variant.attributes = seed_variant.attributes
                variant.is_active = seed_variant.is_active

    await session.flush()
    return SeedSummary(
        merchant_id=merchant.id,
        products=len(products),
        variants=variant_count,
        created=created,
    )
