"""The merchant's explicit request to measure a published representation again.

Publishing an agent-ready representation does not launch anything. A benchmark run costs model
quota and real execution time, and a workflow that started one as a side effect of a publish
would be spending on the merchant's behalf without being asked. So the launch is its own
command, and this row is what that command writes.

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
shelf.

Everything methodology-critical is frozen at admission and immutable afterwards: the merchant,
the representation, the compiler run that produced it, the suite, the benchmark world, the buyer
profile and its frozen configuration digest, and the baseline run the result is to be compared
with. The browser supplies none of it. It names a representation and a request key, and the
server resolves the rest from the merchant its credential authenticated.
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


class ReevaluationStatus(StrEnum):
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


PENDING_STATUSES = frozenset({ReevaluationStatus.QUEUED, ReevaluationStatus.EXECUTING})
SETTLED_STATUSES = frozenset({ReevaluationStatus.COMPLETED, ReevaluationStatus.FAILED})


class BuyerProfile(StrEnum):
    """Which buyer a launch was admitted for, resolved from configuration and then frozen.

    AI_BUYER
        The isolated model buyer. It receives the pinned representation as its agent-ready
        discovery surface, so the representation is genuinely what is under test.

    REFERENCE_BUYER
        The isolated deterministic buyer. It reads structured commerce fields a storefront does
        not publish and has no language to misunderstand, so it never receives a discovery view
        and the run it produces pins no representation. Its result says whether the benchmark
        path works, and is not evidence about an autonomous agent or about a representation.
    """

    AI_BUYER = "AI_BUYER"
    REFERENCE_BUYER = "REFERENCE_BUYER"


REEVALUATION_STATUS = Enum(
    ReevaluationStatus,
    native_enum=False,
    create_constraint=False,
    validate_strings=True,
    length=16,
    name="benchmark_reevaluation_status",
)

BUYER_PROFILE = Enum(
    BuyerProfile,
    native_enum=False,
    create_constraint=False,
    validate_strings=True,
    length=24,
    name="benchmark_reevaluation_buyer_profile",
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

_STATUS_VALUES = ", ".join(f"'{status.value}'" for status in ReevaluationStatus)
_PROFILE_VALUES = ", ".join(f"'{profile.value}'" for profile in BuyerProfile)


class BenchmarkReevaluation(Base):
    """One merchant command to measure one published representation again."""

    __tablename__ = "benchmark_reevaluation"
    __table_args__ = (
        # The idempotency key. A repeated request with the same key is the same launch, and a
        # concurrent one loses here rather than creating a second run.
        UniqueConstraint("merchant_id", "request_key", name="uq_benchmark_reevaluation_request"),
        # One launch per run. A benchmark run is evidence and belongs to at most one launch.
        UniqueConstraint("run_id", name="uq_benchmark_reevaluation_run"),
        ForeignKeyConstraint(
            ["representation_id", "merchant_id"],
            ["commerce_representation.id", "commerce_representation.merchant_id"],
            name="fk_benchmark_reevaluation_representation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["compiler_run_id", "merchant_id"],
            ["compiler_run.id", "compiler_run.merchant_id"],
            name="fk_benchmark_reevaluation_compiler_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["environment_id", "merchant_id"],
            ["benchmark_environment.id", "benchmark_environment.merchant_id"],
            name="fk_benchmark_reevaluation_environment",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["run_id", "merchant_id"],
            ["benchmark_run.id", "benchmark_run.merchant_id"],
            name="fk_benchmark_reevaluation_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["baseline_run_id", "merchant_id"],
            ["benchmark_run.id", "benchmark_run.merchant_id"],
            name="fk_benchmark_reevaluation_baseline",
            ondelete="RESTRICT",
        ),
        CheckConstraint(f"status IN ({_STATUS_VALUES})", name="status_known"),
        CheckConstraint(f"buyer_profile IN ({_PROFILE_VALUES})", name="buyer_profile_known"),
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
            name="buyer_configuration_matches_profile",
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
        # One pending launch per merchant, structurally. A merchant owns one benchmark world, so
        # a second queued launch is two runs resetting each other's shelf rather than two
        # measurements. Frontend button state is not where this is decided.
        Index(
            "uq_benchmark_reevaluation_pending_merchant",
            "merchant_id",
            unique=True,
            postgresql_where=text(PENDING_PREDICATE),
        ),
        Index(None, "merchant_id"),
        Index(
            None,
            "representation_id",
            "merchant_id",
        ),
        Index(
            None,
            "compiler_run_id",
            "merchant_id",
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
    representation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    compiler_run_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
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
    status: Mapped[ReevaluationStatus] = mapped_column(REEVALUATION_STATUS, nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(MAX_FAILURE_CODE_LENGTH), nullable=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    # Frozen at admission. Which prior run this one is to be read against is a methodology
    # decision, and deciding it afterwards would let a reader choose the comparison that
    # flattered the result.
    baseline_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def is_pending(self) -> bool:
        return self.status in PENDING_STATUSES
