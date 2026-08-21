"""A small deterministic catalog for local development.

Not benchmark data. This exists so that a developer can start PostgreSQL, run one command
and have something real to query. The eventual benchmark merchant is a separate concern
and will be built in a later phase.

Seeding is idempotent and convergent: running it twice changes nothing, and running it
after editing the definitions below updates the existing rows rather than duplicating
them. It is never run at application startup.

The data is deliberately imperfect. One product has no description, one has no category,
one product is inactive, one variant is inactive, one variant is out of stock and one is
priced in a second currency. A catalog where everything is present and tidy would not
exercise anything AgentRank cares about.
"""

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.commerce.repository import CatalogRepository, MerchantRepository


@dataclass(frozen=True, slots=True)
class SeedVariant:
    sku: str
    label: str
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


MERCHANT_SLUG = "ampere-supply"
MERCHANT_NAME = "Ampere Supply"

PRODUCTS: tuple[SeedProduct, ...] = (
    SeedProduct(
        external_id="AMP-CHG-100",
        title="100W GaN USB-C Charger",
        description="Two port gallium nitride wall charger with foldable pins",
        category="chargers",
        variants=(
            SeedVariant(
                sku="AMP-CHG-100-BLK",
                label="Black",
                price_amount_minor=499900,
                currency="INR",
                inventory_quantity=24,
                attributes={"color": "black", "wattage": 100, "connector": "USB-C", "ports": 2},
            ),
            SeedVariant(
                sku="AMP-CHG-100-WHT",
                label="White",
                price_amount_minor=519900,
                currency="INR",
                inventory_quantity=6,
                attributes={"color": "white", "wattage": 100, "connector": "USB-C", "ports": 2},
            ),
            SeedVariant(
                sku="AMP-CHG-100-REF",
                label="Black, refurbished",
                price_amount_minor=399900,
                currency="INR",
                inventory_quantity=0,
                attributes={"color": "black", "wattage": 100, "refurbished": True},
                is_active=False,
            ),
        ),
    ),
    SeedProduct(
        external_id="AMP-CHG-065",
        title="65W Travel Charger",
        # No description on purpose: merchant catalogs are routinely incomplete.
        description=None,
        category="chargers",
        variants=(
            SeedVariant(
                sku="AMP-CHG-065-BLK",
                label="Black",
                price_amount_minor=299900,
                currency="INR",
                inventory_quantity=40,
                attributes={"color": "black", "wattage": 65, "connector": "USB-C", "ports": 1},
            ),
        ),
    ),
    SeedProduct(
        external_id="AMP-CBL-USBC",
        title="Braided USB-C to USB-C Cable",
        description="240W rated braided cable with aluminium housings",
        category="cables",
        variants=(
            SeedVariant(
                sku="AMP-CBL-USBC-1M",
                label="1 m",
                price_amount_minor=89900,
                currency="INR",
                inventory_quantity=120,
                attributes={"length_m": 1, "connector": "USB-C", "wattage": 240},
            ),
            SeedVariant(
                sku="AMP-CBL-USBC-2M",
                label="2 m",
                price_amount_minor=109900,
                currency="INR",
                inventory_quantity=80,
                attributes={"length_m": 2, "connector": "USB-C", "wattage": 240},
            ),
            SeedVariant(
                sku="AMP-CBL-USBC-3M",
                label="3 m",
                price_amount_minor=129900,
                currency="INR",
                # Active but out of stock, which is a different thing from inactive.
                inventory_quantity=0,
                attributes={"length_m": 3, "connector": "USB-C", "wattage": 240},
            ),
        ),
    ),
    SeedProduct(
        external_id="AMP-HUB-7P",
        title="7 Port USB-C Hub",
        description="Powered hub with HDMI, card reader and gigabit ethernet",
        # No category on purpose.
        category=None,
        variants=(
            SeedVariant(
                sku="AMP-HUB-7P-GRY",
                label="Space grey",
                price_amount_minor=749900,
                currency="INR",
                inventory_quantity=12,
                attributes={"color": "grey", "ports": 7, "connector": "USB-C", "hdmi": True},
            ),
            SeedVariant(
                sku="AMP-HUB-7P-EU",
                label="Space grey, EU pricing",
                price_amount_minor=8999,
                currency="EUR",
                inventory_quantity=5,
                attributes={"color": "grey", "ports": 7, "connector": "USB-C", "hdmi": True},
            ),
        ),
    ),
    SeedProduct(
        external_id="AMP-DCK-LEGACY",
        title="Legacy Mini Dock",
        description="Discontinued micro USB dock, retained for catalog history",
        category="docks",
        is_active=False,
        variants=(
            SeedVariant(
                sku="AMP-DCK-LEGACY-1",
                label="Black",
                price_amount_minor=349900,
                currency="INR",
                inventory_quantity=2,
                attributes={"color": "black", "connector": "micro-USB"},
            ),
        ),
    ),
)


async def seed_dev_catalog(session: AsyncSession) -> SeedSummary:
    """Create or update the development catalog. Does not commit."""
    merchants = MerchantRepository(session)
    catalog = CatalogRepository(session)
    created = 0

    merchant = await merchants.get_by_slug(MERCHANT_SLUG)
    if merchant is None:
        merchant = await merchants.create(slug=MERCHANT_SLUG, name=MERCHANT_NAME)
        created += 1
    else:
        merchant.name = MERCHANT_NAME

    variant_count = 0
    for definition in PRODUCTS:
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
        products=len(PRODUCTS),
        variants=variant_count,
        created=created,
    )
