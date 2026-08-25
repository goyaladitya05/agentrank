"""Generating a first benchmark suite deterministically from one frozen evaluation catalog.

The tests that matter here are the methodology ones. A generator that produced a plausible suite
with a wrong answer key would pass every shape test and would still be worthless, so the ones
below check the two claims the oracle actually makes: a purchase mission is genuinely
purchasable, and an abstention mission is genuinely impossible, both against the merchant's own
frozen catalog and by the same predicate a run recomputes ground truth with.
"""

from collections.abc import Callable

import pytest
from workspace_support import awkward, catalogued, plain, product, source, variant

from agentrank_api.benchmark.catalog import satisfies
from agentrank_api.benchmark.definitions import (
    BenchmarkSuiteDefinition,
    ExpectedOutcome,
)
from agentrank_api.benchmark.identity import suite_content_hash
from agentrank_api.mandates.intent import AllowedCategory, RequiredAttribute
from agentrank_api.representation.definitions import MerchantSourceDefinition
from agentrank_api.workspace.definitions import (
    DEFAULT_MISSION_BUDGET,
    BootstrapConfiguration,
    BootstrapRefusedError,
    MissionFamily,
)
from agentrank_api.workspace.generation import GeneratedSuite, generate_suite
from agentrank_api.workspace.projection import EvaluationCatalog, project_catalog

SLUG = "test-merchant"


def catalog_for(
    definition: MerchantSourceDefinition | None = None, *, slug: str = SLUG
) -> EvaluationCatalog:
    document = catalogued(slug) if definition is None else definition
    return project_catalog(document, merchant_slug=slug, merchant_name="Test Merchant", version=1)


def generated(
    definition: MerchantSourceDefinition | None = None,
    *,
    slug: str = SLUG,
    version: int = 1,
    configuration: BootstrapConfiguration | None = None,
) -> GeneratedSuite:
    return generate_suite(
        catalog_for(definition, slug=slug),
        merchant_slug=slug,
        version=version,
        configuration=configuration or BootstrapConfiguration(),
    )


def outcomes(suite: BenchmarkSuiteDefinition) -> dict[ExpectedOutcome, int]:
    counts = dict.fromkeys(ExpectedOutcome, 0)
    for mission in suite.missions:
        counts[mission.oracle.expected_outcome] += 1
    return counts


# Ground truth


def test_every_purchase_mission_is_genuinely_purchasable() -> None:
    """Not a claim the generator makes. The merchant's own frozen catalog answers it."""
    catalog = catalog_for()
    suite = generate_suite(
        catalog, merchant_slug=SLUG, version=1, configuration=BootstrapConfiguration()
    )
    for mission in suite.definition.missions:
        if mission.oracle.expected_outcome is not ExpectedOutcome.PURCHASE_AVAILABLE:
            continue
        qualifying = [entry for entry in catalog.entries if satisfies(mission.brief, entry)]
        assert qualifying, mission.key
        assert (
            mission.oracle.simulated_value_amount_minor
            == min(entry.price_amount_minor for entry in qualifying) * mission.brief.quantity
        )


def test_every_abstention_mission_is_genuinely_impossible() -> None:
    catalog = catalog_for()
    suite = generate_suite(
        catalog, merchant_slug=SLUG, version=1, configuration=BootstrapConfiguration()
    )
    for mission in suite.definition.missions:
        if mission.oracle.expected_outcome is not ExpectedOutcome.NO_ACCEPTABLE_PURCHASE:
            continue
        assert not [entry for entry in catalog.entries if satisfies(mission.brief, entry)]
        assert mission.oracle.simulated_value_amount_minor == 0


@pytest.mark.parametrize("builder", [catalogued, plain, awkward])
def test_ground_truth_holds_across_materially_different_catalogs(
    builder: Callable[[str], MerchantSourceDefinition],
) -> None:
    """Three catalogs with different structure, one predicate, no bespoke code for any of them."""
    catalog = catalog_for(builder(SLUG))
    suite = generate_suite(
        catalog, merchant_slug=SLUG, version=1, configuration=BootstrapConfiguration()
    )
    for mission in suite.definition.missions:
        qualifying = [entry for entry in catalog.entries if satisfies(mission.brief, entry)]
        available = mission.oracle.expected_outcome is ExpectedOutcome.PURCHASE_AVAILABLE
        assert bool(qualifying) is available, mission.key


