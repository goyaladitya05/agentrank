"""Benchmark definition and run persistence.

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
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
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
from agentrank_api.benchmark.execution import ExecutorIdentity
from agentrank_api.benchmark.failures import FailureReason
from agentrank_api.benchmark.identity import HASH_LENGTH, HASH_PATTERN
from agentrank_api.benchmark.lifecycle import (
    TERMINAL_MISSION_STATUSES,
    TERMINAL_RUN_STATUSES,
    BenchmarkRunStatus,
    MissionRunStatus,
)
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


class BenchmarkEnvironment(Base):
    """One merchant registered as a benchmark world, prepared from one versioned fixture.

    This row is the answer to two separate questions, and it exists because neither of them
    had one.

    The first is production safety. Preparing a benchmark world overwrites a merchant's catalog
    and gives back the stock its missions were holding. That is exactly right for a fixture and
    catastrophic for a real merchant, so it is refused unless the merchant has been registered
    here. Registration is a deliberate act with a row behind it, and its absence is a refusal
    rather than a warning.

    The second is historical identity. A run pins its suite and its catalog hash, and neither
    says which authored world it was supposed to be measured against. `fixture_key`,
    `fixture_version` and `fixture_hash` do, and a run points at this row, so a report read a
    year later can say which target produced it.

    There is no `updated_at` and no status. Like a published suite, this has no lifecycle: it is
    written once and then only read, and the database refuses UPDATE and DELETE. Changing what a
    world contains means registering a new fixture version, which leaves every earlier run
    interpretable rather than rewriting what it was measured against.

    One key and version identify one world globally, not one per merchant. A fixture names the
    merchant it describes, exactly as a suite names the merchant it was authored against, so
    the same fixture version applied to two merchants would be two worlds claiming one identity.
    """

    __tablename__ = "benchmark_environment"
    __table_args__ = (
        # One world per key and version, globally. The reproducibility guarantee at the storage
        # layer: there is nowhere for a second `voltedge-catalog@1` to live, so a historical run
        # naming one cannot come to mean something else.
        UniqueConstraint("fixture_key", "fixture_version", name="uq_benchmark_environment_version"),
        # Redundant against the primary key, and present only as a composite foreign key target.
        # It is what makes a run's environment provably the run's own merchant rather than
        # somebody else's world with a plausible identifier.
        UniqueConstraint("id", "merchant_id", name="uq_benchmark_environment_binding"),
        CheckConstraint(f"fixture_key ~ '{KEY_PATTERN}'", name="fixture_key_format"),
        CheckConstraint("fixture_version > 0", name="fixture_version_positive"),
        CheckConstraint(f"fixture_hash ~ '{HASH_PATTERN}'", name="fixture_hash_format"),
        # The RESTRICT check when a merchant is deleted. Neither unique constraint above serves
        # it: one has the fixture key leftmost and the other has the identifier.
        Index(None, "merchant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid7)
    # RESTRICT. A registered benchmark world is a record about a merchant, and removing the
    # merchant would leave every run measured against it describing a target nobody can name.
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("merchant.id", ondelete="RESTRICT"),
        nullable=False,
    )
    fixture_key: Mapped[str] = mapped_column(String(MAX_KEY_LENGTH), nullable=False)
    fixture_version: Mapped[int] = mapped_column(Integer, nullable=False)
    fixture_hash: Mapped[str] = mapped_column(String(HASH_LENGTH), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    @property
    def label(self) -> str:
        return f"{self.fixture_key}@{self.fixture_version}"


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
        # Redundant against the primary key, and present only as a composite foreign key
        # target: a mission run is bound through (mission_id, suite_id), so it cannot attribute
        # a result to a mission belonging to a suite its run never executed.
        UniqueConstraint("id", "suite_id", name="uq_benchmark_mission_suite"),
        CheckConstraint(f"mission_key ~ '{KEY_PATTERN}'", name="mission_key_format"),
        CheckConstraint("ordinal >= 0", name="ordinal_not_negative"),
        CheckConstraint("length(btrim(objective)) > 0", name="objective_not_blank"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        # Positive rather than merely non negative. A mission with no money behind it can
        # never be completed and would sit in the potential GMV denominator forever.
        CheckConstraint("budget_amount_minor > 0", name="budget_positive"),
        # A sale cannot be worth more than the buyer was authorized to spend. Without this a
        # mission could carry a value nobody could ever have paid, and potential simulated
        # demand would be inflated by money that was never on the table.
        CheckConstraint(
            "simulated_value_amount_minor <= budget_amount_minor", name="value_within_budget"
        ),
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


# Stored as text with check constraints rather than as native PostgreSQL enums, for the same
# reason as every other enumeration in this schema.
BENCHMARK_RUN_STATUS = Enum(
    BenchmarkRunStatus,
    native_enum=False,
    create_constraint=False,
    validate_strings=True,
    length=16,
    name="benchmark_run_status",
)

MISSION_RUN_STATUS = Enum(
    MissionRunStatus,
    native_enum=False,
    create_constraint=False,
    validate_strings=True,
    length=16,
    name="benchmark_mission_run_status",
)

FAILURE_REASON = Enum(
    FailureReason,
    native_enum=False,
    create_constraint=False,
    validate_strings=True,
    length=32,
    name="benchmark_failure_reason",
)

_RUN_STATUS_VALUES = ", ".join(f"'{status.value}'" for status in BenchmarkRunStatus)
_MISSION_STATUS_VALUES = ", ".join(f"'{status.value}'" for status in MissionRunStatus)
_TERMINAL_VALUES = ", ".join(f"'{status.value}'" for status in sorted(TERMINAL_MISSION_STATUSES))
_REASON_VALUES = ", ".join(f"'{reason.value}'" for reason in FailureReason)
_REASON_JSON = ", ".join(f'"{reason.value}"' for reason in FailureReason)

MAX_REPRESENTATION_LABEL_LENGTH = 100


class BenchmarkRun(Base):
    """One execution of one suite against one merchant representation.

    Merchant owned, unlike the suite it executes. A run holds what happened when a specific
    merchant was measured, which is exactly the kind of thing Phase 1H made private.

    There is no `updated_at` and there are no aggregate count columns. Metrics are derived from
    the mission runs below rather than stored beside them, because a stored count is a count that
    can disagree with the rows it summarises, and the rows are already the answer. See
    docs/decisions.md.

    `representation_label` is a label and not an identity. The Merchant Compiler does not exist,
    so there is no content identity for a merchant representation to record, and this column must
    never be read as one: it holds whatever an operator called the representation, which is
    enough to tell a baseline run from a later one and is not evidence that anything changed.

    `catalog_hash` is the identity the label is not. It pins what the merchant's authoritative
    data looked like when the run started, which is the half of ground truth the suite hash
    cannot cover. Two runs whose pins differ were measured against different merchants, and any
    difference between them is jointly caused by whatever was changed on purpose and by whatever
    else moved at the same time. A before and after comparison across two different pins is not
    a controlled comparison and must not be presented as one.

    `evaluator_version` records the vocabulary and ordering the results were marked with, so
    that two runs marked by different rules are not compared as if they were not.

    Both are nullable, and null means the run predates the column rather than that everything
    was fine.
    """

    __tablename__ = "benchmark_run"
    __table_args__ = (
        # Redundant against the primary key, and present only as a composite foreign key
        # target. Carrying the suite as well as the merchant is what makes two invariants one
        # foreign key: a mission run cannot be attributed to another merchant, and it cannot
        # carry a result for a mission from a suite this run never executed.
        UniqueConstraint("id", "merchant_id", "suite_id", name="uq_benchmark_run_binding"),
        # The world this run was measured against, tied to this run's merchant. Nullable, and
        # PostgreSQL skips a composite foreign key when any of its columns is null, so a run
        # against an unregistered merchant simply has no environment to check. When there is
        # one it provably belongs to the same merchant, so knowing an environment identifier is
        # worth nothing to anybody else.
        ForeignKeyConstraint(
            ["environment_id", "merchant_id"],
            ["benchmark_environment.id", "benchmark_environment.merchant_id"],
            name="fk_benchmark_run_environment",
            ondelete="RESTRICT",
        ),
        CheckConstraint(f"status IN ({_RUN_STATUS_VALUES})", name="status_known"),
        CheckConstraint(
            "representation_label IS NULL OR length(btrim(representation_label)) > 0",
            name="representation_label_not_blank",
        ),
        CheckConstraint(
            f"catalog_hash IS NULL OR catalog_hash ~ '{HASH_PATTERN}'",
            name="catalog_hash_format",
        ),
        CheckConstraint(
            f"evaluator_version IS NULL OR evaluator_version ~ '{HASH_PATTERN}'",
            name="evaluator_version_format",
        ),
        # A kind without a version names a strategy nobody can pin down, and a version without a
        # kind names nothing at all. Either both or neither.
        CheckConstraint(
            "(executor_kind IS NULL) = (executor_version IS NULL)", name="executor_identity_shape"
        ),
        CheckConstraint(
            f"executor_kind IS NULL OR executor_kind ~ '{KEY_PATTERN}'",
            name="executor_kind_format",
        ),
        CheckConstraint(
            "executor_version IS NULL OR executor_version > 0", name="executor_version_positive"
        ),
        # A run that has not started has no start instant, and one that has finished has both.
        CheckConstraint("(status = 'PENDING') = (started_at IS NULL)", name="started_at_matches"),
        CheckConstraint(
            "(status IN ('COMPLETED', 'ABORTED')) = (completed_at IS NOT NULL)",
            name="completed_at_matches",
        ),
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= started_at", name="completion_after_start"
        ),
        # Merchant scoped reads and the RESTRICT check when a merchant is deleted. The ownership
        # constraint above has id leftmost, so it does not serve either.
        Index(None, "merchant_id"),
        Index(None, "suite_id"),
        # There is deliberately no index on environment_id. A registered environment cannot be
        # deleted, so no referential probe ever filters on it, and nothing reads runs by world.
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid7)
    # RESTRICT, not CASCADE. A measurement of a merchant is a record about that merchant and
    # must not disappear as a side effect of removing them.
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("merchant.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # RESTRICT for a stronger reason: a run whose suite was removed would describe a workload
    # nobody can read, which is the failure the whole definition side of this schema prevents.
    suite_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("benchmark_suite.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # The registered benchmark world this run was prepared against. Null means the run was not
    # executed against a registered world, which is what an ad hoc merchant in a test looks
    # like, and never that the target was fine.
    environment_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    representation_label: Mapped[str | None] = mapped_column(
        String(MAX_REPRESENTATION_LABEL_LENGTH), nullable=True
    )
    catalog_hash: Mapped[str | None] = mapped_column(String(HASH_LENGTH), nullable=True)
    evaluator_version: Mapped[str | None] = mapped_column(String(HASH_LENGTH), nullable=True)
    # Which strategy produced this run's results, and which version of it. Two columns rather
    # than one label, so a report can group by strategy and still tell two versions of it apart.
    # There is no model identifier and no provider beside them, because neither exists and a
    # column for one would be a guess at the shape of an agent that has not been built.
    executor_kind: Mapped[str | None] = mapped_column(String(MAX_KEY_LENGTH), nullable=True)
    executor_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # No server default, for the same reason as everywhere else in this schema: an insert that
    # does not state a status is a bug.
    status: Mapped[BenchmarkRunStatus] = mapped_column(BENCHMARK_RUN_STATUS, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    mission_runs: Mapped[list[BenchmarkMissionRun]] = relationship(
        back_populates="run",
        lazy="raise_on_sql",
        cascade="all, delete-orphan",
        # Version 7 identifiers are time ordered, so this is a stable order that is also the
        # order the rows were created in.
        order_by="BenchmarkMissionRun.id",
    )

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_RUN_STATUSES

    @property
    def executor_label(self) -> str | None:
        """How this run's executor is named in a report, or None when nobody recorded one.

        None is honest and is not the same as a default. A run with no executor identity was
        produced by something nobody wrote down, and a report has to say that rather than
        assuming which strategy it was.
        """
        if self.executor_kind is None or self.executor_version is None:
            return None
        return ExecutorIdentity(kind=self.executor_kind, version=self.executor_version).label


class BenchmarkMissionRun(Base):
    """What became of one mission in one run.

    Narrow on purpose. It carries the outcome, the classification, and identifiers for the real
    commerce rows the attempt produced. It does not carry an agent trace: traces will matter
    later and will reference this row's identifier, and designing the whole trace system now
    would be guessing at the shape of an agent that does not exist.

    The three commerce references are nullable composite foreign keys, each carrying
    `merchant_id`. That is what makes merchant isolation structural in both directions: a run for
    one merchant cannot record another merchant's variant, quote or payment, whatever a caller
    passes. PostgreSQL does not enforce a composite foreign key when any of its columns is null,
    which is exactly the behavior wanted here: a mission that never selected anything simply has
    no reference to check.

    Safety is three booleans rather than something derived at report time, because whether the
    executor tried to buy something it was not authorized to buy is a fact about the attempt, and
    the attempt is gone by the time a report is read. `unsafe_attempt` means the merchant's data
    proved the purchase was outside what the buyer authorized; `unverified_attempt` means the
    data did not say, so nothing could be established either way; `unsafe_completion` means money
    moved on a purchase that was not certified compliant. An escape implies one of the two
    attempt flags, and none of the three can sit on a succeeded mission, all at the database.
    """

    __tablename__ = "benchmark_mission_run"
    __table_args__ = (
        # Named explicitly. The convention would generate names longer than the 63 bytes
        # PostgreSQL keeps, so the name in the migration and the name in the database would
        # silently disagree.
        #
        # CASCADE, unlike everything else here. A mission run has no meaning without its run,
        # exactly as a checkout line has none without its quote.
        ForeignKeyConstraint(
            ["run_id", "merchant_id", "suite_id"],
            ["benchmark_run.id", "benchmark_run.merchant_id", "benchmark_run.suite_id"],
            name="fk_benchmark_mission_run_run",
            ondelete="CASCADE",
        ),
        # RESTRICT. The mission definition is what makes this row interpretable. Reached
        # through (mission_id, suite_id), which is the other half of the pair above: between
        # them, the mission this result names has to belong to the suite the run executed.
        # Without it a run could carry results for missions it never contained, and a report
        # would read an oracle from a workload nobody ran.
        ForeignKeyConstraint(
            ["mission_id", "suite_id"],
            ["benchmark_mission.id", "benchmark_mission.suite_id"],
            name="fk_benchmark_mission_run_mission",
            ondelete="RESTRICT",
        ),
        # The three commerce references, each tied to this row's merchant. RESTRICT on all
        # three: a benchmark result that points at a variant, a quote or a payment which no
        # longer exists is a hole in the record of what was measured.
        ForeignKeyConstraint(
            ["selected_variant_id", "merchant_id"],
            ["variant.id", "variant.merchant_id"],
            name="fk_benchmark_mission_run_variant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["checkout_id", "merchant_id"],
            ["checkout_session.id", "checkout_session.merchant_id"],
            name="fk_benchmark_mission_run_checkout",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["payment_attempt_id", "merchant_id"],
            ["payment_attempt.id", "payment_attempt.merchant_id"],
            name="fk_benchmark_mission_run_payment",
            ondelete="RESTRICT",
        ),
        # One result per mission per run. A run executes a suite once, and two results for one
        # mission would make the run's own arithmetic ambiguous.
        UniqueConstraint("run_id", "mission_id", name="uq_benchmark_mission_run_mission"),
        CheckConstraint(f"status IN ({_MISSION_STATUS_VALUES})", name="status_known"),
        CheckConstraint(
            f"primary_failure_reason IS NULL OR primary_failure_reason IN ({_REASON_VALUES})",
            name="failure_reason_known",
        ),
        CheckConstraint(
            "jsonb_typeof(additional_failure_reasons) = 'array'", name="additional_reasons_shape"
        ),
        # An array of the right shape is not an array of the right contents. Without this a row
        # could hold a reason nobody defined, or a number, and `failure_reasons` would raise
        # while reading a row that was already committed. Containment does the whole job: it is
        # a subset test over the known values and it rejects non string members outright.
        CheckConstraint(
            f"additional_failure_reasons <@ '[{_REASON_JSON}]'::jsonb",
            name="additional_reasons_known",
        ),
        # The primary reason is a column, so repeating it in the document would make
        # `failure_reasons` report it twice and a count by reason double count one mission.
        CheckConstraint(
            "primary_failure_reason IS NULL"
            " OR NOT (additional_failure_reasons @> to_jsonb(primary_failure_reason))",
            name="additional_reasons_exclude_primary",
        ),
        # Status and reason are separate facts and are still not free of each other. A success
        # with a reason and a failure without one are both incoherent, and a run that has not
        # produced an outcome has produced no reason either. ABSTAINED is the one status that
        # takes either, because a correct abstention has nothing to explain.
        CheckConstraint(
            "CASE status"
            " WHEN 'SUCCEEDED' THEN primary_failure_reason IS NULL"
            " WHEN 'FAILED' THEN primary_failure_reason IS NOT NULL"
            " WHEN 'ERRORED' THEN primary_failure_reason IS NULL"
            " WHEN 'PENDING' THEN primary_failure_reason IS NULL"
            " WHEN 'RUNNING' THEN primary_failure_reason IS NULL"
            " ELSE true END",
            name="failure_reason_matches_status",
        ),
        # Additional reasons qualify a primary one. Without a primary there is nothing for them
        # to be additional to.
        CheckConstraint(
            "primary_failure_reason IS NOT NULL"
            " OR jsonb_array_length(additional_failure_reasons) = 0",
            name="additional_reasons_need_a_primary",
        ),
        # A purchase that was not certified compliant is one of the two kinds of attempt that
        # could not be certified. There is no third source of an escape.
        CheckConstraint(
            "NOT unsafe_completion OR unsafe_attempt OR unverified_attempt",
            name="completion_implies_attempt",
        ),
        # None of the three can sit on a mission that succeeded: success requires compliance to
        # have been established, so an unsafe or unverified success would be the benchmark
        # contradicting its own definition of safe.
        CheckConstraint(
            "NOT (unsafe_attempt OR unverified_attempt) OR status <> 'SUCCEEDED'",
            name="unsafe_is_never_a_success",
        ),
        CheckConstraint("NOT unsafe_completion OR status = 'FAILED'", name="escape_is_a_failure"),
        # A selection is a variant and a count together. Half of one says nothing.
        CheckConstraint(
            "(selected_variant_id IS NULL) = (selected_quantity IS NULL)", name="selection_shape"
        ),
        CheckConstraint(
            "selected_quantity IS NULL OR selected_quantity > 0", name="quantity_positive"
        ),
        CheckConstraint("(status = 'PENDING') = (started_at IS NULL)", name="started_at_matches"),
        CheckConstraint(
            f"(status IN ({_TERMINAL_VALUES})) = (completed_at IS NOT NULL)",
            name="completed_at_matches",
        ),
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= started_at", name="completion_after_start"
        ),
        # The unique constraint above has run_id leftmost, so loading a run's results and the
        # cascade delete are both served. This covers the RESTRICT check when a mission
        # definition is deleted.
        Index(None, "mission_id"),
        # And these three cover the RESTRICT checks when a variant, a quote or a payment is
        # deleted. Without them PostgreSQL scans every mission run the merchant has ever
        # accumulated on each such delete, which was measured rather than assumed. Partial,
        # because most mission runs never reach a quote or a payment, and the nullable column
        # is leftmost because that is what the referential integrity probe filters on.
        Index(
            None,
            "selected_variant_id",
            "merchant_id",
            postgresql_where=text("selected_variant_id IS NOT NULL"),
        ),
        Index(
            None,
            "checkout_id",
            "merchant_id",
            postgresql_where=text("checkout_id IS NOT NULL"),
        ),
        Index(
            None,
            "payment_attempt_id",
            "merchant_id",
            postgresql_where=text("payment_attempt_id IS NOT NULL"),
        ),
        # There is deliberately no index on merchant_id alone. This table has no direct foreign
        # key to merchant, so no integrity check targets it, and every read reaches a mission
        # run through its run.
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid7)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    merchant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    # The suite both parents have to agree on. Denormalized from the run on purpose: a column
    # is the only thing a composite foreign key can compare, and this is what turns "a result
    # belongs to a mission of the suite that was run" from a hope into a constraint.
    suite_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    mission_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    status: Mapped[MissionRunStatus] = mapped_column(MISSION_RUN_STATUS, nullable=False)
    primary_failure_reason: Mapped[FailureReason | None] = mapped_column(
        FAILURE_REASON, nullable=True
    )
    # The rest of the reasons, in precedence order, never including the primary. A JSONB array
    # rather than a child table: these are read as a group with the row that owns them and are
    # never joined against on their own.
    additional_failure_reasons: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list
    )
    unsafe_attempt: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    unverified_attempt: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    unsafe_completion: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    # Whether the merchant's authoritative data still agreed with this mission's authored ground
    # truth when the mission ran. Null means nobody checked. False does not invalidate the
    # result and never changes the status: it says the oracle may be stale, which is a fact a
    # report has to be able to show rather than one the harness silently acts on.
    oracle_confirmed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    selected_variant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    selected_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checkout_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    payment_attempt_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    run: Mapped[BenchmarkRun] = relationship(back_populates="mission_runs", lazy="raise_on_sql")
    # Read only, and it has to be. `suite_id` sits in both composite foreign keys, so without
    # this SQLAlchemy would treat the mission as another writer of it and warn that two
    # relationships copy into one column. Nothing assigns a mission to a result: the mission is
    # chosen when the run is created, from the suite, and never afterwards.
    mission: Mapped[BenchmarkMission] = relationship(lazy="raise_on_sql", viewonly=True)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_MISSION_STATUSES

    @property
    def failure_reasons(self) -> tuple[FailureReason, ...]:
        """Every reason this mission run carries, primary first.

        Reassembled rather than stored twice. The primary is a column because a report groups by
        it and a check constraint has to see it; the rest are a document because they are only
        ever read alongside it.
        """
        if self.primary_failure_reason is None:
            return ()
        return (
            self.primary_failure_reason,
            *(FailureReason(reason) for reason in self.additional_failure_reasons),
        )
