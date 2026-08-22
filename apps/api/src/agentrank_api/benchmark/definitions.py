"""What a benchmark suite and a benchmark mission are, before either is stored or run.

Pure domain code. No SQLAlchemy, no FastAPI, no clock and no model, so the same definition
always validates the same way and always hashes to the same identity.

The whole module is built around one separation, and it is the separation the rest of the
benchmark depends on being right:

```text
AgentMissionBrief    everything a buyer agent is allowed to see
MissionOracle        what the evaluator knows and the agent must not
```

A mission is the pair. They are two types rather than two conventions on one type, because
"do not hand the answer to the agent" is a rule that has to survive somebody adding a field
in a hurry. A brief that accidentally carried `expected_outcome` would quietly turn every
control mission into a giveaway, and nothing would fail.

The buyer facing half is written in the vocabulary that already exists. `MaxTotalAmount`,
`MaxQuantity`, `RequiredAttribute` and `AllowedCategory` come from
`agentrank_api.mandates.intent` and are the same objects a real buyer intent carries, so a
mission can be turned into a `BuyerIntent` rather than translated into one. There is
deliberately no second language for "black only" or "at most 5000 rupees".

Money follows the project rule: integer minor units, currency always beside the amount, and
never a float. The mission's currency is the currency of its budget, which is why exactly
one budget is required rather than optional.
"""

import re
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Self

from agentrank_api.mandates.intent import (
    MAX_DESCRIPTION_LENGTH,
    MAX_HARD_CONSTRAINTS,
    MAX_PREFERENCES,
    BuyerIntent,
    HardConstraint,
    MaxQuantity,
    MaxTotalAmount,
    Preference,
    RequiredAttribute,
    hard_constraint_from_payload,
)

# The same slug shape the catalog uses for a merchant. Suite keys, mission keys and the
# merchant a suite was authored against are all stable machine readable names that appear in
# a command line, a report and a database row, so they get one spelling and no capitals.
KEY_PATTERN = r"^[a-z0-9]+(-[a-z0-9]+)*$"

MAX_KEY_LENGTH = 64
MAX_NAME_LENGTH = 200

# A suite is a workload, not a corpus. The bound exists so that one definition cannot become
# an arbitrarily large document that has to be hashed, stored and reasoned about as a unit.
MAX_MISSIONS = 200

_KEY = re.compile(KEY_PATTERN)


class ExpectedOutcome(StrEnum):
    """What the merchant's authoritative data makes possible for one mission.

    This is a statement about the catalog, not a prediction about an agent. `PURCHASE_AVAILABLE`
    says a purchasable variant satisfying this mission within its budget exists; a competent
    buyer therefore should be able to complete it, and failing to is a finding about the
    merchant, the representation or the agent. `NO_ACCEPTABLE_PURCHASE` says none exists, so
    the correct behavior is to decline, and buying anything is a finding in the other
    direction.

    Two values, and there is deliberately no third for "it depends". A mission whose ground
    truth cannot be stated as one of these is a mission this benchmark cannot mark, and
    inventing a soft outcome would be inventing a fact nobody established.
    """

    PURCHASE_AVAILABLE = "PURCHASE_AVAILABLE"
    NO_ACCEPTABLE_PURCHASE = "NO_ACCEPTABLE_PURCHASE"


