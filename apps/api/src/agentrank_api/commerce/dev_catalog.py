"""A small deterministic catalog for local development.

Not benchmark data. This exists so that a developer can start PostgreSQL, run one command
and have something real to query. The merchant the first benchmark suite is authored against
is VoltEdge, and it lives in `agentrank_api.benchmark.voltedge`.

The shapes and the convergent seeding both come from `agentrank_api.commerce.catalog_fixture`,
which the two fixtures share. What is here is the data.

The data is deliberately imperfect. One product has no description, one has no category,
one product is inactive, one variant is inactive, one variant is out of stock and one is
priced in a second currency. A catalog where everything is present and tidy would not
exercise anything AgentRank cares about.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.commerce.catalog_fixture import (
    SeedProduct,
    SeedSummary,
    SeedVariant,
    seed_catalog,
)

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
    return await seed_catalog(session, slug=MERCHANT_SLUG, name=MERCHANT_NAME, products=PRODUCTS)
