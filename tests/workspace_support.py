"""Synthetic merchant sources with materially different structure, for workspace tests.

Three shapes, and they are different on purpose rather than for variety. A bootstrap that only
ever ran against one catalog would be an importer for that catalog, and the whole claim this
phase makes is that a merchant does not need bespoke code.

```text
catalogued    categories, several prices per category, structured metadata, one currency
plain         no categories at all, two currencies, no metadata, one product many variants
awkward       the shapes a real merchant pilot actually produced: several wattages stated only
              in prose, optional fields missing on some variants and present on others, and
              metadata this projection cannot compare
```

`awkward` is a synthetic analogue and holds no real merchant's text. What it reproduces is the
difficulty rather than the data: an evaluation catalog built from it carries less than a reader
of the titles would expect, which is exactly the finding a first evaluation exists to produce.
"""

from agentrank_api.representation.definitions import (
    MerchantSourceDefinition,
    SourceProduct,
    SourceVariant,
)

CURRENCY = "INR"


def variant(
    sku: str,
    *,
    label: str | None = None,
    price: int = 100000,
    currency: str = CURRENCY,
    stock: int = 10,
    metadata: dict[str, object] | None = None,
) -> SourceVariant:
    return SourceVariant(
        sku=sku,
        label=label,
        price_amount_minor=price,
        currency=currency,
        inventory_quantity=stock,
        merchant_metadata=dict(metadata or {}),
    )


def product(
    external_id: str,
    *variants: SourceVariant,
    title: str = "A product",
    description: str | None = None,
    category: str | None = "chargers",
    metadata: dict[str, object] | None = None,
) -> SourceProduct:
    return SourceProduct(
        external_id=external_id,
        title=title,
        description=description,
        category=category,
        variants=variants,
        merchant_metadata=dict(metadata or {}),
    )


def source(
    *products: SourceProduct,
    slug: str = "test-merchant",
    key: str = "merchant-source",
    version: int = 1,
    policy_text: dict[str, str] | None = None,
) -> MerchantSourceDefinition:
    return MerchantSourceDefinition(
        key=key,
        version=version,
        merchant_slug=slug,
        products=products,
        policy_text=dict(policy_text or {}),
    )


def catalogued(slug: str = "test-merchant") -> MerchantSourceDefinition:
    """Categories, a price spread within each, stock depth, and structured colour metadata.

    The ordinary shape. Every mission family this generator has is supportable from it except
    the one about a category nobody can supply, which `awkward` covers.
    """
    return source(
        product(
            "CHG-45",
            variant(
                "CHG-45-BLK", label="Black", price=219900, stock=30, metadata={"finish": "black"}
            ),
            title="45W Compact Charger",
            description="A two-port 45W USB-C charger.",
            category="chargers",
        ),
        product(
            "CHG-100",
            variant(
                "CHG-100-BLK", label="Black", price=469900, stock=16, metadata={"finish": "black"}
            ),
            variant(
                "CHG-100-WHT", label="White", price=489900, stock=0, metadata={"finish": "white"}
            ),
            title="100W Multi-Port Charger",
            description="A three-port 100W USB-C charger.",
            category="chargers",
        ),
        product(
            "CBL-1M",
            variant("CBL-1M-STD", label="1 m", price=69900, stock=4),
            variant("CBL-2M-STD", label="2 m", price=89900, stock=2),
            title="USB-C Cable",
            description="A braided USB-C cable.",
            category="cables",
        ),
        slug=slug,
    )


def plain(slug: str = "plain-merchant") -> MerchantSourceDefinition:
    """No categories, two currencies, no metadata, and one product carrying every variant.

    A merchant whose evidence is as thin as the schema allows. It supports the families that
    need nothing but a price and a shelf, and nothing else, which is the answer a merchant with
    this catalog should be given rather than a suite that pretends otherwise.
    """
    return source(
        product(
            "ONLY",
            variant("ONLY-S", label=None, price=50000, stock=9),
            variant("ONLY-M", label=None, price=75000, stock=9),
            variant("ONLY-L", label=None, price=125000, currency="USD", stock=9),
            title="One product",
            description=None,
            category=None,
        ),
        slug=slug,
    )


def awkward(slug: str = "awkward-merchant") -> MerchantSourceDefinition:
    """The difficulties a real merchant pilot produced, as synthetic data.

    Several wattages stated only in a title, a category nobody can currently supply, optional
    fields present on some variants and absent on others, policy prose with no structured
    counterpart, and metadata that is neither a string nor a whole number.
    """
    return source(
        product(
            "PWR-65",
            variant("PWR-65-A", label="Black", price=329900, stock=3, metadata={"finish": "black"}),
            variant("PWR-65-B", label=None, price=339900, stock=1),
            title="65W Nexode Charger",
            description="A 65W two-port charger for phones and tablets.",
            category="chargers",
        ),
        product(
            "PWR-140",
            variant(
                "PWR-140-A",
                label="Space Grey",
                price=829900,
                stock=2,
                # Neither of these can become a comparable attribute: a fractional number does
                # not survive a JSONB round trip unchanged, and a list is not a scalar.
                metadata={"cable_length_m": 1.5, "ports": ["usb-c", "usb-c", "usb-a"]},
            ),
            title="140W Nexode Desktop Charger",
            description="A four-port 140W charger.",
            category="chargers",
        ),
        product(
            "DCK-11",
            variant("DCK-11-A", label="Grey", price=1299900, stock=0),
            variant("DCK-11-B", label="Silver", price=1349900, stock=0),
            title="11-in-1 Docking Station",
            description="A docking station with HDMI and Ethernet.",
            category="docks",
        ),
        policy_text={
            "returns": "Returns are accepted within 30 days of delivery in original packaging.",
            "warranty": "All chargers carry a 24 month limited warranty.",
        },
        slug=slug,
    )
