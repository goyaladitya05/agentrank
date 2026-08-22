"""The merchant's authoritative data, as the benchmark reads it.

Two jobs, both pure, and both about the half of ground truth that the suite content hash cannot
cover.

The first is pinning. A benchmark result is only reproducible if the workload *and* the merchant
are both accounted for, and the suite hash accounts for the workload alone. Prices change, stock
moves, attributes get published, products are withdrawn. Without a record of what the catalog
looked like, a before and after comparison attributes every difference to whatever was changed
on purpose, and there is no way to see that anything else moved. `catalog_content_hash` gives a
run something to be compared across.

The second is checking the oracle. A mission's expected outcome is a human claim about a
catalog, published suites are immutable, and catalogs are not, so ground truth decays. It decays
asymmetrically: a mission authored when something was in stock quietly becomes impossible, and
the executor is marked down for not finding what is no longer there. `facts_for` recomputes the
claim from the merchant's own rows so the disagreement becomes a number.

Reading this from the authoritative database rather than from a merchant surface is the whole
point rather than a shortcut. AgentRank exists because the facts are there and agents cannot use
them; the harness reading them directly is what makes "the data existed and the agent could not
act on it" a measurable statement rather than an assertion.
"""

import hashlib
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from agentrank_api.benchmark.definitions import AgentMissionBrief
from agentrank_api.benchmark.evaluation import CatalogFacts
from agentrank_api.benchmark.identity import HASH_ALGORITHM, canonical_json
from agentrank_api.benchmark.observation import ObservedSelection
from agentrank_api.constraints.rules import ConstraintOperator, compare, lookup_attribute
from agentrank_api.mandates.intent import AllowedCategory, RequiredAttribute
from agentrank_api.money import validate_amount_minor, validate_currency


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """One purchasable configuration, as the facts a mission is decided against.

    Only what the benchmark actually reads. Titles, descriptions and anything else written in
    prose are absent, for the same reason a checkout line omits them: nothing here compares
    prose, and putting it in the pin would mean a copy edit changed the catalog identity.
    """

    variant_id: uuid.UUID
    sku: str
    product_category: str | None
    attributes: Mapping[str, Any] = field(default_factory=dict)
    price_amount_minor: int = 0
    currency: str = "INR"
    inventory_quantity: int = 0
    is_active: bool = True

    def __post_init__(self) -> None:
        validate_amount_minor(self.price_amount_minor)
        validate_currency(self.currency)
        if self.inventory_quantity < 0:
            raise ValueError("catalog inventory must not be negative")

    def can_supply(self, quantity: int) -> bool:
        """Whether a buyer could actually take this many of these away today.

        Active and stocked to the quantity asked for. An inactive variant is something the
        merchant no longer sells and an understocked one is something it has not got enough of,
        and neither can satisfy a mission, which is why one predicate covers both here and two
        failure reasons separate them when an executor tries anyway.

        It takes the quantity rather than testing for any stock at all, and that is a correction
        an independent review forced. A mission wanting two units of something one unit of which
        remains is a mission nobody can complete, and reporting it as satisfiable meant the
        executor was marked down for a discovery failure while the oracle check reported no
        disagreement. The budget comparison beside it already multiplied by the quantity.
        """
        return self.is_active and self.inventory_quantity >= quantity

    def to_payload(self) -> dict[str, Any]:
        """The entry as it enters the catalog pin."""
        return {
            "sku": self.sku,
            "category": self.product_category,
            "attributes": dict(self.attributes),
            "price_amount_minor": self.price_amount_minor,
            "currency": self.currency,
            "inventory_quantity": self.inventory_quantity,
            "is_active": self.is_active,
        }