def test_an_abstention_mission_holds_a_budget_that_could_never_have_been_spent() -> None:
    """A control mission carries no simulated demand, so potential demand cannot be inflated."""
    suite = generated()
    for mission in suite.definition.missions:
        if mission.oracle.expected_outcome is ExpectedOutcome.NO_ACCEPTABLE_PURCHASE:
            assert mission.oracle.simulated_value_amount_minor == 0


def test_a_stock_abstention_asks_for_one_more_than_the_shelf_holds() -> None:
    """Grounded in the merchant's own stock rather than in a number chosen to be impossible."""
    catalog = catalog_for()
    suite = generate_suite(
        catalog, merchant_slug=SLUG, version=1, configuration=BootstrapConfiguration()
    )
    stock = [
        mission
        for mission in suite.definition.missions
        if mission.key.startswith("stock-abstention")
    ]
    assert stock
    for mission in stock:
        allowed = {
            constraint.category
            for constraint in mission.brief.hard_constraints
            if isinstance(constraint, AllowedCategory)
        }
        deepest = max(
            entry.inventory_quantity
            for entry in catalog.entries
            if entry.product_category in allowed and entry.can_supply(1)
        )
        assert mission.brief.quantity == deepest + 1


def test_a_specification_mission_states_only_an_attribute_the_merchant_stated() -> None:
    catalog = catalog_for()
    suite = generate_suite(
        catalog, merchant_slug=SLUG, version=1, configuration=BootstrapConfiguration()
    )
    stated = {(key, value) for entry in catalog.entries for key, value in entry.attributes.items()}
    for mission in suite.definition.missions:
        for constraint in mission.brief.hard_constraints:
            if isinstance(constraint, RequiredAttribute):
                assert (constraint.name, constraint.value) in stated


def test_a_specification_is_only_used_when_a_buyer_could_read_it() -> None:
    """A key nothing on the storefront mentions makes a question no shopper would ask."""
    document = source(
        product(
            "P1",
            variant("P1-A", label="Black", price=100000, metadata={"internal_bin": "A4"}),
            title="A charger",
            description="A charger.",
            category="chargers",
        ),
        slug=SLUG,
    )
    suite = generated(document)
    used = {
        constraint.name
        for mission in suite.definition.missions
        for constraint in mission.brief.hard_constraints
        if isinstance(constraint, RequiredAttribute)
    }
    assert "internal_bin" not in used


def test_a_number_specification_is_matched_on_a_digit_boundary() -> None:
    """`1000W` in a title is not the merchant stating a hundred watts."""
    document = source(
        product(
            "P1",
            variant("P1-A", label="Black", price=100000, metadata={"watts": 100}),
            title="1000W Bench Supply",
            description="A bench supply.",
            category="power",
        ),
        slug=SLUG,
    )
    suite = generated(document)
    used = {
        constraint.name
        for mission in suite.definition.missions
        for constraint in mission.brief.hard_constraints
        if isinstance(constraint, RequiredAttribute)
    }
    assert "watts" not in used


# Independence


def test_a_generated_objective_carries_no_merchant_text() -> None:
    """The objective is the one channel a buyer reads as its own goal, and a second copy of a
    fact the constraints already carry would make a descriptive category name an easier
    benchmark."""
    document = catalogued(SLUG)
    words = {
        text.casefold()
        for entry in document.products
        for text in (entry.title, entry.description, entry.category)
        if text
    }
    for mission in generated(document).definition.missions:
        objective = mission.brief.objective.casefold()
        assert all(word not in objective for word in words)


def test_two_missions_of_different_kinds_read_identically_to_a_buyer() -> None:
    """A control mission whose prose warned the buyer would be handing over the answer key."""
    suite = generated()
    by_outcome: dict[ExpectedOutcome, set[str]] = {outcome: set() for outcome in ExpectedOutcome}
    for mission in suite.definition.missions:
        if mission.brief.quantity == 1:
            by_outcome[mission.oracle.expected_outcome].add(mission.brief.objective)
    assert by_outcome[ExpectedOutcome.PURCHASE_AVAILABLE]
    assert (
        by_outcome[ExpectedOutcome.PURCHASE_AVAILABLE]
        == (by_outcome[ExpectedOutcome.NO_ACCEPTABLE_PURCHASE])
    )


def test_a_brief_never_carries_the_answer() -> None:
    """The serialized buyer-facing half has no field an oracle could hide in."""
    for mission in generated().definition.missions:
        payload = mission.brief.to_payload()
        assert "expected_outcome" not in payload
        assert "simulated_value_amount_minor" not in payload


