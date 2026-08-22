"""Benchmark definitions: validation, the intent vocabulary, and content identity."""

import json
import uuid

import pytest
from benchmark_support import BLACK, CHARGERS, NOT_BLACK, brief, mission, suite

from agentrank_api.benchmark.definitions import (
    AgentMissionBrief,
    BenchmarkSuiteDefinition,
    ExpectedOutcome,
    MissionOracle,
)
from agentrank_api.benchmark.identity import canonical_payload, suite_content_hash
from agentrank_api.constraints.rules import ConstraintOperator
from agentrank_api.mandates.intent import (
    AllowedCategory,
    MaxQuantity,
    MaxTotalAmount,
    Preference,
    RequiredAttribute,
    hard_constraint_from_payload,
)


def test_a_brief_states_its_budget_once() -> None:
    """A second amount ceiling is a ceiling that can disagree with the first."""
    with pytest.raises(ValueError, match="not as a constraint"):
        brief(constraints=(MaxTotalAmount(amount_minor=100, currency="INR"),))


def test_a_brief_states_at_most_one_quantity_ceiling() -> None:
    with pytest.raises(ValueError, match="at most one quantity ceiling"):
        brief(constraints=(MaxQuantity(2), MaxQuantity(3)))


def test_a_brief_refuses_a_zero_budget() -> None:
    """Zero authorizes nothing, so a mission carrying it could never be completed."""
    with pytest.raises(ValueError, match="budget must be positive"):
        brief(budget_minor=0)


@pytest.mark.parametrize("quantity", [0, -1])
def test_a_brief_refuses_a_non_positive_quantity(quantity: int) -> None:
    with pytest.raises(ValueError, match="quantity must be positive"):
        brief(quantity=quantity)


@pytest.mark.parametrize("key", ["Buy_A_Charger", "buy a charger", "", "buy--charger"])
def test_a_mission_key_is_a_slug(key: str) -> None:
    with pytest.raises(ValueError, match="mission key"):
        brief(key)


def test_a_brief_refuses_a_blank_objective() -> None:
    with pytest.raises(ValueError, match="objective must not be blank"):
        brief(objective="   ")


def test_max_quantity_is_read_from_the_constraints_and_is_none_when_unstated() -> None:
    """None means no limit. It is not zero and it is not the desired quantity."""
    assert brief(quantity=2).max_quantity is None
    assert brief(quantity=2, constraints=(MaxQuantity(3),)).max_quantity == 3


def test_a_brief_becomes_a_buyer_intent_in_the_existing_vocabulary() -> None:
    """The benchmark reuses BuyerIntent rather than translating into a second language."""
    merchant_id = uuid.uuid7()
    stated = brief(constraints=(BLACK, CHARGERS), preferences=(Preference("prefer braided"),))

    intent = stated.to_intent(merchant_id)

    assert intent.merchant_id == merchant_id
    assert intent.description == stated.objective
    # The budget rejoins the constraints in first position: on an intent a financial ceiling
    # is one more stated requirement.
    assert intent.hard_constraints == (stated.budget, BLACK, CHARGERS)
    assert intent.preferences == (Preference("prefer braided"),)


def test_an_oracle_requires_a_value_when_a_purchase_is_available() -> None:
    with pytest.raises(ValueError, match="must carry a positive value"):
        MissionOracle(
            expected_outcome=ExpectedOutcome.PURCHASE_AVAILABLE,
            simulated_value_amount_minor=0,
        )


def test_an_oracle_refuses_a_value_when_no_purchase_is_acceptable() -> None:
    """Counting demand nothing could serve would inflate potential simulated GMV."""
    with pytest.raises(ValueError, match="carries no simulated value"):
        MissionOracle(
            expected_outcome=ExpectedOutcome.NO_ACCEPTABLE_PURCHASE,
            simulated_value_amount_minor=1,
        )


def test_a_suite_refuses_duplicate_mission_keys() -> None:
    """A mission key is how a result is attributed, so two would make one ambiguous."""
    with pytest.raises(ValueError, match="unique within a suite"):
        suite(mission("one"), mission("one"))


def test_a_suite_refuses_no_missions() -> None:
    with pytest.raises(ValueError, match="at least one mission"):
        BenchmarkSuiteDefinition(
            key="empty", version=1, merchant_slug="voltedge", name="Empty", missions=()
        )


def test_a_suite_version_starts_at_one() -> None:
    with pytest.raises(ValueError, match="version must be at least 1"):
        suite(version=0)


def test_a_suite_names_a_mission_or_raises() -> None:
    defined = suite(mission("one"), mission("two"))

    assert defined.mission("two").key == "two"
    with pytest.raises(KeyError):
        defined.mission("three")


# The oracle projection: what an agent may never receive.


ORACLE_FIELDS = frozenset({"expected_outcome", "simulated_value_amount_minor"})


def test_the_agent_facing_brief_has_no_oracle_field() -> None:
    """Structural: the brief type does not name a single oracle field."""
    assert ORACLE_FIELDS.isdisjoint(AgentMissionBrief.__dataclass_fields__)


def test_a_serialized_brief_contains_no_oracle_value() -> None:
    """Textual, and the reason it is textual is that a leak need not be a named field.

    An oracle value smuggled into an objective string, a preference or a nested constraint
    payload would pass a field name check. This searches the bytes an agent would actually
    receive for the two facts it must not learn.
    """
    defined = mission(outcome=ExpectedOutcome.NO_ACCEPTABLE_PURCHASE)
    serialized = json.dumps(defined.brief.to_payload())

    assert "NO_ACCEPTABLE_PURCHASE" not in serialized
    assert "expected_outcome" not in serialized
    assert "simulated_value" not in serialized


