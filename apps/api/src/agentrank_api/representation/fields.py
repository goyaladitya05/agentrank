"""The grammar a merchant source field is addressed by, defined once.

A compiler candidate cites the source text behind it as a field address plus an excerpt:
`products[VE-CHG-100].description`, `variants[VE-CBL-USBC-1M].label`, `policy_text.warranty`.
That address is not decoration. It is what makes a proposed fact checkable by the merchant who
supplied the evidence, and it is what the review workflow validates a correction against.

Three readers now share it. The compiler validates that every candidate cites a field this
document actually has, the review workflow validates that a measurement correction is supported
by the text at the field it cites, and the console renders the merchant's own snapshot as the
exact addresses their evidence can be cited by. Three readers of one grammar is when a format
string stops being a detail, so it lives here rather than in whichever module needed it first.

Only text is addressable, and that is deliberate rather than an omission. A price and a stock
level are structured numbers the compiler copies rather than reads, and an excerpt of one would
suggest a reading took place. They are still addressed, as their own decimal string, because a
candidate that copies them has to be able to say where from.

Nothing here normalizes an identifier. A product is addressed by the external identifier the
merchant supplied and a variant by its SKU, exactly as written, so an address is either exact or
it is not an address at all.
"""

from agentrank_api.representation.definitions import MerchantSourceDefinition

# What one field excerpt may be, wherever one is shown or stored. The compiler already refuses a
# candidate excerpt longer than this, and a merchant reading their own source sees the same bound
# rather than an entire product description in a table cell.
MAX_EXCERPT_LENGTH = 500


def source_fields(source: MerchantSourceDefinition) -> dict[str, str]:
    """Every addressable text field of one source document, keyed by its address."""
    fields: dict[str, str] = {}
    for product in source.products:
        prefix = f"products[{product.external_id}]"
        fields[f"{prefix}.title"] = product.title
        if product.description is not None:
            fields[f"{prefix}.description"] = product.description
        if product.category is not None:
            fields[f"{prefix}.category"] = product.category
        for variant in product.variants:
            variant_prefix = f"{prefix}.variants[{variant.sku}]"
            fields[f"{variant_prefix}.price_amount_minor"] = str(variant.price_amount_minor)
            fields[f"{variant_prefix}.inventory_quantity"] = str(variant.inventory_quantity)
            for key, value in variant.merchant_metadata.items():
                if isinstance(value, str):
                    fields[f"{variant_prefix}.merchant_metadata.{key}"] = value
            if variant.label is not None:
                fields[f"{variant_prefix}.label"] = variant.label
    for key, value in source.policy_text.items():
        fields[f"policy_text.{key}"] = value
    return fields


def excerpt(value: str) -> str:
    """One field's text, bounded for display. A truncation says so rather than looking whole."""
    if len(value) <= MAX_EXCERPT_LENGTH:
        return value
    return value[: MAX_EXCERPT_LENGTH - 3] + "..."