# Determinism and identity


def test_generation_is_deterministic() -> None:
    first = generated()
    second = generated()
    assert suite_content_hash(first.definition) == suite_content_hash(second.definition)
    assert [mission.key for mission in first.definition.missions] == [
        mission.key for mission in second.definition.missions
    ]


def test_changed_evidence_changes_the_generated_suite() -> None:
    document = catalogued(SLUG)
    thinner = source(*document.products[:1], slug=SLUG)
    assert suite_content_hash(generated(document).definition) != suite_content_hash(
        generated(thinner).definition
    )


def test_a_different_mission_budget_produces_a_different_suite() -> None:
    small = generated(configuration=BootstrapConfiguration(mission_budget=4))
    large = generated(configuration=BootstrapConfiguration(mission_budget=10))
    assert small.mission_count == 4
    assert large.mission_count == 10
    assert suite_content_hash(small.definition) != suite_content_hash(large.definition)


def test_a_generated_suite_is_bounded_by_its_mission_budget() -> None:
    assert generated().mission_count <= DEFAULT_MISSION_BUDGET


def test_mission_keys_are_unique_and_carry_no_merchant_text() -> None:
    suite = generated()
    keys = [mission.key for mission in suite.definition.missions]
    assert len(set(keys)) == len(keys)
    assert all(key.replace("-", "").isalnum() and key.islower() for key in keys)


# Composition


def test_a_small_budget_still_measures_buying_and_declining() -> None:
    """Draining one family first would give a four mission suite four easy purchases."""
    suite = generated(configuration=BootstrapConfiguration(mission_budget=4))
    counts = outcomes(suite.definition)
    assert counts[ExpectedOutcome.PURCHASE_AVAILABLE] > 0
    assert counts[ExpectedOutcome.NO_ACCEPTABLE_PURCHASE] > 0


def test_the_composition_accounts_for_every_mission() -> None:
    suite = generated()
    assert sum(entry.missions for entry in suite.composition) == suite.mission_count
    for entry in suite.composition:
        assert entry.purchase_available + entry.no_acceptable_purchase == entry.missions


def test_a_family_the_catalog_cannot_support_is_reported_rather_than_invented() -> None:
    """A merchant with no categories and no metadata gets fewer families and is told so."""
    suite = generated(plain(SLUG))
    built = {entry.family for entry in suite.composition}
    unsupported = {entry.family for entry in suite.unsupported}
    assert MissionFamily.SPECIFICATION_PURCHASE in unsupported
    assert MissionFamily.UNAVAILABLE_ABSTENTION in unsupported
    assert built.isdisjoint(unsupported)
    assert all(entry.reason for entry in suite.unsupported)


def test_a_policy_mission_is_always_reported_as_unsupported() -> None:
    """There are two outcomes and a policy answer is neither, so this benchmark cannot mark one."""
    for suite in (generated(), generated(plain(SLUG)), generated(awkward(SLUG))):
        families = {entry.family for entry in suite.unsupported}
        assert MissionFamily.POLICY_CONSTRAINT in families


def test_a_category_nobody_can_supply_produces_an_abstention() -> None:
    suite = generated(awkward(SLUG))
    built = {entry.family for entry in suite.composition}
    assert MissionFamily.UNAVAILABLE_ABSTENTION in built


def test_a_merchant_with_no_category_still_gets_missions() -> None:
    suite = generated(plain(SLUG))
    assert suite.mission_count > 0
    assert not any(
        isinstance(constraint, AllowedCategory)
        for mission in suite.definition.missions
        for constraint in mission.brief.hard_constraints
    )


def test_a_mission_never_mixes_currencies() -> None:
    catalog = catalog_for(plain(SLUG))
    suite = generate_suite(
        catalog, merchant_slug=SLUG, version=1, configuration=BootstrapConfiguration()
    )
    for mission in suite.definition.missions:
        qualifying = [entry for entry in catalog.entries if satisfies(mission.brief, entry)]
        assert all(entry.currency == mission.brief.currency for entry in qualifying)


def test_a_catalog_supporting_no_mission_is_refused_by_name() -> None:
    """A free product cannot carry simulated demand, so nothing here can be built from it."""
    document = source(product("P1", variant("P1-A", price=0), category=None), slug=SLUG)
    with pytest.raises(BootstrapRefusedError) as refused:
        generated(document)
    assert refused.value.blocker.code == "no_mission_family"
