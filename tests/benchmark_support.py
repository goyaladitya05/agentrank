"""Builders for benchmark definitions used by more than one test file.

Not fixtures, because what each test needs varies and a fixture cannot be called twice with
different arguments in one test. Two suites differing in exactly one field is the shape most
of the identity tests are about.

Everything here goes through the real validating constructors, so a definition a test builds
is a definition the application would accept.
"""

from agentrank_api.benchmark.definitions import (
    AgentMissionBrief,
    BenchmarkMissionDefinition,
    BenchmarkSuiteDefinition,
    ExpectedOutcome,
    MissionOracle,
)
from agentrank_api.constraints.rules import ConstraintOperator
from agentrank_api.mandates.intent import (
    AllowedCategory,
    HardConstraint,
    MaxTotalAmount,
    Preference,
    RequiredAttribute,
)

CURRENCY = "INR"
BUDGET = 500000
VALUE = 499900

MERCHANT_SLUG = "test-merchant"

BLACK = RequiredAttribute("color", "black", ConstraintOperator.EQ)
CHARGERS = AllowedCategory("chargers")


def brief(
    key: str = "buy-a-charger",
    *,
    objective: str = "Buy one black charger",
    budget_minor: int = BUDGET,
    currency: str = CURRENCY,
    quantity: int = 1,
    constraints: tuple[HardConstraint, ...] = (BLACK,),
    preferences: tuple[Preference, ...] = (),
) -> AgentMissionBrief:
    return AgentMissionBrief(
        key=key,
        objective=objective,
        budget=MaxTotalAmount(amount_minor=budget_minor, currency=currency),
        quantity=quantity,
        hard_constraints=constraints,
        preferences=preferences,
    )


def oracle(
    outcome: ExpectedOutcome = ExpectedOutcome.PURCHASE_AVAILABLE,
    value_minor: int | None = None,
) -> MissionOracle:
    """Ground truth, defaulting to the value the outcome requires.

    A caller that only wants to change the outcome does not also have to remember that a
    mission with no acceptable purchase is worth nothing.
    """
    if value_minor is None:
        value_minor = VALUE if outcome is ExpectedOutcome.PURCHASE_AVAILABLE else 0
    return MissionOracle(expected_outcome=outcome, simulated_value_amount_minor=value_minor)


def mission(
    key: str = "buy-a-charger",
    *,
    outcome: ExpectedOutcome = ExpectedOutcome.PURCHASE_AVAILABLE,
    value_minor: int | None = None,
    objective: str = "Buy one black charger",
    budget_minor: int = BUDGET,
    currency: str = CURRENCY,
    quantity: int = 1,
    constraints: tuple[HardConstraint, ...] = (BLACK,),
    preferences: tuple[Preference, ...] = (),
) -> BenchmarkMissionDefinition:
    """One mission, defaulting to a purchasable one worth `VALUE`."""
    return BenchmarkMissionDefinition(
        brief=brief(
            key,
            objective=objective,
            budget_minor=budget_minor,
            currency=currency,
            quantity=quantity,
            constraints=constraints,
            preferences=preferences,
        ),
        oracle=oracle(outcome, value_minor),
    )


def suite(
    *missions: BenchmarkMissionDefinition,
    key: str = "test-suite",
    version: int = 1,
    merchant_slug: str = MERCHANT_SLUG,
    name: str = "Test suite",
) -> BenchmarkSuiteDefinition:
    return BenchmarkSuiteDefinition(
        key=key,
        version=version,
        merchant_slug=merchant_slug,
        name=name,
        missions=missions or (mission(),),
    )


# A constraint that differs from BLACK only in its operator, for the identity tests: an
# operator that dropped out of the hash would otherwise be invisible.
NOT_BLACK = RequiredAttribute("color", "black", ConstraintOperator.NE)
