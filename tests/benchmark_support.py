"""Builders for benchmark definitions used by more than one test file.

Not fixtures, because what each test needs varies and a fixture cannot be called twice with
different arguments in one test. Two suites differing in exactly one field is the shape most
of the identity tests are about.

Everything here goes through the real validating constructors, so a definition a test builds
is a definition the application would accept.
"""

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.benchmark.authored import publish_world, read_world
from agentrank_api.benchmark.definitions import (
    AgentMissionBrief,
    BenchmarkMissionDefinition,
    BenchmarkSuiteDefinition,
    ExpectedOutcome,
    MissionOracle,
)
from agentrank_api.benchmark.environment import PreparedEnvironment
from agentrank_api.benchmark.fixtures import BenchmarkFixture
from agentrank_api.benchmark.models import BenchmarkSuite
from agentrank_api.commerce.catalog_fixture import SeedProduct, SeedVariant
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


# The world the suites above are authored against. `run_suite` requires a fixture, because the
# whole point of the orchestrated path is that every mission observes a world somebody described
# rather than whatever was left behind, so a test that runs a suite has to say what that world is.
FIXTURE_KEY = "test-merchant-catalog"

BLACK_CHARGER = SeedVariant(
    sku="TEST-MERCHANT-BLACK",
    label="Black",
    price_amount_minor=VALUE,
    currency=CURRENCY,
    inventory_quantity=3,
    attributes={"color": "black"},
)


def fixture(
    *variants: SeedVariant,
    key: str = FIXTURE_KEY,
    version: int = 1,
    merchant_slug: str = MERCHANT_SLUG,
) -> BenchmarkFixture:
    """The catalog `build_shop` would have built, as an authored benchmark world."""
    return BenchmarkFixture(
        key=key,
        version=version,
        merchant_slug=merchant_slug,
        merchant_name=merchant_slug,
        products=(
            SeedProduct(
                external_id="test-merchant-1",
                title="Charger",
                description=None,
                category="chargers",
                variants=variants or (BLACK_CHARGER,),
            ),
        ),
    )


# The authored VoltEdge world, read from the operator files rather than imported from the
# package. Loaded once here so that every test asking about the reference benchmark asks about
# the same documents an operator command would read, and so that the path appears in one place.
#
# The tests are trusted operator side code and are allowed to read this. A benchmark worker is
# not, which is the whole reason the authored world lives outside the package it runs from. See
# tests/test_benchmark_oracle_isolation.py for what a worker can actually reach.
VOLTEDGE_DIRECTORY = Path(__file__).resolve().parent.parent / "benchmarks" / "voltedge"

VOLTEDGE = read_world(VOLTEDGE_DIRECTORY)


async def seed_voltedge(session: AsyncSession) -> tuple[PreparedEnvironment, BenchmarkSuite]:
    """Register, prepare and publish the authored VoltEdge world."""
    return await publish_world(session, VOLTEDGE)