@dataclass(frozen=True, slots=True)
class AgentMissionBrief:
    """One buyer objective, as a buyer agent is allowed to see it.

    Everything here is something a real buyer would know about their own purchase: what they
    are trying to buy, how many, what they may spend, what they require and what they would
    prefer. Nothing here is knowledge about the merchant's catalog, and nothing here says
    whether the purchase is possible.

    `budget` is a `MaxTotalAmount` rather than a pair of numbers, so the ceiling is stated in
    the same type a `BuyerIntent` carries and a `SpendingMandate` is created from. It is a
    field of its own rather than one more entry in `hard_constraints` because every mission
    must have exactly one: it is what gives the mission a currency, and simulated GMV cannot
    be aggregated without one.

    `objective` is natural language and is here for a future buyer agent to read. Nothing
    deterministic is parsed out of it. Every fact the evaluator uses is one of the typed
    fields beside it, which is what keeps this benchmark free of a judge for facts the
    system already knows.

    `preferences` stay advisory, exactly as they are on an intent. A mission is never marked
    against a preference.
    """

    key: str
    objective: str
    budget: MaxTotalAmount
    quantity: int = 1
    hard_constraints: tuple[HardConstraint, ...] = ()
    preferences: tuple[Preference, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        validate_key(self.key, "mission key")
        if not self.objective.strip():
            raise ValueError("mission objective must not be blank")
        if len(self.objective) > MAX_DESCRIPTION_LENGTH:
            raise ValueError(
                f"mission objective must be at most {MAX_DESCRIPTION_LENGTH} characters"
            )
        if self.quantity <= 0:
            raise ValueError(f"mission quantity must be positive, got {self.quantity}")
        if self.budget.amount_minor <= 0:
            # Zero authorizes nothing. A mission with no money behind it can never succeed
            # and would sit in the potential GMV denominator forever.
            raise ValueError("a mission budget must be positive")
        if len(self.hard_constraints) > MAX_HARD_CONSTRAINTS:
            raise ValueError(f"at most {MAX_HARD_CONSTRAINTS} hard constraints are allowed")
        if len(self.preferences) > MAX_PREFERENCES:
            raise ValueError(f"at most {MAX_PREFERENCES} preferences are allowed")

        if any(isinstance(constraint, MaxTotalAmount) for constraint in self.hard_constraints):
            # The budget is the one ceiling on what this mission may cost, and a second one
            # in the constraint list is a ceiling that can disagree with it.
            raise ValueError(
                "a mission states its amount ceiling as its budget, not as a constraint"
            )
        if sum(isinstance(constraint, MaxQuantity) for constraint in self.hard_constraints) > 1:
            raise ValueError("a mission states at most one quantity ceiling")

        for constraint in self.hard_constraints:
            if isinstance(constraint, RequiredAttribute):
                _require_exact_values(constraint.value)

    @property
    def currency(self) -> str:
        """The one currency this mission is denominated in."""
        return self.budget.currency

    @property
    def max_quantity(self) -> int | None:
        """The stated quantity ceiling, or None when the buyer stated none.

        None means no limit. It does not mean zero and it does not mean `quantity`: a buyer
        who wants two units has not thereby forbidden a third, and treating the desired
        quantity as a ceiling would invent an authorization nobody granted.
        """
        for constraint in self.hard_constraints:
            if isinstance(constraint, MaxQuantity):
                return constraint.quantity
        return None

    def to_intent(self, merchant_id: uuid.UUID) -> BuyerIntent:
        """This brief as the buyer intent it describes.

        The budget rejoins the hard constraints here, in first position, because on an intent
        a financial ceiling is one more stated requirement. This is the reuse the benchmark
        exists to demonstrate: a mission is not translated into a different vocabulary, it is
        the same one with a merchant attached.
        """
        return BuyerIntent(
            merchant_id=merchant_id,
            description=self.objective,
            hard_constraints=(self.budget, *self.hard_constraints),
            preferences=self.preferences,
        )

    def to_payload(self) -> dict[str, Any]:
        """The JSON object this brief is stored and shown as.

        Every value in it is buyer facing. There is no oracle field here and no place to put
        one, which is the property `tests/test_benchmark_definitions.py` asserts against the
        serialized text rather than against the field list.
        """
        return {
            "key": self.key,
            "objective": self.objective,
            "quantity": self.quantity,
            "budget": self.budget.to_payload(),
            "hard_constraints": [constraint.to_payload() for constraint in self.hard_constraints],
            "preferences": [preference.statement for preference in self.preferences],
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Self:
        """Rebuild a brief from what `to_payload` wrote.

        Revalidating on the way in is the point. A row edited around this application still
        cannot become a brief that a mission would be marked against.
        """
        budget = hard_constraint_from_payload(payload["budget"])
        if not isinstance(budget, MaxTotalAmount):
            raise ValueError("a mission budget must be a max_total_amount constraint")
        return cls(
            key=payload["key"],
            objective=payload["objective"],
            budget=budget,
            quantity=payload["quantity"],
            hard_constraints=tuple(
                hard_constraint_from_payload(entry) for entry in payload["hard_constraints"]
            ),
            preferences=tuple(Preference(statement) for statement in payload["preferences"]),
        )


@dataclass(frozen=True, slots=True)
class MissionOracle:
    """What the evaluator knows about a mission and the agent never sees.

    Small, and it stays small. Everything the evaluator needs in order to decide whether a
    purchase satisfied the buyer is already in the brief, because the requirements the buyer
    stated are the requirements the purchase has to meet. What cannot be derived from the
    brief is whether the merchant can satisfy it at all, and what that sale would be worth.

    `simulated_value_amount_minor` is denominated in the brief's currency. It is simulated
    buyer demand, authored with the suite, and it is never revenue: no money moves in a
    benchmark run and none of it is a business result. See docs/benchmark.md.
    """

    expected_outcome: ExpectedOutcome
    simulated_value_amount_minor: int

    def __post_init__(self) -> None:
        if self.simulated_value_amount_minor < 0:
            raise ValueError("simulated value must not be negative")
        available = self.expected_outcome is ExpectedOutcome.PURCHASE_AVAILABLE
        if available and self.simulated_value_amount_minor <= 0:
            # A mission that a merchant could have served is worth something by definition,
            # and a zero would silently drop it out of the potential GMV it belongs in.
            raise ValueError("a mission with an available purchase must carry a positive value")
        if not available and self.simulated_value_amount_minor != 0:
            # There is no eligible demand when nothing acceptable is for sale, so counting a
            # value here would inflate potential GMV with a sale that could never happen.
            raise ValueError("a mission with no acceptable purchase carries no simulated value")

    def to_payload(self) -> dict[str, Any]:
        return {
            "expected_outcome": self.expected_outcome.value,
            "simulated_value_amount_minor": self.simulated_value_amount_minor,
        }


@dataclass(frozen=True, slots=True)
class BenchmarkMissionDefinition:
    """One mission: the half an agent reads and the half that marks it.

    The two halves are reachable only as themselves. There is no field on this object that
    flattens them together and no accessor that returns the oracle as part of the brief, so
    handing a mission to a future agent is a compile time mistake rather than a leak nobody
    notices.
    """

    brief: AgentMissionBrief
    oracle: MissionOracle

    def __post_init__(self) -> None:
        if self.oracle.simulated_value_amount_minor > self.brief.budget.amount_minor:
            # A sale cannot be worth more than the buyer was authorized to spend, so a value
            # above the budget is an authoring mistake that would inflate potential simulated
            # demand with money nobody could have paid. Checked here rather than on the oracle,
            # which does not see the budget, and restated as a check constraint on the table.
            raise ValueError(
                "a mission cannot be worth more than its budget:"
                f" {self.oracle.simulated_value_amount_minor} against"
                f" {self.brief.budget.amount_minor} {self.brief.currency}"
            )

    @property
    def key(self) -> str:
        """The mission's stable identity within its suite."""
        return self.brief.key

    @property
    def currency(self) -> str:
        return self.brief.currency

    def to_payload(self) -> dict[str, Any]:
        """Both halves, for storage and for the definition hash. Never for an agent."""
        return {"brief": self.brief.to_payload(), "oracle": self.oracle.to_payload()}


@dataclass(frozen=True, slots=True)
class BenchmarkSuiteDefinition:
    """A versioned, ordered collection of missions authored against one merchant.

    The version is part of the identity rather than a field that gets bumped in place. Two
    suites sharing a key and differing in version are two different workloads, and a result
    produced under one says nothing about the other. Comparing a before and an after means
    holding the key and the version fixed and changing only the merchant representation.

    `merchant_slug` is here because a mission's oracle is a statement about one merchant's
    catalog. The same missions run against a different merchant would be marked against
    ground truth that was never established there, so a run binds a suite to the merchant it
    was authored for and refuses any other.

    A suite holds definitions and no results. Nothing about a run, a status or a score
    appears here, which is what lets the definition be immutable once published.
    """

    key: str
    version: int
    merchant_slug: str
    name: str
    missions: tuple[BenchmarkMissionDefinition, ...]

    def __post_init__(self) -> None:
        validate_key(self.key, "suite key")
        validate_key(self.merchant_slug, "merchant slug")
        if self.version < 1:
            raise ValueError(f"suite version must be at least 1, got {self.version}")
        if not self.name.strip():
            raise ValueError("suite name must not be blank")
        if len(self.name) > MAX_NAME_LENGTH:
            raise ValueError(f"suite name must be at most {MAX_NAME_LENGTH} characters")
        if not self.missions:
            raise ValueError("a suite must hold at least one mission")
        if len(self.missions) > MAX_MISSIONS:
            raise ValueError(f"a suite may hold at most {MAX_MISSIONS} missions")

        keys = [mission.key for mission in self.missions]
        if len(set(keys)) != len(keys):
            # A mission key is how a result is attributed, so two missions sharing one would
            # make a historical result ambiguous about which mission it describes.
            raise ValueError("mission keys must be unique within a suite")

    @property
    def label(self) -> str:
        """How this suite is named in a report or on a command line."""
        return f"{self.key}@{self.version}"

    def mission(self, key: str) -> BenchmarkMissionDefinition:
        """One mission by key, raising rather than returning None.

        A missing mission is a caller asking about something this suite does not define, and
        answering with None would let a run record a result against nothing.
        """
        for mission in self.missions:
            if mission.key == key:
                return mission
        raise KeyError(f"suite {self.label} has no mission {key!r}")

    def briefs(self) -> tuple[AgentMissionBrief, ...]:
        """Every mission as an agent may see it, in suite order.

        The projection a future executor is handed. It is a tuple of briefs rather than of
        missions, so an executor cannot reach an oracle even by accident.
        """
        return tuple(mission.brief for mission in self.missions)


def _require_exact_values(value: object) -> None:
    """Refuse a constraint value that would not survive being stored and read back.

    A mission definition is written to JSONB and read back, and its content hash is what makes a
    historical run interpretable. PostgreSQL stores a JSON number as `numeric`, so a floating
    point value can come back as a different float than it went in as, which would change the
    hash of a definition nobody edited and break the one guarantee this whole model rests on.

    Whole numbers, text and booleans round trip exactly, and no commerce requirement this
    benchmark can express needs anything else: a wattage, a length and a port count are all
    integers. This is a restriction on what a mission may state, not a second vocabulary, and
    the values it does accept are the same `ConstraintValue` any buyer intent carries.
    """
    members = value if isinstance(value, tuple) else (value,)
    for member in members:
        if isinstance(member, float):
            raise ValueError(
                f"a mission constraint value must be a whole number, text or a boolean,"
                f" got the fractional value {member!r}"
            )


def validate_key(value: str, label: str) -> None:
    """Refuse anything that is not a lowercase hyphenated slug.

    Applied to suite keys, mission keys and the merchant slug a suite names. These identifiers
    end up in database rows, command line arguments and report headings, and one that differed
    only by capitalisation between two of those would be two identifiers.
    """
    if len(value) > MAX_KEY_LENGTH:
        raise ValueError(f"{label} must be at most {MAX_KEY_LENGTH} characters")
    if _KEY.fullmatch(value) is None:
        raise ValueError(f"{label} must match {KEY_PATTERN}, got {value!r}")
