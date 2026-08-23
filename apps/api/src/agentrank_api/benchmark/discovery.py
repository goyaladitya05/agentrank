"""The buyer-facing discovery boundary, and the two sides of it.

An ordinary merchant's buyer-facing discovery surface is the one shoppers see: names, prose
descriptions, variant labels, prices, availability and categories. The normalized typed attribute
dictionary behind it (`wattage: 100 W`, `ports: 3`) is internal catalog structure. A human
shopping the storefront reads it as prose or not at all, so a raw merchant's buyer does not get
it as data.

The Merchant Compiler's product is exactly that second surface: an agent-ready discovery view
where every important fact is explicit, typed and unit-bearing. This module keeps the two apart
as data instead of as an experiment's intention.

Three rules make the split honest rather than cosmetic.

First, the catalog's own attribute dictionaries never reach a model buyer through either arm.
A storefront view drops them; an agent-ready view replaces them with facts taken from the pinned
Commerce IR representation. The compiled arm's enrichment therefore comes from the treatment
artifact itself, never from the authoritative catalog the evaluator marks against, so the oracle
cannot leak into an arm through its own enrichment.

Second, only discovery answers are projected. Quotes, reservations, authorization decisions and
payments are financial truth from the merchant kernel and are identical for both arms. The one
exception is the quote line's typed attribute snapshot: it is stripped for both arms alike,
because it is catalog structure arriving through the checkout channel after selection has already
happened, and leaving it would let a raw buyer recover the treatment difference by quoting.

Third, everything else survives untouched. Titles, descriptions, labels, prices, stock counts and
categories are facts an ordinary storefront publishes to any shopper, and hiding them would be
making the raw arm artificially broken rather than ordinarily informed.

Pure domain code: no SQLAlchemy, no HTTP, no service and no oracle import, so both the trusted
runner and the untrusted worker process can use the same functions without either side gaining a
route to the other.
"""

import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# The one fact shape an agent-ready surface publishes, flattened from Commerce IR. Provenance,
# authority, confidence and review state are compiler workflow metadata and stay behind the
# boundary with them.
FACT_FIELDS = ("key", "kind", "unit", "value")

ORACLE_FIELDS = frozenset({"expected_outcome", "simulated_value", "simulated_value_amount_minor"})


class DiscoveryKind(StrEnum):
    STOREFRONT = "STOREFRONT"
    AGENT_READY = "AGENT_READY"


