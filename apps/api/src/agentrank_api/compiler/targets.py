"""The grammar a compiler candidate target is written in, defined once.

A candidate target names the Commerce IR field one proposal would populate:
`variant.VE-CHG-100-BLK.attribute.wattage`, `product.VE-CHG-100.category`,
`policy.warranty_months`. The extractor writes them, the publisher reads them back to assemble
the IR, and from Phase 4D the diagnostics read model addresses one by constructing the same
string and looking it up by the compiler's own `(run_id, target)` unique key.

Three readers of one grammar is exactly when a format string stops being a detail. A copy that
drifts would not fail: the extractor would keep writing targets, the publisher would keep
assembling an IR, and the lookup would simply stop finding anything, which reads as "there is no
compiler work for this finding" rather than as a bug. So the grammar lives here, in a module with
no imports, and everything that writes or reads a target goes through it.

The identifiers inside a target are the merchant's own. A variant is named by its SKU and a
product by its external identifier, both unique per merchant at the database and both the same
values the merchant supplied in their source snapshot. Nothing here normalizes, folds case or
otherwise interprets them: a target is an exact address or it is not one at all.
"""

VARIANT_KIND = "variant"
PRODUCT_KIND = "product"

ATTRIBUTE_SEGMENT = "attribute"
COMPATIBILITY_SEGMENT = "compatibility"

POLICY_PREFIX = "policy."


def variant_prefix(sku: str) -> str:
    """Everything a target about one variant begins with."""
    return f"{VARIANT_KIND}.{sku}."


def product_prefix(external_id: str) -> str:
    """Everything a target about one product begins with."""
    return f"{PRODUCT_KIND}.{external_id}."


def variant_price_target(sku: str) -> str:
    return f"{variant_prefix(sku)}price"


def variant_availability_target(sku: str) -> str:
    return f"{variant_prefix(sku)}availability"


def variant_attribute_prefix(sku: str) -> str:
    return f"{variant_prefix(sku)}{ATTRIBUTE_SEGMENT}."


def variant_attribute_target(sku: str, attribute_key: str) -> str:
    """The one target that would carry `attribute_key` for the variant with this SKU."""
    return f"{variant_attribute_prefix(sku)}{attribute_key}"


def variant_compatibility_prefix(sku: str) -> str:
    return f"{variant_prefix(sku)}{COMPATIBILITY_SEGMENT}."


def variant_compatibility_target(sku: str, capability: str) -> str:
    return f"{variant_compatibility_prefix(sku)}{capability}"


def product_title_target(external_id: str) -> str:
    return f"{product_prefix(external_id)}title"


def product_category_target(external_id: str) -> str:
    """The one target that would carry a published category for this product."""
    return f"{product_prefix(external_id)}category"


def policy_target(name: str) -> str:
    return f"{POLICY_PREFIX}{name}"
