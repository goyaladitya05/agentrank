"""Generating one merchant's first benchmark suite from their frozen evaluation catalog.

A published suite is trusted benchmark truth: its oracle is the answer key an executor is marked
against, and a wrong one is worse than no benchmark at all because it produces a confident number
about a merchant that means nothing. Generating one therefore has to be held to a stricter
standard than reading a catalog is, and this module is built around one rule that makes that
possible.

**Every oracle is computed, never asserted.** A candidate mission is proposed from catalog facts,
and its expected outcome is then decided by `agentrank_api.benchmark.catalog.satisfies`, which is
the same predicate a benchmark run uses to recompute a mission's ground truth while it executes,
written in the same vocabulary the semantic authorization gate denies with. Nothing here writes
an expected outcome down; it asks the merchant's own frozen data and takes the answer.

That is what makes two properties true by construction rather than by care:

```text
a purchase mission is genuinely purchasable    something in the frozen catalog satisfies it
an abstention mission is genuinely impossible  nothing in the frozen catalog satisfies it
```

A candidate whose computed outcome disagrees with the family that proposed it is dropped rather
than relabelled, so a family can never quietly turn into its opposite and no mission is ever
published with an outcome nobody derived.

Four separations keep the result a benchmark of a merchant rather than of AgentRank.

Generation reads the frozen source-derived catalog and nothing else. No compiler run, no Commerce
IR, no candidate, no review and no published representation. If a first benchmark needed compiled
facts to state its own truth, the compiler would be measuring itself.

It reads no benchmark result, no mission trace, no diagnostic finding and no previous suite. A
workload shaped around what a buyer failed at last time is not a measurement of a merchant, and
there is no input here through which one could be.

It runs no model. Nothing decides that a title implies a wattage or that two products are
compatible. The only semantic claims a generated mission makes are ones the merchant stated as
structured data, and one further filter is applied to those, described at `_observable`.

And no generated mission's prose carries merchant text. Objectives are written here and
parameterized by a quantity alone. That is a security property, because a mission objective is
the one channel a buyer reads as its own goal rather than as merchant data. It is also a
methodology property, and that is the stronger of the two reasons: an objective restating a
category in prose would hand the buyer the same fact through a second channel, so a merchant
whose category reads `wireless-chargers` would measure as more machine-readable than one whose
category reads `cat-17` for a reason that has nothing to do with their data.

Sampling is a stable total order followed by a bounded take. There is no random number generator
here and therefore no seed: the same catalog and the same configuration produce the same suite,
in the same order, in every process and every database.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from agentrank_api.benchmark.catalog import CatalogEntry, satisfies
from agentrank_api.benchmark.definitions import (
    MAX_NAME_LENGTH,
    AgentMissionBrief,
    BenchmarkMissionDefinition,
    BenchmarkSuiteDefinition,
    ExpectedOutcome,
    MissionOracle,
)
from agentrank_api.constraints.rules import (
    MAX_ATTRIBUTE_KEY_LENGTH,
    MAX_TEXT_VALUE_LENGTH,
    ConstraintOperator,
    normalize_text,
)
from agentrank_api.mandates.intent import (
    AllowedCategory,
    HardConstraint,
    MaxTotalAmount,
    RequiredAttribute,
)
from agentrank_api.workspace.definitions import (
    FAMILY_ORDER,
    MAX_STOCK_ABSTENTION_QUANTITY,
    MULTI_UNIT_QUANTITY,
    NO_SUPPORTING_EVIDENCE,
    POLICY_UNSUPPORTED,
    BootstrapBlocker,
    BootstrapConfiguration,
    BootstrapRefusedError,
    FamilyComposition,
    MissionFamily,
    UnsupportedFamily,
    mission_key,
    workspace_key,
)
from agentrank_api.workspace.projection import EvaluationCatalog

# The suffix a generated suite's key carries, so a suite this package built is distinguishable
# from one an operator authored without reading a row's provenance.
SUITE_KEY_SUFFIX = "-workspace-suite"

# The display name of a generated suite. A label and nothing more: it is excluded from the suite
# content hash exactly as an authored suite's name is.
SUITE_NAME = "Generated evaluation suite"

# How many missions one family may contribute. A merchant with forty categories should not get a
# suite that is forty near-identical category purchases and nothing else, so each family is
# capped and the mission budget is filled by cycling the families that have anything left rather
# than by draining the first one.
MAX_MISSIONS_PER_FAMILY = 3

# The provisional key a candidate's brief carries while its ground truth is computed. A mission
# key names a position in the published suite, which is not known until the draw is finished, and
# nothing about the oracle depends on it.
_PROVISIONAL_ORDINAL = 0


@dataclass(frozen=True, slots=True)
class GeneratedSuite:
    """One merchant's generated first workload, and an account of how it was composed.

    `composition` and `unsupported` are the product surface this owes a merchant: what they are
    about to be measured on, and which shapes of mission their own data could not support. A
    suite that happened to contain only easy purchases and a suite whose merchant cannot express
    an abstention look identical without them.
    """

    definition: BenchmarkSuiteDefinition
    composition: tuple[FamilyComposition, ...]
    unsupported: tuple[UnsupportedFamily, ...]

    @property
    def mission_count(self) -> int:
        return len(self.definition.missions)

    def to_payload(self) -> dict[str, Any]:
        """The composition as it is stored beside the workspace and rendered in the console."""
        return {
            "families": [entry.to_payload() for entry in self.composition],
            "unsupported": [entry.to_payload() for entry in self.unsupported],
        }


@dataclass(frozen=True, slots=True)
class _Candidate:
    """One proposed mission, before the catalog has said whether it is possible.

    Holds no outcome and no value. Those are computed from the catalog after the brief exists,
    which is the whole point: a candidate is a question about the merchant's data and never an
    answer. `expected` is what the family that proposed it claims the answer will be, and it is
    checked rather than trusted.
    """

    family: MissionFamily
    quantity: int
    budget: MaxTotalAmount
    constraints: tuple[HardConstraint, ...]
    expected: ExpectedOutcome
    # The stable order key within its family. Content only, never an identifier and never a
    # position in whatever order a dictionary happened to be built in.
    order: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class _Resolved:
    """One candidate the catalog agreed with, with the oracle it computed."""

    family: MissionFamily
    brief: AgentMissionBrief
    oracle: MissionOracle


def generate_suite(
    catalog: EvaluationCatalog,
    *,
    merchant_slug: str,
    version: int,
    configuration: BootstrapConfiguration,
) -> GeneratedSuite:
    """Build one merchant's first benchmark suite from their frozen evaluation catalog.

    Refuses rather than padding. A merchant whose catalog supports no mission at all gets a
    named blocker, because a suite of zero missions would report a perfect score over nothing and
    a suite of invented ones would report a score about nothing.
    """
    resolved = {family: _resolved(family, catalog) for family in FAMILY_ORDER}
    drawn = _drawn(resolved, budget=configuration.mission_budget)
    if not drawn:
        raise BootstrapRefusedError(
            BootstrapBlocker(
                "no_mission_family",
                "AgentRank could not build a single benchmark mission from your merchant"
                " information. A catalog needs at least one product a buyer could be asked to"
                " buy, with a price and stock.",
            )
        )

    missions: list[tuple[MissionFamily, BenchmarkMissionDefinition]] = []
    counters = dict.fromkeys(FAMILY_ORDER, 0)
    for entry in drawn:
        counters[entry.family] += 1
        brief = replace(entry.brief, key=mission_key(entry.family, counters[entry.family]))
        missions.append(
            (entry.family, BenchmarkMissionDefinition(brief=brief, oracle=entry.oracle))
        )

    definition = BenchmarkSuiteDefinition(
        key=workspace_key(merchant_slug, SUITE_KEY_SUFFIX),
        version=version,
        merchant_slug=merchant_slug,
        name=SUITE_NAME[:MAX_NAME_LENGTH],
        missions=tuple(mission for _, mission in missions),
    )
    return GeneratedSuite(
        definition=definition,
        composition=_composition(missions),
        unsupported=_unsupported(missions),
    )


def _resolved(family: MissionFamily, catalog: EvaluationCatalog) -> list[_Resolved]:
    """Every candidate of one family the catalog actually agreed with, in a stable order.

    Sorted by content before the bound is applied, so which candidates survive does not depend
    on the order a dictionary or a set happened to iterate in. A candidate the catalog disagreed
    with is dropped here, so it never consumes a place in the mission budget that a mission the
    catalog does support could have had.
    """
    builder = _BUILDERS.get(family)
    if builder is None:
        # `POLICY_CONSTRAINT`, and anything added later without a builder. Absent on purpose
        # rather than empty by accident: a family with no builder produces nothing and is
        # reported as unsupported.
        return []
    accepted: list[_Resolved] = []
    for candidate in sorted(builder(catalog), key=lambda proposal: proposal.order):
        agreed = _agreed(candidate, catalog.entries)
        if agreed is not None:
            accepted.append(agreed)
        if len(accepted) == MAX_MISSIONS_PER_FAMILY:
            break
    return accepted


def _agreed(candidate: _Candidate, entries: Sequence[CatalogEntry]) -> _Resolved | None:
    """This candidate with its computed oracle, or None when the catalog says otherwise.

    Three ways to be dropped, and all three are the catalog disagreeing rather than an error.
    The computed outcome may not be the one the family claimed, which is the check that makes a
    purchase family incapable of publishing an abstention under its own name. And a purchase
    whose cheapest qualifying line is free carries no simulated demand, which a mission with an
    available purchase is not allowed to do, because a zero would drop it out of the potential
    demand it belongs in.
    """
    brief = AgentMissionBrief(
        key=mission_key(candidate.family, _PROVISIONAL_ORDINAL),
        objective=_objective(candidate.quantity),
        budget=candidate.budget,
        quantity=candidate.quantity,
        hard_constraints=candidate.constraints,
    )
    outcome, value = _ground_truth(brief, entries)
    if outcome is not candidate.expected:
        return None
    if outcome is ExpectedOutcome.PURCHASE_AVAILABLE and value <= 0:
        return None
    return _Resolved(
        family=candidate.family,
        brief=brief,
        oracle=MissionOracle(expected_outcome=outcome, simulated_value_amount_minor=value),
    )


def _objective(quantity: int) -> str:
    """What a generated mission says to a buyer, in this repository's own words.

    One sentence for every generated mission, differing only in the quantity. It carries no
    merchant string, states no expected outcome and hints at nothing: an abstention mission and
    a purchase mission read identically, which is what stops the prose from being a second copy
    of the answer key. Everything the buyer is meant to satisfy is in the typed constraints
    beside it, which is where the evaluator reads them from as well.
    """
    if quantity == 1:
        return (
            "Buy one unit of a product that meets every requirement stated in this mission,"
            " within the stated budget."
        )
    return (
        f"Buy {quantity} units of one product that meets every requirement stated in this"
        " mission, within the stated budget."
    )


def _ground_truth(
    brief: AgentMissionBrief, entries: Sequence[CatalogEntry]
) -> tuple[ExpectedOutcome, int]:
    """What the merchant's frozen catalog makes possible for this mission, and what it is worth.

    Computed over the whole evaluation catalog rather than over whichever slice proposed the
    mission. A brief with a category constraint excludes other categories through the constraint
    itself, which is the same exclusion the authorization gate applies, so answering from a
    pre-filtered subset would be answering a different question than the one a run asks.

    The value is the cheapest qualifying line at the mission's quantity. It is simulated buyer
    demand rather than revenue, and taking the cheapest keeps it a floor: the merchant could have
    served this mission for at least that much.
    """
    qualifying = [entry for entry in entries if satisfies(brief, entry)]
    if not qualifying:
        return ExpectedOutcome.NO_ACCEPTABLE_PURCHASE, 0
    cheapest = min(entry.price_amount_minor for entry in qualifying)
    return ExpectedOutcome.PURCHASE_AVAILABLE, cheapest * brief.quantity


def _drawn(resolved: Mapping[MissionFamily, list[_Resolved]], *, budget: int) -> list[_Resolved]:
    """Fill the mission budget by cycling the families, so a small suite still measures both.

    Round robin in `FAMILY_ORDER`, which alternates purchase and abstention. Draining one family
    before starting the next would give a merchant with many categories a suite of category
    purchases and no abstention at all, and a benchmark that only ever asks a buyer to buy cannot
    tell a careful buyer from one that buys whatever it finds.
    """
    remaining = {family: list(entries) for family, entries in resolved.items()}
    drawn: list[_Resolved] = []
    while len(drawn) < budget:
        progressed = False
        for family in FAMILY_ORDER:
            if len(drawn) >= budget:
                break
            pool = remaining.get(family)
            if not pool:
                continue
            drawn.append(pool.pop(0))
            progressed = True
        if not progressed:
            break
    return drawn


def _composition(
    missions: Sequence[tuple[MissionFamily, BenchmarkMissionDefinition]],
) -> tuple[FamilyComposition, ...]:
    """How many missions of each family the suite holds, and what each of them expects."""
    families: list[FamilyComposition] = []
    for family in FAMILY_ORDER:
        built = [mission for name, mission in missions if name is family]
        if not built:
            continue
        available = sum(
            1
            for mission in built
            if mission.oracle.expected_outcome is ExpectedOutcome.PURCHASE_AVAILABLE
        )
        families.append(
            FamilyComposition(
                family=family,
                missions=len(built),
                purchase_available=available,
                no_acceptable_purchase=len(built) - available,
            )
        )
    return tuple(families)


def _unsupported(
    missions: Sequence[tuple[MissionFamily, BenchmarkMissionDefinition]],
) -> tuple[UnsupportedFamily, ...]:
    """Which mission shapes this merchant's evidence did not produce, and why not.

    Reported rather than quietly absent. A merchant reading a suite of four purchases should be
    able to see that their catalog carries no structured specification and nothing out of stock,
    rather than concluding that AgentRank only ever asks easy questions.
    """
    built = {family for family, _ in missions}
    missing = [
        UnsupportedFamily(family=family, reason=NO_SUPPORTING_EVIDENCE)
        for family in FAMILY_ORDER
        if family not in built
    ]
    return (*missing, UnsupportedFamily(MissionFamily.POLICY_CONSTRAINT, POLICY_UNSUPPORTED))


@dataclass(frozen=True, slots=True)
class _Group:
    """One category and currency, and the lines in it. The unit a family is proposed from.

    Currency is part of the key because a mission has exactly one budget in exactly one currency,
    and a ceiling compared against a price in another currency is not a tighter constraint, it is
    a meaningless one. Nothing here converts between currencies and nothing sums across them.
    """

    category: str | None
    currency: str
    purchasable: tuple[CatalogEntry, ...]
    every: tuple[CatalogEntry, ...]

    @property
    def constraints(self) -> tuple[HardConstraint, ...]:
        """The category constraint this group states, or nothing when it has no category.

        A merchant whose products carry no category still gets missions; they are simply about
        the whole catalog. Inventing a category name for them would be writing down a fact the
        merchant did not state.
        """
        return () if self.category is None else (AllowedCategory(self.category),)

    @property
    def order(self) -> tuple[Any, ...]:
        return (self.category or "", self.currency)


def _groups(catalog: EvaluationCatalog) -> list[_Group]:
    """The catalog split by category and currency, in a stable order."""
    entries = catalog.entries
    keys = sorted({(entry.product_category or "", entry.currency) for entry in entries})
    groups: list[_Group] = []
    for category, currency in keys:
        member = [
            entry
            for entry in entries
            if (entry.product_category or "") == category and entry.currency == currency
        ]
        groups.append(
            _Group(
                category=_category(category),
                currency=currency,
                purchasable=tuple(entry for entry in member if entry.can_supply(1)),
                every=tuple(member),
            )
        )
    return groups


def _category(value: str) -> str | None:
    """A category that can be stated as a constraint, or None.

    Blank is None rather than a constraint nobody can satisfy. A product whose category is an
    empty string has no category, whatever the column holds, and `AllowedCategory` refuses a
    blank anyway.
    """
    return value if value.strip() else None


def _buyer_text(catalog: EvaluationCatalog) -> dict[str, str]:
    """What a buyer reading the ordinary storefront can see about each variant, normalized.

    Title, prose description and variant label, which is exactly what the storefront discovery
    boundary publishes. The typed attribute dictionary is deliberately absent: a raw merchant's
    buyer does not receive one, and this exists to ask what such a buyer could read.
    """
    text: dict[str, str] = {}
    for product in catalog.fixture.products:
        shared = " ".join(
            part for part in (product.title, product.description, product.category) if part
        )
        for variant in product.variants:
            text[variant.sku] = normalize_text(f"{shared} {variant.label or ''}")
    return text


def _observable(value: str | int, text: str) -> bool:
    """Whether one attribute value is also visible in the buyer-facing text of its variant.

    A discoverability filter and never evidence of meaning. It decides which of a merchant's own
    structured facts are worth building a mission around, and it decides nothing about what any
    of them mean: an attribute is only ever used with the key and the value the merchant wrote.

    Without it, a merchant who records `internal_bin: A4` would be asked to sell a buyer
    something with an internal bin of A4, which is a question no shopper would ask and no
    storefront could answer. That is a bad mission rather than a false one, and a benchmark full
    of them would measure how much operational metadata a merchant keeps.

    Numbers are matched on a digit boundary, so a mission about a hundred watt charger is not
    proposed on the strength of a title reading `1000W`.
    """
    if isinstance(value, bool):
        # A boolean has no faithful prose form. `true` appearing in a description is not the
        # merchant saying the flag is set, so this filter has nothing to check and refuses.
        return False
    if isinstance(value, int):
        return re.search(rf"(?<!\d){value}(?!\d)", text) is not None
    normalized = normalize_text(value)
    return bool(normalized) and normalized in text


def _specifications(group: _Group, text: Mapping[str, str]) -> list[tuple[str, str | int]]:
    """Every attribute key and value in this group that a mission may be built around.

    Bounded by the constraint vocabulary's own rules rather than by a judgement about meaning: a
    key or a value the authorization layer could not compare is not one a mission may state.
    Sorted, so the proposal order is a property of the content rather than of a dictionary.
    """
    stated: set[tuple[str, str | int]] = set()
    for entry in group.every:
        observable = text.get(entry.sku, "")
        for key, value in entry.attributes.items():
            if isinstance(value, bool) or not isinstance(value, str | int):
                continue
            if not key.strip() or len(key) > MAX_ATTRIBUTE_KEY_LENGTH:
                continue
            if isinstance(value, str) and (not value.strip() or len(value) > MAX_TEXT_VALUE_LENGTH):
                continue
            if not _observable(value, observable):
                continue
            stated.add((key, value))
    return sorted(stated, key=lambda pair: (pair[0], str(pair[1])))


def _category_purchases(catalog: EvaluationCatalog) -> Iterable[_Candidate]:
    """Buy something from this category, with room for anything in it.

    The budget covers the most expensive purchasable line in the group, so the mission is about
    finding something in the category at all rather than about the money. This is the
    straightforward purchase family, and a benchmark of only these would be a weak one, which is
    why it is one of eight.
    """
    for group in _groups(catalog):
        if not group.purchasable:
            continue
        ceiling = max(entry.price_amount_minor for entry in group.purchasable)
        if ceiling <= 0:
            continue
        yield _Candidate(
            family=MissionFamily.CATEGORY_PURCHASE,
            quantity=1,
            budget=MaxTotalAmount(amount_minor=ceiling, currency=group.currency),
            constraints=group.constraints,
            expected=ExpectedOutcome.PURCHASE_AVAILABLE,
            order=group.order,
        )


def _budget_constrained_purchases(catalog: EvaluationCatalog) -> Iterable[_Candidate]:
    """Buy something from this category for exactly what the cheapest one costs.

    Only where the group holds more than one price, because otherwise this is the straightforward
    purchase written twice. The budget is the cheapest purchasable line's own price, so a buyer
    picking anything dearer is refused by its own authorization rather than by the merchant, and
    the mission genuinely requires reading prices.
    """
    for group in _groups(catalog):
        prices = {entry.price_amount_minor for entry in group.purchasable}
        if len(prices) < 2:
            continue
        cheapest = min(prices)
        if cheapest <= 0:
            continue
        yield _Candidate(
            family=MissionFamily.BUDGET_CONSTRAINED_PURCHASE,
            quantity=1,
            budget=MaxTotalAmount(amount_minor=cheapest, currency=group.currency),
            constraints=group.constraints,
            expected=ExpectedOutcome.PURCHASE_AVAILABLE,
            order=group.order,
        )


def _multi_unit_purchases(catalog: EvaluationCatalog) -> Iterable[_Candidate]:
    """Buy more than one of something the merchant holds more than one of.

    Proposed only from lines the merchant actually has enough of, so the quantity is a property
    of the shelf rather than a number chosen to make the mission harder.
    """
    quantity = MULTI_UNIT_QUANTITY
    for group in _groups(catalog):
        stocked = [entry for entry in group.purchasable if entry.can_supply(quantity)]
        if not stocked:
            continue
        ceiling = max(entry.price_amount_minor for entry in stocked) * quantity
        if ceiling <= 0:
            continue
        yield _Candidate(
            family=MissionFamily.MULTI_UNIT_PURCHASE,
            quantity=quantity,
            budget=MaxTotalAmount(amount_minor=ceiling, currency=group.currency),
            constraints=group.constraints,
            expected=ExpectedOutcome.PURCHASE_AVAILABLE,
            order=group.order,
        )


def _specification_purchases(catalog: EvaluationCatalog) -> Iterable[_Candidate]:
    """Buy something in this category that carries a specification the merchant stated.

    The one family that measures whether a merchant's own structured facts survive contact with
    a buyer reading their storefront. The constraint is the merchant's key and the merchant's
    value, unchanged; what makes the mission hard is that a raw storefront publishes no typed
    attribute dictionary, so the buyer has to find the fact in prose. That is the product thesis
    stated as a measurement rather than as an assertion.
    """
    text = _buyer_text(catalog)
    for group in _groups(catalog):
        if not group.purchasable:
            continue
        ceiling = max(entry.price_amount_minor for entry in group.purchasable)
        if ceiling <= 0:
            continue
        stated = [
            (key, value)
            for key, value in _specifications(group, text)
            if any(_carries(entry, key, value) for entry in group.purchasable)
        ]
        for rank, (key, value) in enumerate(stated):
            yield _Candidate(
                family=MissionFamily.SPECIFICATION_PURCHASE,
                quantity=1,
                budget=MaxTotalAmount(amount_minor=ceiling, currency=group.currency),
                constraints=(
                    *group.constraints,
                    RequiredAttribute(key, value, ConstraintOperator.EQ),
                ),
                expected=ExpectedOutcome.PURCHASE_AVAILABLE,
                # Rank first, so the families that can propose several candidates per group are
                # drawn one from each group before a second from any of them. Sorting by category
                # alone would spend the whole family on whichever category sorts first.
                order=(rank, *group.order, key, str(value)),
            )


def _budget_abstentions(catalog: EvaluationCatalog) -> Iterable[_Candidate]:
    """Ask for something in this category with half the money the cheapest one costs.

    Genuinely impossible and ordinarily so. A shopper who cannot afford anything on the shelf is
    the most common reason a purchase does not happen, and the correct behaviour is to decline
    rather than to buy something outside the authorization.

    Half rather than one minor unit below, because a ceiling a rounding error under the price is
    a trick question. Half rather than a rounded figure, because rounding needs a currency
    exponent and AgentRank deliberately does not decide what any currency's minor unit is worth.
    """
    for group in _groups(catalog):
        if not group.purchasable:
            continue
        cheapest = min(entry.price_amount_minor for entry in group.purchasable)
        budget = cheapest // 2
        if budget < 1:
            continue
        yield _Candidate(
            family=MissionFamily.BUDGET_ABSTENTION,
            quantity=1,
            budget=MaxTotalAmount(amount_minor=budget, currency=group.currency),
            constraints=group.constraints,
            expected=ExpectedOutcome.NO_ACCEPTABLE_PURCHASE,
            order=group.order,
        )


def _stock_abstentions(catalog: EvaluationCatalog) -> Iterable[_Candidate]:
    """Ask for one more unit than this category holds of anything, with money to spare.

    Proposed only where the deepest line in the group is shallow enough that asking for one more
    is an ordinary request. A category holding eighty of something would otherwise produce a
    mission asking for eighty one, which is impossible and is not a thing any buyer would ask.

    The budget covers the whole quantity at the dearest price in the group, so the reason nothing
    qualifies is the shelf rather than the money.
    """
    for group in _groups(catalog):
        if not group.purchasable:
            continue
        deepest = max(entry.inventory_quantity for entry in group.purchasable)
        quantity = deepest + 1
        if quantity > MAX_STOCK_ABSTENTION_QUANTITY:
            continue
        ceiling = max(entry.price_amount_minor for entry in group.purchasable) * quantity
        if ceiling <= 0:
            continue
        yield _Candidate(
            family=MissionFamily.STOCK_ABSTENTION,
            quantity=quantity,
            budget=MaxTotalAmount(amount_minor=ceiling, currency=group.currency),
            constraints=group.constraints,
            expected=ExpectedOutcome.NO_ACCEPTABLE_PURCHASE,
            order=group.order,
        )


def _unavailable_abstentions(catalog: EvaluationCatalog) -> Iterable[_Candidate]:
    """Ask for something from a category the merchant lists and cannot currently supply.

    A category whose every line is out of stock is a real and common merchant state, and it is
    exactly the case where a buyer reading a storefront can see a product and cannot have it.
    The budget covers the dearest line in it, so the money is never the reason.
    """
    for group in _groups(catalog):
        if group.category is None or group.purchasable:
            continue
        ceiling = max(entry.price_amount_minor for entry in group.every)
        if ceiling <= 0:
            continue
        yield _Candidate(
            family=MissionFamily.UNAVAILABLE_ABSTENTION,
            quantity=1,
            budget=MaxTotalAmount(amount_minor=ceiling, currency=group.currency),
            constraints=group.constraints,
            expected=ExpectedOutcome.NO_ACCEPTABLE_PURCHASE,
            order=group.order,
        )


def _specification_abstentions(catalog: EvaluationCatalog) -> Iterable[_Candidate]:
    """Ask for a specification this merchant lists and has none of in stock.

    The merchant states the fact, so the buyer can find it on the storefront; every line carrying
    it is out of stock or withdrawn, so the honest answer is to decline. Distinct from the budget
    abstention because the money is never the reason: the budget covers the dearest line in the
    group.

    Proposed only where nothing purchasable in the group carries the value, which is what makes
    the mission genuinely impossible rather than merely narrow.
    """
    text = _buyer_text(catalog)
    for group in _groups(catalog):
        if group.category is None or not group.every:
            continue
        ceiling = max(entry.price_amount_minor for entry in group.every)
        if ceiling <= 0:
            continue
        stated = [
            (key, value)
            for key, value in _specifications(group, text)
            if not any(_carries(entry, key, value) for entry in group.purchasable)
        ]
        for rank, (key, value) in enumerate(stated):
            yield _Candidate(
                family=MissionFamily.SPECIFICATION_ABSTENTION,
                quantity=1,
                budget=MaxTotalAmount(amount_minor=ceiling, currency=group.currency),
                constraints=(
                    *group.constraints,
                    RequiredAttribute(key, value, ConstraintOperator.EQ),
                ),
                expected=ExpectedOutcome.NO_ACCEPTABLE_PURCHASE,
                order=(rank, *group.order, key, str(value)),
            )


def _carries(entry: CatalogEntry, key: str, value: str | int) -> bool:
    """Whether one line states this exact attribute, by the comparison the gate would apply."""
    actual = entry.attributes.get(key)
    if isinstance(value, str):
        return isinstance(actual, str) and normalize_text(actual) == normalize_text(value)
    return isinstance(actual, int) and not isinstance(actual, bool) and actual == value


_BUILDERS = {
    MissionFamily.CATEGORY_PURCHASE: _category_purchases,
    MissionFamily.BUDGET_CONSTRAINED_PURCHASE: _budget_constrained_purchases,
    MissionFamily.MULTI_UNIT_PURCHASE: _multi_unit_purchases,
    MissionFamily.SPECIFICATION_PURCHASE: _specification_purchases,
    MissionFamily.BUDGET_ABSTENTION: _budget_abstentions,
    MissionFamily.STOCK_ABSTENTION: _stock_abstentions,
    MissionFamily.UNAVAILABLE_ABSTENTION: _unavailable_abstentions,
    MissionFamily.SPECIFICATION_ABSTENTION: _specification_abstentions,
}