@dataclass(frozen=True, slots=True)
class BuyerDiscoveryView:
    """Which discovery surface this mission's buyer sees, and the facts behind it."""

    kind: DiscoveryKind
    representation_id: uuid.UUID | None = None
    attributes_by_sku: dict[str, tuple[dict[str, Any], ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind is DiscoveryKind.STOREFRONT:
            if self.representation_id is not None or self.attributes_by_sku:
                raise ValueError("a storefront discovery view carries no representation")
            return
        if self.representation_id is None:
            raise ValueError("an agent-ready discovery view names its representation")
        if not isinstance(self.attributes_by_sku, dict):
            raise ValueError("agent-ready attributes must be keyed by sku")
        for sku, facts in self.attributes_by_sku.items():
            if not isinstance(sku, str) or not sku.strip():
                raise ValueError("agent-ready attributes must be keyed by sku")
            for item in facts:
                if set(item) != set(FACT_FIELDS):
                    raise ValueError("an agent-ready fact carries key, kind, unit and value")


def storefront_view() -> BuyerDiscoveryView:
    """The ordinary merchant surface: no typed attribute dictionaries anywhere."""
    return BuyerDiscoveryView(kind=DiscoveryKind.STOREFRONT)


def agent_ready_view(
    representation_id: uuid.UUID, attributes_by_sku: dict[str, tuple[dict[str, Any], ...]]
) -> BuyerDiscoveryView:
    """The compiler-produced surface: the storefront plus typed facts from one Commerce IR."""
    return BuyerDiscoveryView(
        kind=DiscoveryKind.AGENT_READY,
        representation_id=representation_id,
        attributes_by_sku=attributes_by_sku,
    )


def buyer_discovery_view(
    *,
    representation_kind: str,
    representation_id: uuid.UUID | None,
    representation_payload: dict[str, Any] | None,
) -> BuyerDiscoveryView:
    """The view one experiment treatment must use, decided by its frozen sample identity.

    A raw sample binds no representation and gets the storefront. A compiled sample binds
    exactly one representation and gets that representation's facts. Any other pairing is a
    construction error rather than a silently weakened arm.
    """
    if representation_kind == "RAW":
        if representation_id is not None or representation_payload is not None:
            raise ValueError("a raw treatment cannot carry a Commerce IR representation")
        return storefront_view()
    if representation_kind == "COMPILED":
        if representation_id is None or representation_payload is None:
            raise ValueError("a compiled treatment requires its Commerce IR representation")
        return agent_ready_view(representation_id, _ir_attributes(representation_payload))
    raise ValueError(f"unknown representation kind {representation_kind!r}")


def _ir_attributes(payload: dict[str, Any]) -> dict[str, tuple[dict[str, Any], ...]]:
    """Flatten one Commerce IR payload into typed facts keyed by variant sku."""
    by_sku: dict[str, tuple[dict[str, Any], ...]] = {}
    for product in payload.get("products", []):
        for variant in product.get("variants", []):
            sku = variant.get("sku")
            if not isinstance(sku, str) or not sku.strip():
                raise ValueError("a Commerce IR variant is missing its sku")
            facts: list[dict[str, Any]] = []
            for attribute in variant.get("attributes", []):
                facts.append(
                    {
                        "key": attribute["key"],
                        "kind": attribute["kind"],
                        "unit": attribute["unit"],
                        "value": attribute["fact"]["value"],
                    }
                )
            by_sku[sku] = tuple(facts)
    return by_sku


def to_payload(view: BuyerDiscoveryView) -> dict[str, Any]:
    """One discovery view as protocol JSON."""
    payload: dict[str, Any] = {"kind": view.kind.value}
    if view.kind is DiscoveryKind.AGENT_READY:
        assert view.representation_id is not None  # enforced by the view itself
        payload["representation_id"] = str(view.representation_id)
        payload["attributes"] = [
            {"sku": sku, **{name: fact[name] for name in FACT_FIELDS}}
            for sku, facts in sorted(view.attributes_by_sku.items())
            for fact in facts
        ]
    return payload


def view_from_payload(payload: Any) -> BuyerDiscoveryView:
    """Rebuild and verify one discovery view handed across the worker boundary."""
    if not isinstance(payload, dict):
        raise ValueError("the discovery view must be an object")
    kind = payload.get("kind")
    if kind == DiscoveryKind.STOREFRONT.value:
        if set(payload) != {"kind"}:
            raise ValueError("a storefront discovery view carries no representation")
        return storefront_view()
    if kind != DiscoveryKind.AGENT_READY.value:
        raise ValueError(f"unknown discovery view {kind!r}")
    if set(payload) != {"kind", "representation_id", "attributes"}:
        raise ValueError("an agent-ready discovery view carries its representation and facts")
    try:
        representation_id = uuid.UUID(payload["representation_id"])
    except (TypeError, ValueError) as malformed:
        raise ValueError("an agent-ready discovery view names a representation") from malformed
    if not isinstance(payload["attributes"], list):
        raise ValueError("agent-ready attributes must be a list")
    reject_oracle_fields(payload)
    by_sku: dict[str, tuple[dict[str, Any], ...]] = {}
    for item in payload["attributes"]:
        if not isinstance(item, dict) or set(item) != {"sku", *FACT_FIELDS}:
            raise ValueError("an agent-ready fact names its sku, key, kind, unit and value")
        sku = item["sku"]
        if not isinstance(sku, str) or not sku.strip():
            raise ValueError("an agent-ready fact names its sku")
        if item["key"] in ORACLE_FIELDS:
            raise ValueError("a published fact may not be named like benchmark ground truth")
        by_sku.setdefault(sku, ())
        by_sku[sku] = (*by_sku[sku], {name: item[name] for name in FACT_FIELDS})
    return agent_ready_view(representation_id, by_sku)


def discover(view: BuyerDiscoveryView, document: Any) -> Any:
    """One discovery answer as this buyer sees it.

    Every variant presentation the merchant returns carries its catalog attribute dictionary.
    That dictionary is replaced here, never shown: a storefront buyer gets prose and labels
    without it, and an agent-ready buyer gets the pinned representation's typed facts in its
    place. Variants the representation does not cover are simply unenriched, which is honest:
    an incomplete agent-ready surface is less help, not wrong help.
    """
    return _project(view, document)


def quoted(document: Any) -> Any:
    """One quote, reservation, authorization or payment answer as the buyer sees it.

    Two things are removed for both arms identically, so the commerce channel stays financial
    truth and cannot quietly restore the discovery difference after a selection has been made.

    The typed attribute snapshot on a checkout line is what trusted semantic authorization
    decides against, not what a shopper is shown. And the `actual` value of an intent
    authorization violation is that same catalog dictionary read out for the variant that was
    quoted; a buyer probing refusals could otherwise recover it one attribute at a time. The
    violation's code, attribute name and expected value survive, so a denial stays explainable.
    """
    stripped = _strip(document, "variant_attributes")
    return _strip(stripped, "actual")


def reject_oracle_fields(value: Any) -> None:
    """Keep benchmark ground truth out of a discovery view's facts."""
    if isinstance(value, dict):
        forbidden = sorted(ORACLE_FIELDS & set(value))
        if forbidden:
            raise ValueError(f"discovery view contains benchmark oracle fields: {forbidden}")
        for item in value.values():
            reject_oracle_fields(item)
    elif isinstance(value, list):
        for item in value:
            reject_oracle_fields(item)


def _project(view: BuyerDiscoveryView, value: Any) -> Any:
    if isinstance(value, list):
        return [_project(view, item) for item in value]
    if not isinstance(value, dict):
        return value
    projected = {name: _project(view, item) for name, item in value.items()}
    if "attributes" in projected and "sku" in projected:
        facts = (
            view.attributes_by_sku.get(projected["sku"])
            if view.kind is (DiscoveryKind.AGENT_READY)
            else ()
        )
        if facts:
            projected["attributes"] = [dict(fact) for fact in facts]
        else:
            projected.pop("attributes")
    return projected


def _strip(value: Any, name: str) -> Any:
    if isinstance(value, list):
        return [_strip(item, name) for item in value]
    if not isinstance(value, dict):
        return value
    return {key: _strip(item, name) for key, item in value.items() if key != name}
