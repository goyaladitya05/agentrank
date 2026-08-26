"""Neutral buyer projections for raw merchant sources and published Commerce IR."""

from typing import Any

from agentrank_api.representation.fixtures import parse_source
from agentrank_api.representation.models import CommerceRepresentation, MerchantSourceSnapshot


def raw_projection(source: MerchantSourceSnapshot) -> dict[str, Any]:
    """Ordinary merchant information, with no compiler interpretation mixed in.

    Stock reaches the buyer exactly as the merchant stated it, which now means a state and a
    count that may be null. A merchant who published "in stock" and no number gives a buyer
    `IN_STOCK` and a null count, which is what a shopper reading that storefront also gets; how
    many there are is a question the commerce runtime answers when the buyer asks it.

    Read through the document reader rather than by indexing the payload, so there is one
    reading of what a stored variant says about its stock and not a second one here that could
    come to disagree with it.
    """
    definition = parse_source(source.payload)
    return {
        "products": [
            {
                "external_id": product.external_id,
                "title": product.title,
                "description": product.description,
                "category": product.category,
                "variants": [
                    {
                        "sku": variant.sku,
                        "label": variant.label,
                        "price_amount_minor": variant.price_amount_minor,
                        "currency": variant.currency,
                        "availability": variant.availability.value,
                        "inventory_quantity": variant.inventory_quantity,
                    }
                    for variant in product.variants
                ],
            }
            for product in definition.products
        ],
        "policy_text": dict(definition.policy_text),
    }


def compiled_projection(representation: CommerceRepresentation) -> dict[str, Any]:
    """Commerce IR facts for a buyer, deliberately omitting compiler workflow metadata."""
    products: list[dict[str, Any]] = []
    for product in representation.payload["products"]:
        products.append(
            {
                "external_id": product["external_id"],
                "title": product["title"]["value"],
                "category": None if product["category"] is None else product["category"]["value"],
                "variants": [
                    {
                        "sku": variant["sku"],
                        "label": variant["label"],
                        "price": variant["price"]["value"],
                        "availability": variant["availability"]["value"],
                        "attributes": [
                            {
                                "key": attribute["key"],
                                "kind": attribute["kind"],
                                "unit": attribute["unit"],
                                "value": attribute["fact"]["value"],
                            }
                            for attribute in variant["attributes"]
                        ],
                        "compatibility": {
                            key: fact["value"] for key, fact in variant["compatibility"].items()
                        },
                    }
                    for variant in product["variants"]
                ],
                "policy_facts": {
                    key: fact["value"] for key, fact in product["policy_facts"].items()
                },
            }
        )
    return {"products": products}
