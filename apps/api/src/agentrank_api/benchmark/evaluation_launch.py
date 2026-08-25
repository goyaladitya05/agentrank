"""The merchant's explicit request to run one benchmark evaluation.

Nothing else starts one. Publishing an agent-ready representation writes an artifact and spends
nothing, and provisioning a merchant measures nothing at all. A benchmark run costs model quota
and real execution time, so asking for one is its own command, and this row is what that command
writes.

Two kinds of evaluation are admitted here and they are told apart by `purpose` rather than by
which table they landed in. A second launch record for first evaluations would be a second
account of the same thing: the same one-pending-launch rule, the same request key, the same
worker claim, the same settlement against the run it produced.

```text
INITIAL        measure the merchant's current merchant-facing state, before anything is
               compiled. Freezes the source snapshot that state is recorded in, pins no
               Commerce IR representation and no compiler run, and has no prior run to be
               read against
REEVALUATION   measure one published agent-ready representation again. Freezes that
               representation and the compiler run behind it, and freezes which prior run the
               result is to be compared with
```

The row is not a second copy of run truth. A benchmark run already records what was measured,
how it was marked and what became of every mission, and none of that is duplicated here. What
this holds is the three things a run cannot:

```text
launch identity      one request key per merchant, so a double submit is one launch
launch lifecycle     queued before any run exists, and settled when one cannot be produced
comparison identity  which prior run this launch is to be read against, frozen before it ran
```

The lifecycle is deliberately short. QUEUED means admitted and durable with nothing executed.
EXECUTING means exactly one benchmark run is bound and that run carries every execution fact.
COMPLETED and FAILED are the settled states, and a database trigger refuses either unless the
bound run agrees: COMPLETED needs a COMPLETED run, and FAILED with a run needs an ABORTED one.
Benchmark run statuses are one way, so agreement at the moment of writing is agreement forever,
and there is no stored status here that can drift away from the rows it describes.

At most one launch per merchant is pending, enforced by a partial unique index over the two
non-settled statuses rather than by an application check. That is the same invariant the run
table already holds for execution, applied one step earlier: a merchant owns one benchmark world,
so a second queued launch is not a second measurement, it is two runs resetting each other's
shelf. The index does not read `purpose`, so an initial evaluation and a re-evaluation cannot be
pending at once either.

An initial evaluation carries no baseline, and that is a check constraint rather than a rule
application code has to remember. A merchant's first evaluation has no before, and the one way
this schema could tell a merchant otherwise is by holding a prior run identifier on a row that
has no business naming one.

Everything methodology-critical is frozen at admission and immutable afterwards: the merchant,
the purpose, the measured artifact for that purpose, the suite, the benchmark world, the buyer
profile and its frozen configuration digest, and the baseline run the result is to be compared
with. The browser supplies none of it. It names a purpose, the artifact it was shown, a request
key and the digest of the preflight it read, and the server resolves the rest from the merchant
its credential authenticated.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
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
from sqlalchemy.orm import Mapped, mapped_column

from agentrank_api.benchmark.definitions import KEY_PATTERN, MAX_KEY_LENGTH
from agentrank_api.benchmark.identity import HASH_LENGTH, HASH_PATTERN
from agentrank_api.models import Base


class EvaluationLaunchStatus(StrEnum):
    """Where one launch command has got to, and nothing about how the run went.

    QUEUED
        Admitted and durable. No benchmark run exists, so nothing has been executed and nothing
        has been spent.

    EXECUTING
        One benchmark run is bound. That run is the only account of what is happening; this row
        says only that the launch produced it.

    COMPLETED
        The bound run reached COMPLETED. Written only when the run already agrees.

    FAILED
        The launch could not produce a finished run. `failure_code` says why in this
        repository's own vocabulary, never a provider's or an exception's words.
    """

    QUEUED = "QUEUED"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


PENDING_STATUSES = frozenset({EvaluationLaunchStatus.QUEUED, EvaluationLaunchStatus.EXECUTING})
SETTLED_STATUSES = frozenset({EvaluationLaunchStatus.COMPLETED, EvaluationLaunchStatus.FAILED})


class EvaluationPurpose(StrEnum):
    """Which kind of evaluation one launch admitted, decided by the server and then frozen.

    INITIAL
        Measure how the buyer does against this merchant's current merchant-facing state. The
        buyer is given the ordinary storefront discovery boundary and the merchant's own
        information as recorded in a source snapshot, so no Commerce IR representation is
        involved and the run it produces pins none. There is no prior run: this is admitted
        only while the merchant has no completed benchmark run at all.

    REEVALUATION
        Measure one published agent-ready representation again. The buyer is given that
        representation as its discovery surface, and the launch freezes which prior run the
        result is to be read against.

    The two are never the same measurement and are deliberately not merged into one. An initial
    evaluation is observational evidence about a merchant as they are; a re-evaluation is
    evidence about an artifact they published. Neither is a controlled experiment, which is a
    third thing again and lives in its own tables.
    """

    INITIAL = "INITIAL"
    REEVALUATION = "REEVALUATION"


class BuyerProfile(StrEnum):
    """Which buyer a launch was admitted for, resolved from configuration and then frozen.

    AI_BUYER
        The isolated model buyer. What it receives as its discovery surface is decided by the
        launch purpose: a re-evaluation gives it the pinned representation, so the artifact is
        genuinely what is under test, and an initial evaluation gives it the ordinary storefront
        and the merchant's own recorded information.

    REFERENCE_BUYER
        The isolated deterministic buyer. It reads structured commerce fields a storefront does
        not publish and has no language to misunderstand, so it never receives a discovery view
        at all and the run it produces pins no representation. Its result says whether the
        benchmark path works, and is not evidence about an autonomous agent, about a
        representation, or about how readable a merchant is.
    """

    AI_BUYER = "AI_BUYER"
    REFERENCE_BUYER = "REFERENCE_BUYER"


EVALUATION_LAUNCH_STATUS = Enum(
    EvaluationLaunchStatus,
    native_enum=False,
    create_constraint=False,
    validate_strings=True,
    length=16,
    name="benchmark_evaluation_launch_status",
)

EVALUATION_PURPOSE = Enum(
    EvaluationPurpose,
    native_enum=False,
    create_constraint=False,
    validate_strings=True,
    length=16,
    name="benchmark_evaluation_launch_purpose",
)

BUYER_PROFILE = Enum(
    BuyerProfile,
    native_enum=False,
    create_constraint=False,
    validate_strings=True,
    length=24,
    name="benchmark_evaluation_launch_buyer_profile",
)

# A launch is pending while it is neither completed nor failed. Static, because a partial index
# has to be, and it is the whole of "one pending launch per merchant".
PENDING_PREDICATE = "status IN ('QUEUED', 'EXECUTING')"

# The console generates one of these per rendered preflight, so re-submitting the same form is
# the same launch and opening the page again is a new one. Bounded and character restricted
# because it arrives from a browser and is stored.
REQUEST_KEY_PATTERN = r"^[0-9a-zA-Z_-]{8,64}$"
MAX_REQUEST_KEY_LENGTH = 64

MAX_FAILURE_CODE_LENGTH = 64

_STATUS_VALUES = ", ".join(f"'{status.value}'" for status in EvaluationLaunchStatus)
_PROFILE_VALUES = ", ".join(f"'{profile.value}'" for profile in BuyerProfile)
_PURPOSE_VALUES = ", ".join(f"'{purpose.value}'" for purpose in EvaluationPurpose)


class BenchmarkEvaluationLaunch(Base):
    """One merchant command to run one benchmark evaluation of their own merchant."""

    __tablename__ = "benchmark_evaluation_launch"
    __table_args__ = (
        # The idempotency key. A repeated request with the same key is the same launch, and a
        # concurrent one loses here rather than creating a second run.
        UniqueConstraint(
            "merchant_id", "request_key", name="uq_benchmark_evaluation_launch_request"
        ),
        # One launch per run. A benchmark run is evidence and belongs to at most one launch.
        UniqueConstraint("run_id", name="uq_benchmark_evaluation_launch_run"),
        ForeignKeyConstraint(
            ["representation_id", "merchant_id"],
            ["commerce_representation.id", "commerce_representation.merchant_id"],
            name="fk_benchmark_evaluation_launch_representation",
            ondelete="RESTRICT",
        ),
        # What an initial evaluation measures instead: the merchant's own information as it was
        # recorded when they asked. PostgreSQL skips a composite foreign key when any of its
        # columns is null, so a re-evaluation, which names no source, has nothing to check here.
        ForeignKeyConstraint(
            ["source_snapshot_id", "merchant_id"],
            ["merchant_source_snapshot.id", "merchant_source_snapshot.merchant_id"],
            name="fk_benchmark_evaluation_launch_source",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["compiler_run_id", "merchant_id"],
            ["compiler_run.id", "compiler_run.merchant_id"],
            name="fk_benchmark_evaluation_launch_compiler_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["environment_id", "merchant_id"],
            ["benchmark_environment.id", "benchmark_environment.merchant_id"],
            name="fk_benchmark_evaluation_launch_environment",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["run_id", "merchant_id"],
            ["benchmark_run.id", "benchmark_run.merchant_id"],
            name="fk_benchmark_evaluation_launch_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["baseline_run_id", "merchant_id"],
            ["benchmark_run.id", "benchmark_run.merchant_id"],
            name="fk_benchmark_evaluation_launch_baseline",
            ondelete="RESTRICT",
        ),
        CheckConstraint(f"status IN ({_STATUS_VALUES})", name="status_known"),
        CheckConstraint(f"purpose IN ({_PURPOSE_VALUES})", name="purpose_known"),
        CheckConstraint(f"buyer_profile IN ({_PROFILE_VALUES})", name="buyer_profile_known"),
        # Each purpose names exactly the artifact it measures and nothing else. An initial
        # evaluation that also named a representation would be claiming to have measured one,
        # and a re-evaluation that named a source snapshot would be claiming the storefront was
        # what the buyer read.
        CheckConstraint(
            "(purpose = 'INITIAL' AND representation_id IS NULL AND compiler_run_id IS NULL"
            " AND source_snapshot_id IS NOT NULL)"
            " OR (purpose = 'REEVALUATION' AND representation_id IS NOT NULL"
            " AND compiler_run_id IS NOT NULL AND source_snapshot_id IS NULL)",
            name="purpose_identity_shape",
        ),
        # A first evaluation has no before. Structural, so that the one place this schema could
        # tell a merchant otherwise cannot hold the value that would.
        CheckConstraint(
            "purpose <> 'INITIAL' OR baseline_run_id IS NULL", name="initial_has_no_baseline"
        ),
        CheckConstraint(f"request_key ~ '{REQUEST_KEY_PATTERN}'", name="request_key_format"),
        CheckConstraint(f"executor_kind ~ '{KEY_PATTERN}'", name="executor_kind_format"),
        # A queued launch has executed nothing and settled nothing.
        CheckConstraint(
            "status <> 'QUEUED' OR (run_id IS NULL AND started_at IS NULL"
            " AND settled_at IS NULL AND failure_code IS NULL)",
            name="queued_shape",
        ),
        # A launch that reached a run names it and says when.
        CheckConstraint(
            "status NOT IN ('EXECUTING', 'COMPLETED')"
            " OR (run_id IS NOT NULL AND started_at IS NOT NULL AND failure_code IS NULL)",
            name="executing_shape",
        ),
        CheckConstraint(
            "status <> 'EXECUTING' OR settled_at IS NULL", name="executing_is_not_settled"
        ),
        CheckConstraint(
            "(status IN ('COMPLETED', 'FAILED')) = (settled_at IS NOT NULL)", name="settled_shape"
        ),
        # Only a failure carries a reason, and every failure carries one.
        CheckConstraint("(status = 'FAILED') = (failure_code IS NOT NULL)", name="failure_shape"),
        CheckConstraint("run_id IS NULL OR started_at IS NOT NULL", name="run_needs_a_start"),
        CheckConstraint("started_at IS NULL OR started_at >= requested_at", name="start_order"),
        CheckConstraint("settled_at IS NULL OR settled_at >= requested_at", name="settle_order"),
        # All three instants come from the database's own clock, so this is a property the table
        # can hold rather than one that depends on which host wrote the row.
        CheckConstraint(
            "settled_at IS NULL OR started_at IS NULL OR settled_at >= started_at",
            name="settle_after_start",
        ),
        # A launch is never its own baseline.
        CheckConstraint(
            "baseline_run_id IS NULL OR run_id IS NULL OR baseline_run_id <> run_id",
            name="baseline_is_another_run",
        ),
        # The model buyer freezes a configuration; the reference buyer has none to freeze, and a
        # column holding one for it would be a configuration nothing reads.
        CheckConstraint(
            "(buyer_profile = 'AI_BUYER') = (buyer_configuration IS NOT NULL)",
            name="buyer_matches_profile",
        ),
        CheckConstraint(
            "(buyer_configuration IS NULL) = (buyer_configuration_digest IS NULL)",
            name="buyer_digest_shape",
        ),
        CheckConstraint(
            "buyer_configuration IS NULL OR jsonb_typeof(buyer_configuration) = 'object'",
            name="buyer_configuration_object",
        ),
        CheckConstraint(
            f"buyer_configuration_digest IS NULL OR buyer_configuration_digest ~ '{HASH_PATTERN}'",
            name="buyer_digest_format",
        ),
        # The execution budget is frozen with everything else the merchant was shown. Three
        # columns that arrive together or not at all: a launch either was admitted with an
        # allowance or predates the idea, and there is no half-stated budget in between.
        #
        # That a model buyer must have one is an insert rule on the guard trigger rather than a
        # check constraint, and the difference is history. Launches admitted before this phase
        # ran under no execution budget at all, and backfilling them with one this revision
        # invented would tell a reader they ran under a bound nothing enforced. They keep null,
        # which is the true statement, and every launch admitted from here carries a budget
        # because the trigger refuses an insert without one.
        CheckConstraint(
            "(max_provider_requests IS NULL) = (max_requests_per_mission IS NULL)"
            " AND (max_provider_requests IS NULL) = (execution_budget_version IS NULL)",
            name="budget_shape",
        ),
        CheckConstraint(
            "max_provider_requests IS NULL OR max_provider_requests >= 1",
            name="budget_total_positive",
        ),
        CheckConstraint(
            "max_requests_per_mission IS NULL OR max_requests_per_mission >= 1",
            name="budget_mission_positive",
        ),
        # One mission may never be allowed more than the whole launch is. A per-mission ceiling
        # above the total would be a bound that reads as a bound and is not one.
        CheckConstraint(
            "max_requests_per_mission IS NULL OR max_requests_per_mission <= max_provider_requests",
            name="budget_mission_within_total",
        ),
        CheckConstraint(
            "execution_budget_version IS NULL OR execution_budget_version >= 1",
            name="budget_version_positive",
        ),
        # The one unique the provider execution permit points at, so a permit can never name one
        # merchant's launch while claiming to belong to another's.
        UniqueConstraint("id", "merchant_id", name="uq_benchmark_evaluation_launch_tenant"),
        # One pending launch per merchant, structurally. A merchant owns one benchmark world, so
        # a second queued launch is two runs resetting each other's shelf rather than two
        # measurements. Frontend button state is not where this is decided.
        Index(
            "uq_benchmark_evaluation_launch_pending_merchant",
            "merchant_id",
            unique=True,
            postgresql_where=text(PENDING_PREDICATE),
        ),
        Index(None, "merchant_id"),
        # Partial, because the RESTRICT probe these serve only has anything to find on the
        # launches whose purpose names that artifact at all.
        Index(
            None,
            "representation_id",
            "merchant_id",
            postgresql_where=text("representation_id IS NOT NULL"),
        ),
        Index(
            None,
            "compiler_run_id",
            "merchant_id",
            postgresql_where=text("compiler_run_id IS NOT NULL"),
        ),
        Index(
            None,
            "source_snapshot_id",
            "merchant_id",
            postgresql_where=text("source_snapshot_id IS NOT NULL"),
        ),
        Index(None, "suite_id"),
        Index(None, "environment_id", "merchant_id"),
        # The RESTRICT probes when a benchmark run is deleted, and the read that resolves a
        # launch from the run it produced.
        Index(
            None,
            "baseline_run_id",
            "merchant_id",
            postgresql_where=text("baseline_run_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid7)
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("merchant.id", ondelete="RESTRICT"), nullable=False
    )
    request_key: Mapped[str] = mapped_column(String(MAX_REQUEST_KEY_LENGTH), nullable=False)
    purpose: Mapped[EvaluationPurpose] = mapped_column(EVALUATION_PURPOSE, nullable=False)
    # The measured artifact, and which of the three is present is decided by the purpose above
    # rather than by whatever a writer happened to pass. Null here is never "unknown": it is
    # the other purpose, and a check constraint is what makes that readable.
    representation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    compiler_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    source_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    suite_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("benchmark_suite.id", ondelete="RESTRICT"), nullable=False
    )
    environment_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    buyer_profile: Mapped[BuyerProfile] = mapped_column(BUYER_PROFILE, nullable=False)
    buyer_configuration: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    buyer_configuration_digest: Mapped[str | None] = mapped_column(
        String(HASH_LENGTH), nullable=True
    )
    executor_kind: Mapped[str] = mapped_column(String(MAX_KEY_LENGTH), nullable=False)
    # What this launch is allowed to spend at its provider, resolved by the server from the
    # capacity policy and then frozen. The browser never supplies these and cannot raise them:
    # the preflight digest covers them, admission recomputes them from the policy, and a
    # database trigger refuses to let them move afterwards. `execution_budget_version` is the
    # policy identity they were computed under, so widening the policy later leaves every
    # historical launch describing the allowance it actually ran with.
    execution_budget_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_provider_requests: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_requests_per_mission: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[EvaluationLaunchStatus] = mapped_column(EVALUATION_LAUNCH_STATUS, nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(MAX_FAILURE_CODE_LENGTH), nullable=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    # Frozen at admission. Which prior run this one is to be read against is a methodology
    # decision, and deciding it afterwards would let a reader choose the comparison that
    # flattered the result. Always null on an initial evaluation, and by check constraint
    # rather than by convention: a merchant's first evaluation has no before.
    baseline_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def is_pending(self) -> bool:
        return self.status in PENDING_STATUSES
