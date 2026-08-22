"""Benchmark definition persistence.

A published suite is the historical record of one workload. A run points at it, and a report
read a year later has to mean what it meant on the day it was produced, so these rows are
written once and never touched again.

Four properties are enforced by the database rather than by application code, because the
database is the only layer that cannot be bypassed:

- a suite key and version identify one suite, through a unique constraint. There is no way to
  have two definitions of `voltedge-core@1`
- a mission belongs to exactly one suite, and its key and its position within that suite are
  each unique, so a result attributed to a mission key is never ambiguous
- a mission's simulated value agrees with its expected outcome: an available purchase is worth
  something and an unavailable one is worth nothing, so potential GMV cannot be inflated by a
  sale that could never have happened
- neither table can be updated or deleted, so editing the source fixture cannot change what a
  historical run measured

The last one is a trigger rather than a constraint, because it is a rule about a transition
rather than about a row. See the migration for the statement itself.

Suites are global templates and are deliberately not merchant owned. There is no `merchant_id`
here and no foreign key to `merchant`: a suite is a workload definition that a run binds to a
merchant, and modelling it as merchant property would make the same workload a different
object for every merchant it is run against. `merchant_slug` records which merchant the
missions were authored against, because a mission oracle is a statement about one catalog, and
the run service refuses to run a suite against any other merchant. See docs/decisions.md.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from agentrank_api.benchmark.definitions import (
    KEY_PATTERN,
    MAX_KEY_LENGTH,
    MAX_NAME_LENGTH,
    AgentMissionBrief,
    BenchmarkMissionDefinition,
    BenchmarkSuiteDefinition,
    ExpectedOutcome,
    MissionOracle,
)
from agentrank_api.benchmark.identity import HASH_LENGTH, HASH_PATTERN
from agentrank_api.mandates.intent import MAX_DESCRIPTION_LENGTH
from agentrank_api.models import Base
from agentrank_api.money import CURRENCY_PATTERN

# Stored as text with a check constraint rather than as a native PostgreSQL enum, for the same
# reason as every other enumeration in this schema: adding a value should be an ordinary
# constraint change rather than an ALTER TYPE.
EXPECTED_OUTCOME = Enum(
    ExpectedOutcome,
    native_enum=False,
    create_constraint=False,
    validate_strings=True,
    length=24,
    name="benchmark_expected_outcome",
)

_OUTCOME_VALUES = ", ".join(f"'{outcome.value}'" for outcome in ExpectedOutcome)


class BenchmarkSuite(Base):
    """One published, versioned workload.

    There is no `updated_at` and no status. A published suite has no lifecycle: it is written
    once and then only read. Changing what a suite measures means publishing a new version,
    which leaves every earlier result interpretable rather than rewriting what it meant.

    `definition_hash` is the content identity of the missions below. It is what makes
    republishing a modified fixture under an existing version a refusal rather than a silent
    reinterpretation of history.
    """

    __tablename__ = "benchmark_suite"
    __table_args__ = (
        # One definition per key and version. This is the whole reproducibility guarantee at
        # the storage layer: there is nowhere for a second `voltedge-core@1` to live.
        UniqueConstraint("suite_key", "version", name="uq_benchmark_suite_version"),
        CheckConstraint(f"suite_key ~ '{KEY_PATTERN}'", name="suite_key_format"),
        CheckConstraint(f"merchant_slug ~ '{KEY_PATTERN}'", name="merchant_slug_format"),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint("length(btrim(name)) > 0", name="name_not_blank"),
        CheckConstraint(f"definition_hash ~ '{HASH_PATTERN}'", name="definition_hash_format"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid7)
    suite_key: Mapped[str] = mapped_column(String(MAX_KEY_LENGTH), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # The merchant these missions were authored against, as a slug rather than a foreign key.
    # A suite can be published before its merchant exists, and it stays valid after that
    # merchant is removed, because it is a description of a workload rather than of a row.
    merchant_slug: Mapped[str] = mapped_column(String(MAX_KEY_LENGTH), nullable=False)
    name: Mapped[str] = mapped_column(String(MAX_NAME_LENGTH), nullable=False)
    definition_hash: Mapped[str] = mapped_column(String(HASH_LENGTH), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    missions: Mapped[list[BenchmarkMission]] = relationship(
        back_populates="suite",
        lazy="raise_on_sql",
        cascade="all, delete-orphan",
        order_by="BenchmarkMission.ordinal",
    )

    @property
    def label(self) -> str:
        return f"{self.suite_key}@{self.version}"

    def to_definition(self) -> BenchmarkSuiteDefinition:
        """The validated domain form of this suite and every mission under it.

        Building the definition revalidates every row, so a suite that reached the tables
        around this application still cannot reach an evaluator misshapen. Reading requires
        `missions` to have been loaded, and `lazy="raise_on_sql"` makes an unloaded collection
        raise rather than quietly producing an empty suite.
        """
        return BenchmarkSuiteDefinition(
            key=self.suite_key,
            version=self.version,
            merchant_slug=self.merchant_slug,
            name=self.name,
            missions=tuple(mission.to_definition() for mission in self.missions),
        )


class BenchmarkMission(Base):
    """One buyer objective and its ground truth, as one row.

    The buyer facing half and the evaluator's half are separate columns rather than one
    document, so the separation is visible in the schema and a projection that returns the
    wrong set of columns is a mistake somebody can see in a diff.

    `hard_constraints` and `preferences` are JSONB because they are genuinely heterogeneous:
    the value inside a required attribute is text, a number or a list, exactly as it is on the
    authoritative `intent_constraint` table. Everything else is a column, so the shape of a
    mission is readable in SQL and can be constrained.
    """

    __tablename__ = "benchmark_mission"
    __table_args__ = (
        # A mission key is how a result is attributed, so one suite cannot define two.
        UniqueConstraint("suite_id", "mission_key", name="uq_benchmark_mission_key"),
        # And one position cannot be held twice, so suite order is total. Ordering is what
        # makes a rerun present the same workload in the same sequence.
        UniqueConstraint("suite_id", "ordinal", name="uq_benchmark_mission_ordinal"),
        CheckConstraint(f"mission_key ~ '{KEY_PATTERN}'", name="mission_key_format"),
        CheckConstraint("ordinal >= 0", name="ordinal_not_negative"),
        CheckConstraint("length(btrim(objective)) > 0", name="objective_not_blank"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        # Positive rather than merely non negative. A mission with no money behind it can
        # never be completed and would sit in the potential GMV denominator forever.
        CheckConstraint("budget_amount_minor > 0", name="budget_positive"),
        CheckConstraint(f"currency ~ '{CURRENCY_PATTERN}'", name="currency_format"),
        CheckConstraint("jsonb_typeof(hard_constraints) = 'array'", name="hard_constraints_shape"),
        CheckConstraint("jsonb_typeof(preferences) = 'array'", name="preferences_shape"),
        CheckConstraint(f"expected_outcome IN ({_OUTCOME_VALUES})", name="expected_outcome_known"),
        # The one rule that keeps simulated GMV honest at the storage layer. A mission the
        # merchant could have served is worth something; a mission nothing acceptable exists
        # for is worth nothing, and a row claiming otherwise would inflate potential GMV with
        # a sale that could never have happened.
        CheckConstraint(
            "CASE expected_outcome"
            " WHEN 'PURCHASE_AVAILABLE' THEN simulated_value_amount_minor > 0"
            " WHEN 'NO_ACCEPTABLE_PURCHASE' THEN simulated_value_amount_minor = 0"
            " ELSE false END",
            name="simulated_value_matches_outcome",
        ),
        # The composite unique constraints above both have suite_id leftmost, so a lookup by
        # suite and the cascade delete are already served and no further index is needed.
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid7)
    # RESTRICT rather than CASCADE. A published definition is history, and removing a suite
    # that a run points at would leave that run describing a workload nobody can read.
    suite_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("benchmark_suite.id", ondelete="RESTRICT"),
        nullable=False,
    )
    mission_key: Mapped[str] = mapped_column(String(MAX_KEY_LENGTH), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)

    # Buyer facing. Everything from here to `preferences` is what a future agent is shown.
    objective: Mapped[str] = mapped_column(String(MAX_DESCRIPTION_LENGTH), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    budget_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    hard_constraints: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list
    )
    preferences: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list
    )

    # Oracle. Neither of these two may ever be handed to a buyer agent. They are here rather
    # than in a JSON document precisely so that the boundary is a column list.
    expected_outcome: Mapped[ExpectedOutcome] = mapped_column(EXPECTED_OUTCOME, nullable=False)
    simulated_value_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    suite: Mapped[BenchmarkSuite] = relationship(back_populates="missions", lazy="raise_on_sql")

    def to_brief(self) -> AgentMissionBrief:
        """This mission as a buyer agent may see it.

        The only projection an executor is ever given. It reads the buyer facing columns and
        cannot read the two oracle columns, because it does not name them.
        """
        return AgentMissionBrief.from_payload(
            {
                "key": self.mission_key,
                "objective": self.objective,
                "quantity": self.quantity,
                "budget": {
                    "kind": "max_total_amount",
                    "amount_minor": self.budget_amount_minor,
                    "currency": self.currency,
                },
                "hard_constraints": list(self.hard_constraints),
                "preferences": list(self.preferences),
            }
        )

    def to_definition(self) -> BenchmarkMissionDefinition:
        """This mission as the evaluator sees it, which is the brief plus the ground truth."""
        return BenchmarkMissionDefinition(
            brief=self.to_brief(),
            oracle=MissionOracle(
                expected_outcome=self.expected_outcome,
                simulated_value_amount_minor=self.simulated_value_amount_minor,
            ),
        )