def catalog_content_hash(entries: Sequence[CatalogEntry]) -> str:
    """A labelled digest of everything about a merchant's catalog that a mission can read.

    Keyed by SKU and sorted by it, so the digest does not depend on the order rows came back in
    or on identifiers that differ between databases. Two runs whose pins match were measured
    against the same merchant; two whose pins differ were not, and any difference between them
    is jointly caused by whatever changed and by whatever else changed at the same time.

    Deliberately not stored as the catalog itself. This says whether the merchant moved, not
    what it looked like, and a benchmark is not an archive of somebody's product data.
    """
    payload = {entry.sku: entry.to_payload() for entry in entries}
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8"))
    return f"{HASH_ALGORITHM}:{digest.hexdigest()}"


def facts_for(
    brief: AgentMissionBrief,
    entries: Sequence[CatalogEntry],
    selection: ObservedSelection | None = None,
) -> CatalogFacts:
    """What the merchant's own data says about one mission, right now.

    Two answers. Whether anything the merchant sells satisfies this buyer within their budget,
    which is the mission's ground truth recomputed rather than taken on an author's word. And
    whether what the executor picked is something the merchant actually sells, which is what
    makes `INVALID_VARIANT` reachable without waiting for the merchant to volunteer a refusal.

    The predicates are the buyer's own, compared through the same vocabulary the semantic
    authorization gate uses, so this cannot conclude that something qualifies which that gate
    would then deny.
    """
    return CatalogFacts(
        qualifying_variant_exists=any(satisfies(brief, entry) for entry in entries),
        selection_is_sellable=None if selection is None else _sellable(selection, entries),
    )


def satisfies(brief: AgentMissionBrief, entry: CatalogEntry) -> bool:
    """Whether one purchasable variant meets everything this mission requires.

    Fail closed in the same three ways the evaluator is. An inactive variant, or one the merchant
    has not got enough of, cannot satisfy anything. A category the merchant never published
    cannot be an allowed one. An attribute that is absent, or present in a form that cannot be
    compared, is not a pass.

    The quantity is compared against stock as well as against the budget, and the executor that
    decides what to buy applies the same rule. When the two disagreed, a mission whose only
    qualifying variant had one unit left and wanted two was reported as satisfiable here and
    declined there, so the executor was marked down for a discovery failure it never had a
    chance at and the oracle check reported no disagreement to explain it.

    The buyer's own quantity *ceiling* is still not checked here. It is a property of the brief
    alone, so checking it per entry would make this answer false for every variant on the
    strength of something no variant has anything to do with. `AgentMissionBrief` refuses that
    mission outright instead.
    """
    if not entry.can_supply(brief.quantity):
        return False
    if entry.currency != brief.currency:
        return False
    if entry.price_amount_minor * brief.quantity > brief.budget.amount_minor:
        return False
    allowed = tuple(
        constraint.category
        for constraint in brief.hard_constraints
        if isinstance(constraint, AllowedCategory)
    )
    if allowed:
        if entry.product_category is None:
            return False
        if compare(ConstraintOperator.IN, allowed, entry.product_category) is not True:
            return False

    for constraint in brief.hard_constraints:
        if not isinstance(constraint, RequiredAttribute):
            continue
        found, actual = lookup_attribute(entry.attributes, constraint.name)
        if not found:
            return False
        if compare(constraint.operator, constraint.value, actual) is not True:
            return False
    return True


def _sellable(selection: ObservedSelection, entries: Sequence[CatalogEntry]) -> bool:
    """Whether the merchant offers the thing that was selected at all.

    False for a variant the merchant does not have and for one it no longer sells. Deliberately
    true for one it has simply run out of: the merchant does sell that, it cannot ship it today,
    and those are different findings with different repairs and separate reasons. Reporting an
    empty shelf as a catalog that lists things nobody sells would be the wrong repair to hand a
    merchant, and the taxonomy says so in `INVALID_VARIANT`'s own words.

    Absence is the interesting case: an executor that named a variant this merchant has never
    had is the shape a hallucinated identifier takes.
    """
    for entry in entries:
        if entry.variant_id == selection.variant_id:
            return entry.is_active
    return False