def test_the_briefs_projection_yields_briefs_and_not_missions() -> None:
    """An executor is handed briefs, so it cannot reach an oracle even by accident."""
    projected = suite(mission("one"), mission("two")).briefs()

    assert [item.key for item in projected] == ["one", "two"]
    assert all(isinstance(item, AgentMissionBrief) for item in projected)


# Round tripping, which is what makes a stored definition the same definition.


def test_a_brief_round_trips_through_its_payload() -> None:
    original = brief(
        constraints=(
            BLACK,
            CHARGERS,
            RequiredAttribute("wattage", 100, ConstraintOperator.GTE),
            RequiredAttribute("connector", ("USB-C", "USB-A"), ConstraintOperator.IN),
            MaxQuantity(2),
        ),
        preferences=(Preference("prefer black"), Preference("prefer two ports")),
    )

    assert AgentMissionBrief.from_payload(json.loads(json.dumps(original.to_payload()))) == original


def test_a_constraint_payload_with_an_unknown_kind_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown hard constraint kind"):
        hard_constraint_from_payload({"kind": "max_carbon_footprint", "value": 1})


def test_a_constraint_payload_with_a_mistyped_field_is_refused() -> None:
    """A requirement nobody stated is a requirement that silently passes."""
    with pytest.raises(ValueError, match="amount_minor must be a whole number"):
        hard_constraint_from_payload(
            {"kind": "max_total_amount", "amount_minor": "5000", "currency": "INR"}
        )


def test_a_boolean_is_not_a_whole_number_in_a_constraint_payload() -> None:
    """`bool` is a subclass of `int`, and this is the coercion the vocabulary refuses."""
    with pytest.raises(ValueError, match="quantity must be a whole number"):
        hard_constraint_from_payload({"kind": "max_quantity", "quantity": True})


def test_a_budget_payload_of_the_wrong_kind_is_refused() -> None:
    with pytest.raises(ValueError, match="must be a max_total_amount"):
        AgentMissionBrief.from_payload(
            {
                "key": "wrong",
                "objective": "Buy something",
                "quantity": 1,
                "budget": AllowedCategory("chargers").to_payload(),
                "hard_constraints": [],
                "preferences": [],
            }
        )


# Content identity.


def test_the_same_definition_hashes_the_same_from_two_independent_constructions() -> None:
    """Determinism, asserted between two objects rather than against one function twice."""
    first = suite(mission("one"), mission("two", constraints=(CHARGERS,)))
    second = suite(mission("one"), mission("two", constraints=(CHARGERS,)))

    assert first is not second
    assert suite_content_hash(first) == suite_content_hash(second)


def test_the_hash_is_a_labelled_sha256() -> None:
    digest = suite_content_hash(suite())

    assert digest.startswith("sha256:")
    assert len(digest) == 71


@pytest.mark.parametrize(
    ("label", "changed"),
    [
        # Each entry differs from `suite()` in exactly one field. A case that changed two
        # would still pass with either one missing from the identity, which is the way a
        # sensitivity test quietly stops testing anything.
        ("suite key", suite(key="other-suite")),
        ("suite version", suite(version=2)),
        ("merchant slug", suite(merchant_slug="other-merchant")),
        ("mission key", suite(mission("renamed"))),
        ("objective", suite(mission(objective="Buy one white charger"))),
        ("quantity", suite(mission(quantity=2))),
        ("budget", suite(mission(budget_minor=400000))),
        ("currency", suite(mission(currency="EUR"))),
        ("constraint", suite(mission(constraints=(CHARGERS,)))),
        ("constraint value", suite(mission(constraints=(RequiredAttribute("color", "white"),)))),
        ("constraint operator", suite(mission(constraints=(NOT_BLACK,)))),
        ("preference", suite(mission(preferences=(Preference("prefer braided"),)))),
        ("expected outcome", suite(mission(outcome=ExpectedOutcome.NO_ACCEPTABLE_PURCHASE))),
        ("simulated value", suite(mission(value_minor=1))),
        ("mission count", suite(mission(), mission("second"))),
    ],
)
def test_every_semantic_change_changes_the_hash(
    label: str, changed: BenchmarkSuiteDefinition
) -> None:
    """A field an author could change without the identity noticing is the bug this has."""
    assert suite_content_hash(changed) != suite_content_hash(suite()), label


def test_mission_order_is_part_of_the_identity() -> None:
    """The sequence a workload presents its missions in is part of the workload."""
    forwards = suite(mission("one"), mission("two"))
    backwards = suite(mission("two"), mission("one"))

    assert suite_content_hash(forwards) != suite_content_hash(backwards)


def test_the_display_name_is_not_part_of_the_identity() -> None:
    """Correcting a label must not force a version bump, or authors will edit in place."""
    assert suite_content_hash(suite(name="One name")) == suite_content_hash(suite(name="Another"))


def test_the_canonical_payload_holds_the_fields_the_identity_covers() -> None:
    """Asserted on the payload as well as the digest, so a missing field is nameable."""
    payload = canonical_payload(suite())

    assert set(payload) == {"key", "version", "merchant_slug", "missions"}
    assert set(payload["missions"][0]) == {"brief", "oracle"}
    assert set(payload["missions"][0]["brief"]) == {
        "key",
        "objective",
        "quantity",
        "budget",
        "hard_constraints",
        "preferences",
    }
    assert set(payload["missions"][0]["oracle"]) == ORACLE_FIELDS
