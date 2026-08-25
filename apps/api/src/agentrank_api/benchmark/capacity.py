"""What one deployment is allowed to spend at a model provider, and what it has spent.

Two tables and the arithmetic that connects them.

`ProviderCapacityPolicy` is an operator's statement about one provider: whether AgentRank may
call it at all, how many evaluations may be in flight against it at once, how large an
allowance one evaluation is admitted with, and an optional windowed ceiling over the whole
deployment. Nothing here encodes a vendor's published quota. Provider limits differ by account
and change without notice, so a number baked into this repository would be a guess that outlived
whoever made it; what is baked in is a conservative default an operator can widen deliberately.

`ProviderExecutionPermit` is what was actually reserved. One row per mission, written before the
process that makes the calls exists, and it is the only thing that answers "may another provider
call be paid for". A permit is reserved for a whole mission rather than for a single call because
the process that makes the calls has no database: it is handed the number it was granted and
cannot exceed it, and this row is what that number was charged against.

The charge is a stored generated column rather than something application code computes on read,
and the reason is the whole safety property of this phase:

```text
RESERVED        the full grant is charged. The worker may be calling the provider right now
ASSUMED_SPENT   the full grant stays charged. The worker's outcome is unknown, and unknown
                consumption is never zero
RECONCILED      the requests the worker reported are charged, and the rest is released
RELEASED        nothing is charged, and this is written only where no call could have happened
```

Only `RESERVED` may move, and it may move once. A permit that has been assumed spent cannot
later be reconciled down to a smaller number, because reconciling it down would be restoring
allowance for a request that may already have reached the provider and been paid for. That is
enforced by a trigger rather than by application care, so a recovery path written next year
cannot undo it by being written in the wrong order.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
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
from sqlalchemy.orm import Mapped, mapped_column

from agentrank_api.benchmark.definitions import KEY_PATTERN, MAX_KEY_LENGTH
from agentrank_api.models import Base

MAX_ATTEMPT_KEY_LENGTH = 128

# A window an operator can reason about without arithmetic, and the one every deployment cap in
# this repository is expressed over unless an operator says otherwise.
DEFAULT_WINDOW_SECONDS = 86_400

# The policy identity a deployment that has configured nothing runs under. It is AgentRank's own
# conservatism rather than a claim about any provider's account limits: one evaluation at a time,
# an evaluation allowance half again as large as its missions strictly need, a per-mission
# ceiling of twice the configured model turns, and no deployment ceiling until an operator sets
# one. An operator who wants any of it different writes a policy row, which carries its own
# version and leaves every launch admitted before it exactly as it was.
DEFAULT_POLICY_VERSION = 1
DEFAULT_MAX_CONCURRENT_LAUNCHES = 1
DEFAULT_MISSION_REQUEST_MULTIPLIER = 2
DEFAULT_LAUNCH_RETRY_ALLOWANCE_PERCENT = 50


class PermitState(StrEnum):
    """Where one mission's reserved provider requests have got to.

    RESERVED
        Written and committed before the worker process was started. The full grant is charged
        and the worker may be talking to the provider at this instant.

    RECONCILED
        The worker exited cleanly and reported how many requests it made. Only the reported
        number stays charged, and the rest of the grant is available again.

    ASSUMED_SPENT
        The worker's outcome is unknown: it timed out, it died, it produced a report nobody
        could read, or the process holding its lease disappeared. The full grant stays charged
        forever. Overcounting an ambiguous request is the safe direction for a spending bound,
        and the state name is what makes the overcount visible rather than silent.

    RELEASED
        Nothing was charged, and this is written only where trusted evidence establishes that
        no provider call could have been made: the worker refused its environment or the request
        before reading a mission at all, or the process could never be started.
    """

    RESERVED = "RESERVED"
    RECONCILED = "RECONCILED"
    ASSUMED_SPENT = "ASSUMED_SPENT"
    RELEASED = "RELEASED"


OPEN_STATES = frozenset({PermitState.RESERVED})
SETTLED_STATES = frozenset(
    {PermitState.RECONCILED, PermitState.ASSUMED_SPENT, PermitState.RELEASED}
)

PERMIT_STATE = Enum(
    PermitState,
    native_enum=False,
    create_constraint=False,
    validate_strings=True,
    length=16,
    name="provider_execution_permit_state",
)

_PERMIT_STATE_VALUES = ", ".join(f"'{state.value}'" for state in PermitState)

# What each state charges, as a column the database computes rather than a rule application code
# applies on read. A sum over this is the authoritative answer to "what has this launch spent",
# and there is no second implementation of the rule for it to disagree with.
#
# The `::text` casts are written out because PostgreSQL stores them in the generation expression
# it keeps, and `alembic check` compares the two texts after only crude normalisation. Without
# them the model and the schema describe the same column and the drift check reports a
# difference it cannot act on, which is a false alarm nobody can fix by editing a migration.
CHARGE_EXPRESSION = (
    "CASE state"
    " WHEN 'RECONCILED'::text THEN consumed_requests"
    " WHEN 'RELEASED'::text THEN 0"
    " ELSE granted_requests"
    " END"
)


class ProviderCapacityPolicy(Base):
    """One operator statement about what may be spent at one model provider."""

    __tablename__ = "provider_capacity_policy"
    __table_args__ = (
        UniqueConstraint("provider", name="uq_provider_capacity_policy_provider"),
        CheckConstraint(f"provider ~ '{KEY_PATTERN}'", name="provider_format"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("max_concurrent_launches >= 1", name="concurrency_positive"),
        CheckConstraint("mission_request_multiplier >= 1", name="mission_multiplier_positive"),
        CheckConstraint(
            "launch_retry_allowance_percent >= 0 AND launch_retry_allowance_percent <= 1000",
            name="retry_allowance_bounded",
        ),
        # Null is "no deployment ceiling", which is a different statement from a ceiling of zero.
        # Zero would be a provider nobody may call, and `enabled` is how that is said.
        CheckConstraint(
            "max_requests_per_window IS NULL OR max_requests_per_window >= 1",
            name="window_cap_positive",
        ),
        CheckConstraint("window_seconds >= 60", name="window_is_meaningful"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid7)
    provider: Mapped[str] = mapped_column(String(MAX_KEY_LENGTH), nullable=False)
    # Bumped on every operator change. A launch freezes the version it was admitted under, so
    # widening or narrowing a policy afterwards leaves every historical launch saying exactly
    # what it was admitted with rather than what the policy says today.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=DEFAULT_POLICY_VERSION)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    max_concurrent_launches: Mapped[int] = mapped_column(Integer, nullable=False)
    mission_request_multiplier: Mapped[int] = mapped_column(Integer, nullable=False)
    launch_retry_allowance_percent: Mapped[int] = mapped_column(Integer, nullable=False)
    max_requests_per_window: Mapped[int | None] = mapped_column(Integer, nullable=True)
    window_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_WINDOW_SECONDS
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ProviderExecutionPermit(Base):
    """One mission's reserved provider requests, and what became of them."""

    __tablename__ = "provider_execution_permit"
    __table_args__ = (
        # The idempotency key. A trusted caller that lost the database's answer and reserved
        # again for the same intended attempt gets the permit it already has rather than a
        # second grant against the same run.
        UniqueConstraint("run_id", "attempt_key", name="uq_provider_execution_permit_attempt"),
        ForeignKeyConstraint(
            ["launch_id", "merchant_id"],
            ["benchmark_evaluation_launch.id", "benchmark_evaluation_launch.merchant_id"],
            name="fk_provider_execution_permit_launch",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["run_id", "merchant_id"],
            ["benchmark_run.id", "benchmark_run.merchant_id"],
            name="fk_provider_execution_permit_run",
            ondelete="RESTRICT",
        ),
        CheckConstraint(f"state IN ({_PERMIT_STATE_VALUES})", name="state_known"),
        CheckConstraint(f"provider ~ '{KEY_PATTERN}'", name="provider_format"),
        CheckConstraint("granted_requests >= 1", name="grant_positive"),
        CheckConstraint("attempt >= 1", name="attempt_positive"),
        CheckConstraint("policy_version >= 1", name="policy_version_positive"),
        # Only a reconciled permit says how many requests were actually made, and every
        # reconciled one says. A reserved permit's consumption is not zero, it is unknown, and
        # a column holding zero for it would be the fake zero this phase exists to prevent.
        CheckConstraint(
            "(state = 'RECONCILED') = (consumed_requests IS NOT NULL)", name="consumption_shape"
        ),
        CheckConstraint(
            "consumed_requests IS NULL"
            " OR (consumed_requests >= 0 AND consumed_requests <= granted_requests)",
            name="consumption_bounded",
        ),
        CheckConstraint("(state <> 'RESERVED') = (closed_at IS NOT NULL)", name="closed_shape"),
        CheckConstraint("closed_at IS NULL OR closed_at >= opened_at", name="close_after_open"),
        # The window sum, which runs on every reservation while a policy has a deployment cap.
        Index(None, "provider", "opened_at"),
        # The run sum, which runs on every reservation, and the launch read behind a merchant
        # asking what their own evaluation spent.
        Index(None, "run_id"),
        Index(None, "launch_id", postgresql_where=text("launch_id IS NOT NULL")),
        # A merchant reading their own execution usage, and the RESTRICT probe on a run.
        Index(None, "merchant_id", "opened_at"),
        Index(None, "run_id", "merchant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid7)
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("merchant.id", ondelete="RESTRICT"), nullable=False
    )
    # Null for a run an operator started directly, which spends the same quota and is charged
    # the same way. Never null for a run a merchant's launch produced.
    launch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    mission_key: Mapped[str] = mapped_column(String(MAX_KEY_LENGTH), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    attempt_key: Mapped[str] = mapped_column(String(MAX_ATTEMPT_KEY_LENGTH), nullable=False)
    provider: Mapped[str] = mapped_column(String(MAX_KEY_LENGTH), nullable=False)
    requested_model: Mapped[str] = mapped_column(String(128), nullable=False)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    granted_requests: Mapped[int] = mapped_column(Integer, nullable=False)
    consumed_requests: Mapped[int | None] = mapped_column(Integer, nullable=True)
    charged_requests: Mapped[int] = mapped_column(
        Integer, Computed(CHARGE_EXPRESSION, persisted=True), nullable=False
    )
    state: Mapped[PermitState] = mapped_column(PERMIT_STATE, nullable=False)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


@dataclass(frozen=True, slots=True)
class CapacityPolicy:
    """One provider's capacity policy as trusted code reads it, row or default.

    A value rather than the row, so the default a deployment that configured nothing runs under
    and a policy an operator wrote are the same thing to every caller. What differs between them
    is the version, and the version is what a launch freezes.
    """

    provider: str
    version: int
    enabled: bool
    max_concurrent_launches: int
    mission_request_multiplier: int
    launch_retry_allowance_percent: int
    max_requests_per_window: int | None
    window_seconds: int
    configured: bool

    @classmethod
    def default_for(cls, provider: str) -> CapacityPolicy:
        return cls(
            provider=provider,
            version=DEFAULT_POLICY_VERSION,
            enabled=True,
            max_concurrent_launches=DEFAULT_MAX_CONCURRENT_LAUNCHES,
            mission_request_multiplier=DEFAULT_MISSION_REQUEST_MULTIPLIER,
            launch_retry_allowance_percent=DEFAULT_LAUNCH_RETRY_ALLOWANCE_PERCENT,
            max_requests_per_window=None,
            window_seconds=DEFAULT_WINDOW_SECONDS,
            configured=False,
        )

    @classmethod
    def of(cls, row: ProviderCapacityPolicy) -> CapacityPolicy:
        return cls(
            provider=row.provider,
            version=row.version,
            enabled=row.enabled,
            max_concurrent_launches=row.max_concurrent_launches,
            mission_request_multiplier=row.mission_request_multiplier,
            launch_retry_allowance_percent=row.launch_retry_allowance_percent,
            max_requests_per_window=row.max_requests_per_window,
            window_seconds=row.window_seconds,
            configured=True,
        )


@dataclass(frozen=True, slots=True)
class ExecutionBudget:
    """What one evaluation launch is allowed to spend at a provider, frozen at admission.

    Two numbers and the identity that produced them.

    `max_provider_requests` is the whole launch's ceiling and the one a merchant is shown. It is
    every mission's turns plus a retry allowance, because a retry is a provider request and
    presenting a merchant with a number that only counted first attempts would understate the
    ceiling by exactly the amount a throttled provider costs.

    `max_requests_per_mission` is how much of that ceiling one mission may take. It exists
    because the process making the calls has no database: it is handed this number and cannot
    exceed it, so one pathological mission cannot drain the launch before the others run.
    """

    policy_version: int
    mission_count: int
    max_model_turns: int
    max_provider_requests: int
    max_requests_per_mission: int


def frozen_budget(
    policy: CapacityPolicy, *, mission_count: int, max_model_turns: int
) -> ExecutionBudget:
    """The execution budget one launch would be admitted with, computed once and then frozen.

    A pure function so the preflight a merchant reads and the row admission writes are the same
    arithmetic rather than two implementations that could drift. Integer arithmetic throughout,
    rounded up, so the allowance is never quietly smaller than the percentage says.
    """
    if mission_count < 1 or max_model_turns < 1:
        raise ValueError("an execution budget needs at least one mission and one model turn")
    base = mission_count * max_model_turns
    total = (base * (100 + policy.launch_retry_allowance_percent) + 99) // 100
    per_mission = max_model_turns * policy.mission_request_multiplier
    return ExecutionBudget(
        policy_version=policy.version,
        mission_count=mission_count,
        max_model_turns=max_model_turns,
        max_provider_requests=max(total, base),
        max_requests_per_mission=min(max(per_mission, max_model_turns), max(total, base)),
    )
