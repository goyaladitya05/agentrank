"""Whether the artifact an evaluation measures describes the world it will be run in.

An evaluation has two halves that come from different places. The world is the isolated catalog
the buyer transacts against and the oracle is computed from, which for a merchant with an
evaluation workspace is the catalog that workspace generated. The merchant information handed to
the buyer as context is the artifact under test: a source snapshot for a first evaluation, a
published Commerce IR for a re-evaluation.

Those two can drift apart, and when they do the run measures the drift rather than the buyer.

```text
world says      VE-CHG-100 costs 499900 INR and there are three
artifact says   VE-CHG-100 costs 249900 INR
buyer           quotes 249900, is charged 499900, breaks its own budget, is marked wrong
```

Nothing about that is the buyer's doing and nothing about it is the merchant's product being
bad. It is AgentRank having told the buyer something the shop it put them in does not agree with.

A first evaluation cannot reach that state, because the launch freezes the workspace's own source
snapshot and the world is that snapshot projected. A re-evaluation can: the representation under
test is compiled from whichever snapshot the merchant compiled, which may be newer than the one
their evaluation setup was built from. Adding an attribute to a source and recompiling is the
ordinary product loop and contradicts nothing; changing a price or withdrawing a line is a
different artifact describing a different shop.

The controlled experiment is the third place an artifact is paired with a world, and it had every
lineage rule binding the representation to the source and both to the merchant, and none binding
either to the environment. It uses this too, and refuses the same drift a launch does.

So this compares, and it compares only the three facts a world is authoritative for. Titles,
descriptions, categories and typed attributes are exactly what a representation exists to
improve, and a representation that states more of them than the world holds is doing its job.

Only where a workspace generated the world. An operator-authored world's catalog is a file and the
environment row records its identity rather than its content, so there is nothing in the database
to compare against and nothing here is asked.

Plain dictionaries in and reasons out. No session, no ORM and no fixture construction: the
workspace's stored catalog payload and the representation's stored payload are both already in
hand wherever this is called, and rebuilding either into a validated domain object to compare
three fields per variant would be work a page render pays for on every load.
"""

from dataclasses import dataclass
from typing import Any


# What a world is the authority on, and therefore what an artifact may not contradict. A null
# `purchasable` is a representation that says it does not know, which contradicts nothing.
@dataclass(frozen=True, slots=True)
class VariantFacts:
    price_amount_minor: int
    currency: str
    purchasable: bool | None


# How many contradictions a refusal names. A merchant reading one needs enough to recognise what
# happened and does not need all two hundred and fifty, and the message is a response body.
MAX_REPORTED = 3


def world_facts(catalog: dict[str, Any]) -> dict[str, VariantFacts]:
    """One stored evaluation catalog payload as the facts it is authoritative for, by SKU."""
    found: dict[str, VariantFacts] = {}
    for product in _entries(catalog, "products"):
        product_active = product.get("is_active", True) is True
        for variant in _entries(product, "variants"):
            sku = variant.get("sku")
            price = _whole(variant.get("price_amount_minor"))
            currency = variant.get("currency")
            quantity = _whole(variant.get("inventory_quantity"))
            if not isinstance(sku, str) or price is None or not isinstance(currency, str):
                continue
            active = product_active and variant.get("is_active", True) is True
            found[sku] = VariantFacts(
                price_amount_minor=price,
                currency=currency,
                purchasable=active and quantity is not None and quantity > 0,
            )
    return found


def representation_facts(payload: dict[str, Any]) -> dict[str, VariantFacts]:
    """One stored Commerce IR payload as the same three facts, by SKU.

    A variant whose price fact is not the shape a compiler writes is skipped rather than guessed
    at. This function exists to find disagreements, and a fact it cannot read is not one.
    """
    found: dict[str, VariantFacts] = {}
    for product in _entries(payload, "products"):
        for variant in _entries(product, "variants"):
            sku = variant.get("sku")
            price = variant.get("price")
            if not isinstance(sku, str) or not isinstance(price, dict):
                continue
            value = price.get("value")
            if not isinstance(value, dict):
                continue
            amount = _whole(value.get("amount_minor"))
            currency = value.get("currency")
            if amount is None or not isinstance(currency, str):
                continue
            found[sku] = VariantFacts(
                price_amount_minor=amount,
                currency=currency,
                purchasable=_availability(variant.get("availability")),
            )
    return found


def contradictions(
    world: dict[str, VariantFacts], artifact: dict[str, VariantFacts]
) -> tuple[str, ...]:
    """Every way this artifact says something about the world that the world denies.

    One direction only. An artifact that omits a variant the world holds is an incomplete
    description, which is a worse discovery surface and not a false one; the buyer still finds
    the line through the storefront and the oracle is unaffected. An artifact that states
    something different is a false one.
    """
    found: list[str] = []
    for sku in sorted(artifact):
        stated = artifact[sku]
        actual = world.get(sku)
        if actual is None:
            found.append(f"{sku} is not in the evaluation world at all")
            continue
        if (stated.price_amount_minor, stated.currency) != (
            actual.price_amount_minor,
            actual.currency,
        ):
            found.append(
                f"{sku} is {stated.price_amount_minor} {stated.currency} here and"
                f" {actual.price_amount_minor} {actual.currency} in the evaluation world"
            )
        if stated.purchasable is not None and stated.purchasable is not actual.purchasable:
            available = "available" if stated.purchasable else "unavailable"
            found.append(f"{sku} is {available} here and the opposite in the evaluation world")
    return tuple(found)


def _availability(fact: Any) -> bool | None:
    if not isinstance(fact, dict):
        return None
    value = fact.get("value")
    if value == "TRUE":
        return True
    if value == "FALSE":
        return False
    return None


def _entries(payload: dict[str, Any], name: str) -> list[dict[str, Any]]:
    value = payload.get(name)
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, dict)]


def _whole(value: Any) -> int | None:
    """One JSON number as an integer, or None for anything that is not one.

    Booleans are excluded on purpose. `True` is an `int` in Python and is not a price.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value
