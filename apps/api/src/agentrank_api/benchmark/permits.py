"""Deciding whether AgentRank may pay for another model provider call, and recording that it did.

This is the trusted boundary the whole of Phase 5D rests on. Every live provider call this
system makes happens inside a worker process that has no database, so the process making the
call cannot be the thing that decides whether the call is affordable. What decides is here, it
decides before the process exists, and it commits its decision before anything leaves the
machine.

The order matters and it is the only ordering that is safe:

```text
reserve a permit and commit it          durable, and outside any long transaction
start the worker process                the network call happens here, with no database
read what the worker reported           trusted code, from a process that has already exited
reconcile or assume the grant spent     one write, and it can never restore an unknown request
```

No provider call is made inside a database transaction, and no database transaction is held
across one. The reservation opens and closes before the subprocess is spawned, and the
reconciliation opens after it has exited.

Two coordination questions are answered here and they are answered differently on purpose.

Provider concurrency is read off the launch table rather than held in a lease of its own. A
launch that is EXECUTING is a launch with a run against a merchant's world, its missions are
sequential by construction, and its provider follows from the executor kind it froze. So "how
many evaluations are calling this provider right now" is a count of rows that already exist,
with an existing operator recovery path when a dispatcher dies holding one. Inventing a second
lease table would have created a second answer to that question and a second thing to leak.

Provider spending is its own table, because nothing else records it. A permit is charged before
the call and settled after it, and the settlement can only ever move the charge down to a number
the worker reported. When the worker's outcome is unknown the full grant stays charged for good.

Spending is accounted per benchmark run rather than per merchant launch, because a launch is not
the only thing that spends. An operator executing one sample of a controlled experiment makes the
same provider calls with the same money, and an accounting that only knew about launches would
have left that path charged to nothing. Every provider call happens inside a run, so the run is
where the ledger balances.

Both checks run under one transaction-scoped advisory lock keyed by provider name. Per provider
rather than global, so an OpenAI reservation never waits on a Gemini one, and advisory rather
than a row lock so a deployment that has configured no policy still serializes correctly.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, NoReturn

from sqlalchemy import Integer, Select, case, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentrank_api.benchmark.capacity import (
    CapacityPolicy,
    ExecutionBudget,
    PermitState,
    ProviderCapacityPolicy,
    ProviderExecutionPermit,
)
from agentrank_api.benchmark.evaluation_launch import (
    BenchmarkEvaluationLaunch,
    EvaluationLaunchStatus,
)
from agentrank_api.benchmark.llm import SUPPORTED_PROVIDERS, executor_kind_for
from agentrank_api.benchmark.models import AgentProviderUsage
from agentrank_api.errors import ConflictError, NotFoundError

RESOURCE = "provider_execution_permit"

# A fixed namespace for the transaction-scoped advisory lock every reservation takes, so the
# provider name is the only thing that decides which reservations wait for each other. Chosen
# once and written down rather than derived, because two different constants would silently
# stop coordinating.
CAPACITY_LOCK_NAMESPACE = 5140


class ExecutionWaitReason(StrEnum):
    """Why provider work is not proceeding, in AgentRank's own words.

    Every one of these is a fact about this deployment's execution governance rather than about
    a merchant's catalog or a provider's health, and that distinction is the point of naming
    them at all. A merchant whose evaluation is waiting because an operator paused the provider
    must never be shown a provider outage, and a launch that stopped because its allowance ran
    out must never be shown as an HTTP 429.

    PROVIDER_PAUSED
        An operator disabled execution for this provider. Nothing is broken and nothing is
        lost; no new call is admitted until it is enabled again.

    PROVIDER_CAPACITY_OCCUPIED
        As many evaluations are already running against this provider as the policy allows.
        Ordinary contention, and the work stays queued.

    PROVIDER_WINDOW_CAP_REACHED
        The deployment has charged its configured ceiling of provider requests within the
        policy's rolling window. An operator's own safety cap rather than anything a provider
        said.

    LAUNCH_BUDGET_EXHAUSTED
        This launch has charged the whole execution allowance it was admitted with. Retries
        count towards that allowance, so this is what retry amplification looks like when it
        reaches the bound.
    """

    PROVIDER_PAUSED = "PROVIDER_PAUSED"
    PROVIDER_CAPACITY_OCCUPIED = "PROVIDER_CAPACITY_OCCUPIED"
    PROVIDER_WINDOW_CAP_REACHED = "PROVIDER_WINDOW_CAP_REACHED"
    LAUNCH_BUDGET_EXHAUSTED = "LAUNCH_BUDGET_EXHAUSTED"


# How a launch is settled when execution governance stopped it, in the same vocabulary the rest
# of the launch failure codes use. Never a provider's words and never an exception's text.
LAUNCH_FAILURE_CODES: dict[ExecutionWaitReason, str] = {
    ExecutionWaitReason.PROVIDER_PAUSED: "provider_execution_paused",
    ExecutionWaitReason.PROVIDER_CAPACITY_OCCUPIED: "provider_capacity_unavailable",
    ExecutionWaitReason.PROVIDER_WINDOW_CAP_REACHED: "provider_window_cap_reached",
    ExecutionWaitReason.LAUNCH_BUDGET_EXHAUSTED: "provider_budget_exhausted",
}

# What a merchant is told when a launch settled for each of them. Sentences about AgentRank's
# own governance, because that is whose decision each one was.
WAIT_SENTENCES: dict[ExecutionWaitReason, str] = {
    ExecutionWaitReason.PROVIDER_PAUSED: (
        "AgentRank paused model execution for this provider, so no model request was made."
    ),
    ExecutionWaitReason.PROVIDER_CAPACITY_OCCUPIED: (
        "AgentRank runs a limited number of evaluations against this provider at once, and the"
        " others were still running."
    ),
    ExecutionWaitReason.PROVIDER_WINDOW_CAP_REACHED: (
        "AgentRank reached its own deployment ceiling for model requests, so it stopped rather"
        " than making more."
    ),
    ExecutionWaitReason.LAUNCH_BUDGET_EXHAUSTED: (
        "This evaluation used the whole model request allowance it was launched with, including"
        " the requests that were retried, so it stopped rather than making more."
    ),
}


class ProviderExecutionHaltedError(Exception):
    """AgentRank declined to make another provider call, and why.

    Raised where the decision is made rather than diagnosed afterwards from a state, so a caller
    that has to abort a run and settle a launch has the reason in its hands. It is deliberately
    not a `ConflictError`: nothing conflicted, and the existing conflict handling in the
    dispatcher means something quite different.
    """

    def __init__(self, reason: ExecutionWaitReason, detail: str | None = None) -> None:
        super().__init__(detail or WAIT_SENTENCES[reason])
        self.reason = reason
        self.failure_code = LAUNCH_FAILURE_CODES[reason]
        self.detail = detail or WAIT_SENTENCES[reason]


@dataclass(frozen=True, slots=True)
class ProviderGrant:
    """One mission's reserved provider requests, as the trusted side hands them on.

    Identifiers and a number. The worker receives only the number; the permit identifier stays
    on this side, because a worker that could name its own permit could be asked to settle it.
    """

    permit_id: uuid.UUID
    granted_requests: int
    provider: str
    requested_model: str


@dataclass(frozen=True, slots=True)
class ProviderCapacityStatus:
    """What an operator reads about one provider without opening a database client."""

    provider: str
    policy: CapacityPolicy
    executing_launches: int
    open_permits: int
    requests_charged_in_window: int
    window_remaining: int | None
    admits_new_work: bool
    wait_reason: ExecutionWaitReason | None


@dataclass(frozen=True, slots=True)
class LaunchProviderUsage:
    """What one launch actually consumed, measured rather than estimated.

    Provider request attempts and provider-reported tokens are kept apart and never added
    together. An attempt is something AgentRank observed itself, before the call; a token count
    is something the provider chose to report, and Gemini has historically reported none. A
    single combined number would be two different kinds of evidence in one field, and the
    weaker kind would silently decide it.

    `unknown_usage_invocations` is how many provider responses carried no token counts at all.
    It is reported rather than folded into a zero, because those requests happened and cost
    whatever they cost.
    """

    provider: str | None
    requested_model: str | None
    max_provider_requests: int | None
    permits: int
    permits_open: int
    permits_reconciled: int
    permits_assumed_spent: int
    permits_released: int
    requests_charged: int
    requests_reconciled: int
    requests_assumed_spent: int
    requests_remaining: int | None
    provider_responses: int
    unknown_usage_invocations: int
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None

    @property
    def has_ambiguous_consumption(self) -> bool:
        """Whether any charge here is an assumption rather than a measurement."""
        return self.permits_assumed_spent > 0


class ProviderExecutionService:
    """The one path a provider call is authorized through, and the one that records it."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # Policy, which an operator reads and writes and nothing else does.

    async def policy(self, provider: str) -> CapacityPolicy:
        """This provider's configured policy, or the conservative default it runs under.

        A default rather than a refusal, because a deployment that has configured nothing is an
        ordinary state and one that could not run an evaluation until somebody wrote a row would
        be a worse one. The default is narrow enough to be safe and is reported as unconfigured
        so an operator can tell it from a policy somebody chose.
        """
        _require_supported(provider)
        row = await self._policy_row(provider)
        return CapacityPolicy.default_for(provider) if row is None else CapacityPolicy.of(row)

    async def policies(self) -> list[CapacityPolicy]:
        """Every supported provider's policy, configured or default, in a stable order."""
        rows = {
            row.provider: row
            for row in (await self._session.execute(select(ProviderCapacityPolicy))).scalars()
        }
        return [
            CapacityPolicy.of(rows[provider])
            if provider in rows
            else CapacityPolicy.default_for(provider)
            for provider in sorted(SUPPORTED_PROVIDERS)
        ]

    async def set_policy(
        self,
        provider: str,
        *,
        enabled: bool | None = None,
        max_concurrent_launches: int | None = None,
        mission_request_multiplier: int | None = None,
        launch_retry_allowance_percent: int | None = None,
        max_requests_per_window: int | None = None,
        clear_window_cap: bool = False,
        window_seconds: int | None = None,
    ) -> CapacityPolicy:
        """Write this provider's policy, bumping the version every time it changes.

        The version is what a launch freezes, so bumping it on every write is what keeps a
        historical launch describing the allowance it was actually admitted with rather than
        whatever the policy says today. Nothing here rewrites a launch, and nothing here can:
        the launch's budget columns are frozen by a database trigger.
        """
        _require_supported(provider)
        row = await self._policy_row(provider, lock=True)
        current = CapacityPolicy.default_for(provider) if row is None else CapacityPolicy.of(row)
        window_cap = (
            None
            if clear_window_cap
            else (
                current.max_requests_per_window
                if max_requests_per_window is None
                else max_requests_per_window
            )
        )
        values = {
            "enabled": current.enabled if enabled is None else enabled,
            "max_concurrent_launches": (
                current.max_concurrent_launches
                if max_concurrent_launches is None
                else max_concurrent_launches
            ),
            "mission_request_multiplier": (
                current.mission_request_multiplier
                if mission_request_multiplier is None
                else mission_request_multiplier
            ),
            "launch_retry_allowance_percent": (
                current.launch_retry_allowance_percent
                if launch_retry_allowance_percent is None
                else launch_retry_allowance_percent
            ),
            "max_requests_per_window": window_cap,
            "window_seconds": (
                current.window_seconds if window_seconds is None else window_seconds
            ),
        }
        if row is None:
            row = ProviderCapacityPolicy(provider=provider, version=current.version + 1, **values)
            self._session.add(row)
        else:
            row.version = row.version + 1
            for name, value in values.items():
                setattr(row, name, value)
            row.updated_at = await self._clock()
        await self._session.commit()
        await self._session.refresh(row)
        return CapacityPolicy.of(row)

    async def status(self, provider: str) -> ProviderCapacityStatus:
        """One provider's policy and what is currently running against it.

        A report, so nothing is locked and nothing is written. It answers the question an
        operator has when a merchant says nothing is happening, and it answers it from the same
        rows the reservation reads rather than from a monitoring copy that could disagree.
        """
        policy = await self.policy(provider)
        executing = await self._executing_launches(provider)
        charged = await self._window_charge(provider, policy.window_seconds)
        open_permits = (
            await self._session.scalar(
                select(func.count())
                .select_from(ProviderExecutionPermit)
                .where(
                    ProviderExecutionPermit.provider == provider,
                    ProviderExecutionPermit.state == PermitState.RESERVED,
                )
            )
        ) or 0
        remaining = (
            None
            if policy.max_requests_per_window is None
            else max(0, policy.max_requests_per_window - charged)
        )
        reason: ExecutionWaitReason | None = None
        if not policy.enabled:
            reason = ExecutionWaitReason.PROVIDER_PAUSED
        elif executing >= policy.max_concurrent_launches:
            reason = ExecutionWaitReason.PROVIDER_CAPACITY_OCCUPIED
        elif remaining is not None and remaining == 0:
            reason = ExecutionWaitReason.PROVIDER_WINDOW_CAP_REACHED
        return ProviderCapacityStatus(
            provider=provider,
            policy=policy,
            executing_launches=executing,
            open_permits=open_permits,
            requests_charged_in_window=charged,
            window_remaining=remaining,
            admits_new_work=reason is None,
            wait_reason=reason,
        )

    # Admission and reservation, which are the only things that authorize a provider call.

    async def admit_launch(self, provider: str, *, excluding: uuid.UUID) -> None:
        """Refuse to start another evaluation against a provider that cannot take one.

        Called inside the transaction that moves a launch to EXECUTING, under this provider's
        advisory lock, so two dispatchers binding two merchants' launches at the same instant
        cannot both believe they hold the last free slot. `excluding` is the launch being
        admitted: it may already be EXECUTING by the time this runs, and counting it against
        itself would make a concurrency of one admit nothing.

        The launch budget is not checked here. A launch that has spent nothing cannot be
        exhausted, and the check that matters runs per mission where the spending happens.

        This raises without rolling back, unlike the reservation below, and the asymmetry is
        deliberate. A reservation owns its transaction and lets go of the provider lock on the
        way out; this one is a gate inside somebody else's, so ending that transaction here
        would discard a caller's work rather than release a lock. The caller rolls back, which
        releases the lock with it.
        """
        _require_supported(provider)
        await self._lock(provider)
        policy = await self.policy(provider)
        if not policy.enabled:
            raise ProviderExecutionHaltedError(ExecutionWaitReason.PROVIDER_PAUSED)
        if await self._executing_launches(provider, excluding=excluding) >= (
            policy.max_concurrent_launches
        ):
            raise ProviderExecutionHaltedError(ExecutionWaitReason.PROVIDER_CAPACITY_OCCUPIED)
        if policy.max_requests_per_window is not None:
            charged = await self._window_charge(provider, policy.window_seconds)
            if charged >= policy.max_requests_per_window:
                raise ProviderExecutionHaltedError(ExecutionWaitReason.PROVIDER_WINDOW_CAP_REACHED)

    async def reserve(
        self,
        *,
        merchant_id: uuid.UUID,
        launch_id: uuid.UUID | None,
        run_id: uuid.UUID,
        mission_key: str,
        attempt: int,
        provider: str,
        requested_model: str,
        budget: ExecutionBudget,
    ) -> ProviderGrant:
        """Reserve one mission's provider requests durably, before any process can spend them.

        Committed before it returns, which is the property the rest of this phase depends on:
        the worker that may reach the provider is started afterwards, so a crash between the two
        leaves a reserved permit charged rather than a spent request nobody recorded.

        Idempotent on the attempt key. A trusted caller that lost the database's answer and
        reserved again for the same intended attempt receives the permit it already holds
        instead of a second grant, so AgentRank's own retries cannot double-reserve one mission.

        The grant is the smallest of what the mission may take, what the launch has left and
        what the deployment window has left, and the floor under all three is one request per
        model turn. A mission that cannot be given that is not the mission the merchant was
        shown, so execution halts instead of running a crippled one.
        """
        _require_supported(provider)
        attempt_key = permit_attempt_key(run_id, mission_key, attempt)
        await self._lock(provider)
        existing = await self._by_attempt(run_id, attempt_key)
        if existing is not None:
            if existing.state is not PermitState.RESERVED:
                raise ConflictError(
                    "permit_already_settled",
                    f"provider execution permit {existing.id} is {existing.state.value}",
                    resource=RESOURCE,
                    identifier=str(existing.id),
                )
            await self._session.commit()
            return _grant(existing)
        policy = await self.policy(provider)
        floor = budget.max_model_turns
        allowed = [budget.max_requests_per_mission]
        # Every halt below releases this provider's lock before it leaves, so a caller that
        # handles the refusal and goes on using its session is not holding a lock every other
        # reservation for this provider would wait behind.
        if not policy.enabled:
            await self._halt(ExecutionWaitReason.PROVIDER_PAUSED)
        launch_remaining = budget.max_provider_requests - await self._run_charge(run_id)
        if launch_remaining < floor:
            await self._halt(ExecutionWaitReason.LAUNCH_BUDGET_EXHAUSTED)
        allowed.append(launch_remaining)
        if policy.max_requests_per_window is not None:
            window_remaining = policy.max_requests_per_window - await self._window_charge(
                provider, policy.window_seconds
            )
            if window_remaining < floor:
                await self._halt(ExecutionWaitReason.PROVIDER_WINDOW_CAP_REACHED)
            allowed.append(window_remaining)
        granted = min(allowed)
        statement = (
            insert(ProviderExecutionPermit)
            .values(
                id=uuid.uuid7(),
                merchant_id=merchant_id,
                launch_id=launch_id,
                run_id=run_id,
                mission_key=mission_key,
                attempt=attempt,
                attempt_key=attempt_key,
                provider=provider,
                requested_model=requested_model,
                policy_version=budget.policy_version,
                granted_requests=granted,
                state=PermitState.RESERVED,
            )
            .on_conflict_do_nothing(constraint="uq_provider_execution_permit_attempt")
            .returning(ProviderExecutionPermit.id)
        )
        permit_id = await self._session.scalar(statement)
        await self._session.commit()
        if permit_id is None:
            # Another trusted caller reserved this same intended attempt between the read above
            # and this insert. The unique constraint is what made that safe rather than a second
            # grant, and the permit it wrote is the one this mission runs under.
            raced = await self._by_attempt(run_id, attempt_key)
            if raced is None or raced.state is not PermitState.RESERVED:
                raise ConflictError(
                    "permit_unavailable",
                    "another process holds this mission's provider permit",
                    resource=RESOURCE,
                    identifier=attempt_key,
                )
            return _grant(raced)
        return ProviderGrant(
            permit_id=permit_id,
            granted_requests=granted,
            provider=provider,
            requested_model=requested_model,
        )

    # Settlement, which can lower a charge to a measurement and can never raise an unknown one.

    async def reconcile(self, permit_id: uuid.UUID, *, consumed_requests: int) -> None:
        """Settle a permit against what the worker process reported it actually spent.

        Only from RESERVED, and the number is clamped to the grant rather than trusted blindly:
        the allowance object in the worker cannot let more through than it was given, so a
        larger number is a defect on this side and charging the grant is the safe reading of it.
        """
        permit = await self._reservable(permit_id)
        if permit is None:
            return
        permit.consumed_requests = max(0, min(consumed_requests, permit.granted_requests))
        permit.state = PermitState.RECONCILED
        permit.closed_at = await self._clock()
        await self._session.commit()

    async def assume_spent(self, permit_id: uuid.UUID) -> None:
        """Settle a permit whose worker outcome is unknown, charging the whole grant for good.

        The conservative direction, and the one this system takes whenever it cannot establish
        what happened: a process that died after its worker may have reached the provider has
        possibly spent every request it was granted, and restoring any of that allowance would
        be AgentRank telling itself a paid request was free.
        """
        permit = await self._reservable(permit_id)
        if permit is None:
            return
        permit.state = PermitState.ASSUMED_SPENT
        permit.closed_at = await self._clock()
        await self._session.commit()

    async def release(self, permit_id: uuid.UUID) -> None:
        """Settle a permit where trusted evidence establishes that no call could have happened.

        Written only from the two worker exits that occur before a mission is read at all, and
        from a process that could not be started, because those are the only outcomes where this
        side knows the provider was never reached. Everything else is assumed spent.
        """
        permit = await self._reservable(permit_id)
        if permit is None:
            return
        permit.state = PermitState.RELEASED
        permit.closed_at = await self._clock()
        await self._session.commit()

    # Evidence.

    async def launch_usage(self, launch_id: uuid.UUID) -> LaunchProviderUsage:
        """What one launch reserved, spent, and had reported back to it about tokens."""
        launch = await self._session.get(BenchmarkEvaluationLaunch, launch_id)
        if launch is None:
            raise NotFoundError("benchmark_evaluation_launch", str(launch_id))
        return (await self.launch_usages([launch]))[launch_id]

    async def launch_usages(
        self, launches: Sequence[BenchmarkEvaluationLaunch]
    ) -> dict[uuid.UUID, LaunchProviderUsage]:
        """One page of launches' execution evidence, read in two grouped queries.

        Batched rather than looped because a list of launches is a page, and a per-launch read
        would be a query per row of it. One implementation of the arithmetic either way: the
        single-launch answer is this with a list of one.
        """
        if not launches:
            return {}
        runs = [launch.run_id for launch in launches if launch.run_id is not None]
        permits = await self._permit_aggregates(runs)
        tokens = await self._usage_aggregates(runs)
        return {
            launch.id: _launch_usage(
                launch,
                {} if launch.run_id is None else permits.get(launch.run_id, {}),
                tokens.get(launch.run_id) if launch.run_id is not None else None,
            )
            for launch in launches
        }

    async def _permit_aggregates(
        self, run_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, dict[PermitState, _PermitTotals]]:
        if not run_ids:
            return {}
        rows = (
            await self._session.execute(
                select(
                    ProviderExecutionPermit.run_id,
                    ProviderExecutionPermit.state,
                    func.count().label("permits"),
                    func.coalesce(func.sum(ProviderExecutionPermit.charged_requests), 0).label(
                        "charged"
                    ),
                    func.coalesce(func.sum(ProviderExecutionPermit.granted_requests), 0).label(
                        "granted"
                    ),
                    func.coalesce(func.sum(ProviderExecutionPermit.consumed_requests), 0).label(
                        "consumed"
                    ),
                    func.min(ProviderExecutionPermit.provider).label("provider"),
                    func.min(ProviderExecutionPermit.requested_model).label("model"),
                )
                .where(ProviderExecutionPermit.run_id.in_(list(run_ids)))
                .group_by(ProviderExecutionPermit.run_id, ProviderExecutionPermit.state)
            )
        ).all()
        totals: dict[uuid.UUID, dict[PermitState, _PermitTotals]] = {}
        for row in rows:
            totals.setdefault(row.run_id, {})[PermitState(row.state)] = _PermitTotals(
                permits=int(row.permits),
                charged=int(row.charged),
                granted=int(row.granted),
                consumed=int(row.consumed),
                provider=row.provider,
                requested_model=row.model,
            )
        return totals

    async def _usage_aggregates(
        self, run_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, _TokenTotals]:
        """Provider-reported tokens per run, with unknown staying unknown in the database.

        The `CASE` around each sum is the whole point. A run where one response reported no
        input tokens has an unknown input total, not a total that quietly omits it, and deciding
        that here rather than in Python means no caller can sum these rows a second way.
        """
        if not run_ids:
            return {}
        totals = {
            name: case(
                (
                    func.count() == func.count(column),
                    func.coalesce(func.sum(column), 0).cast(Integer),
                ),
                else_=None,
            ).label(name)
            for name, column in (
                ("input_tokens", AgentProviderUsage.input_tokens),
                ("output_tokens", AgentProviderUsage.output_tokens),
                ("total_tokens", AgentProviderUsage.total_tokens),
            )
        }
        rows = (
            await self._session.execute(
                select(
                    AgentProviderUsage.run_id,
                    func.count().label("responses"),
                    func.count()
                    .filter(
                        AgentProviderUsage.input_tokens.is_(None),
                        AgentProviderUsage.output_tokens.is_(None),
                        AgentProviderUsage.total_tokens.is_(None),
                    )
                    .label("unknown"),
                    *totals.values(),
                )
                .where(AgentProviderUsage.run_id.in_(list(run_ids)))
                .group_by(AgentProviderUsage.run_id)
            )
        ).all()
        return {
            row.run_id: _TokenTotals(
                responses=int(row.responses),
                unknown=int(row.unknown),
                input_tokens=row.input_tokens,
                output_tokens=row.output_tokens,
                total_tokens=row.total_tokens,
            )
            for row in rows
        }

    # Internals.

    async def _halt(self, reason: ExecutionWaitReason) -> NoReturn:
        """Refuse, having first let go of the provider lock this reservation was holding.

        The rollback is what makes the refusal cheap for everybody else. A transaction-scoped
        advisory lock ends with its transaction whatever happens, so a caller that abandons its
        session releases it anyway; a caller that catches the refusal and carries on would
        otherwise hold every other reservation for this provider until it happened to commit.
        """
        await self._session.rollback()
        raise ProviderExecutionHaltedError(reason)

    async def _lock(self, provider: str) -> None:
        """Serialize this provider's admissions and reservations for this transaction.

        A transaction-scoped advisory lock rather than a row lock, because a deployment that has
        written no policy row has no row to lock and would otherwise coordinate on nothing. It
        is released when the transaction ends, whichever way it ends, so a caller that raises
        does not strand it.
        """
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(:namespace, hashtext(:provider))"),
            {"namespace": CAPACITY_LOCK_NAMESPACE, "provider": provider},
        )

    async def _policy_row(
        self, provider: str, *, lock: bool = False
    ) -> ProviderCapacityPolicy | None:
        statement = select(ProviderCapacityPolicy).where(
            ProviderCapacityPolicy.provider == provider
        )
        if lock:
            statement = statement.with_for_update()
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def _executing_launches(
        self, provider: str, *, excluding: uuid.UUID | None = None
    ) -> int:
        """How many evaluations are currently running against this provider.

        Read off the launch table rather than a lease of its own. An executing launch is one
        benchmark run whose missions are sequential, so it is exactly one provider conversation
        at a time, and the executor kind it froze names the provider without a second column
        that could disagree with it.
        """
        statement = (
            select(func.count())
            .select_from(BenchmarkEvaluationLaunch)
            .where(
                BenchmarkEvaluationLaunch.status == EvaluationLaunchStatus.EXECUTING,
                BenchmarkEvaluationLaunch.executor_kind == executor_kind_for(provider),
            )
        )
        if excluding is not None:
            statement = statement.where(BenchmarkEvaluationLaunch.id != excluding)
        launches = (await self._session.scalar(statement)) or 0
        return launches + await self._operator_runs(provider)

    async def _operator_runs(self, provider: str) -> int:
        """Runs an operator started directly that are inside a provider call right now.

        Counted from open permits rather than from a status, because a run an operator started
        has no launch row to be EXECUTING. It is a narrower signal than the launch count and
        deliberately so: it sees an operator run while a mission is in flight and not in the gap
        between two missions, which is the honest limit of reading concurrency off spending.
        """
        return (
            await self._session.scalar(
                select(func.count(func.distinct(ProviderExecutionPermit.run_id))).where(
                    ProviderExecutionPermit.provider == provider,
                    ProviderExecutionPermit.state == PermitState.RESERVED,
                    ProviderExecutionPermit.launch_id.is_(None),
                )
            )
        ) or 0

    async def _run_charge(self, run_id: uuid.UUID) -> int:
        return (
            await self._session.scalar(
                _charge_sum().where(ProviderExecutionPermit.run_id == run_id)
            )
        ) or 0

    async def _window_charge(self, provider: str, window_seconds: int) -> int:
        """What this provider has been charged inside the policy's rolling window.

        A rolling window measured from the database's own clock at the moment of the check, not
        a calendar day and not a counter something resets. There is nothing to reset and nothing
        to schedule: a permit stops counting exactly when it becomes older than the window.
        """
        return (
            await self._session.scalar(
                _charge_sum().where(
                    ProviderExecutionPermit.provider == provider,
                    ProviderExecutionPermit.opened_at
                    >= func.now() - func.make_interval(0, 0, 0, 0, 0, 0, window_seconds),
                )
            )
        ) or 0

    async def _by_attempt(
        self, run_id: uuid.UUID, attempt_key: str
    ) -> ProviderExecutionPermit | None:
        return (
            await self._session.execute(
                select(ProviderExecutionPermit).where(
                    ProviderExecutionPermit.run_id == run_id,
                    ProviderExecutionPermit.attempt_key == attempt_key,
                )
            )
        ).scalar_one_or_none()

    async def _reservable(self, permit_id: uuid.UUID) -> ProviderExecutionPermit | None:
        """The permit to settle, locked, or None when it is already settled.

        None rather than a refusal, so settling twice is settling once. A permit that a sweep
        already assumed spent stays assumed spent: the database refuses the transition anyway,
        and returning here means a late reconciliation cannot even try to lower it.
        """
        permit = (
            await self._session.execute(
                select(ProviderExecutionPermit)
                .where(ProviderExecutionPermit.id == permit_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if permit is None or permit.state is not PermitState.RESERVED:
            await self._session.rollback()
            return None
        return permit

    async def _clock(self) -> datetime:
        now = await self._session.scalar(select(func.now()))
        assert now is not None  # `now()` always answers
        return now


def _charge_sum() -> Select[tuple[int]]:
    return select(
        func.coalesce(func.sum(ProviderExecutionPermit.charged_requests), 0).cast(Integer)
    )


def _grant(permit: ProviderExecutionPermit) -> ProviderGrant:
    return ProviderGrant(
        permit_id=permit.id,
        granted_requests=permit.granted_requests,
        provider=permit.provider,
        requested_model=permit.requested_model,
    )


def permit_attempt_key(run_id: uuid.UUID, mission_key: str, attempt: int) -> str:
    """The stable identity of one intended provider conversation.

    Stable rather than random, because that is what makes reserving twice for the same intended
    attempt safe. A caller whose database answer was lost recomputes exactly this string and the
    unique constraint hands back the permit it already has.
    """
    return f"{run_id}:{mission_key}:{attempt}"


def _require_supported(provider: str) -> None:
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(f"provider execution governance does not know {provider!r}")


@dataclass(frozen=True, slots=True)
class _PermitTotals:
    """One launch's permits in one state, aggregated by the database."""

    permits: int
    charged: int
    granted: int
    consumed: int
    provider: str | None
    requested_model: str | None


@dataclass(frozen=True, slots=True)
class _TokenTotals:
    """One run's provider-reported usage, with unknown already decided in SQL."""

    responses: int
    unknown: int
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None


_EMPTY_TOTALS = _PermitTotals(
    permits=0, charged=0, granted=0, consumed=0, provider=None, requested_model=None
)


def _launch_usage(
    launch: BenchmarkEvaluationLaunch,
    permits: dict[PermitState, _PermitTotals],
    tokens: _TokenTotals | None,
) -> LaunchProviderUsage:
    """Assemble one launch's execution evidence from what was aggregated, inventing nothing.

    Provider request attempts and provider-reported tokens stay apart and are never added
    together. An attempt is something AgentRank observed itself, before the call; a token count
    is something the provider chose to report, and Gemini has historically reported none. One
    combined number would be two kinds of evidence in one field, and the weaker kind would
    silently decide it.
    """
    state = {kind: permits.get(kind, _EMPTY_TOTALS) for kind in PermitState}
    charged = sum(totals.charged for totals in state.values())
    ceiling = launch.max_provider_requests
    configured: dict[str, Any] = launch.buyer_configuration or {}
    provider = configured.get("provider") or next(
        (totals.provider for totals in state.values() if totals.provider is not None), None
    )
    model = configured.get("requested_model") or next(
        (totals.requested_model for totals in state.values() if totals.requested_model is not None),
        None,
    )
    return LaunchProviderUsage(
        provider=provider,
        requested_model=model,
        max_provider_requests=ceiling,
        permits=sum(totals.permits for totals in state.values()),
        permits_open=state[PermitState.RESERVED].permits,
        permits_reconciled=state[PermitState.RECONCILED].permits,
        permits_assumed_spent=state[PermitState.ASSUMED_SPENT].permits,
        permits_released=state[PermitState.RELEASED].permits,
        requests_charged=charged,
        requests_reconciled=state[PermitState.RECONCILED].consumed,
        requests_assumed_spent=state[PermitState.ASSUMED_SPENT].granted,
        requests_remaining=None if ceiling is None else max(0, ceiling - charged),
        provider_responses=0 if tokens is None else tokens.responses,
        unknown_usage_invocations=0 if tokens is None else tokens.unknown,
        input_tokens=None if tokens is None else tokens.input_tokens,
        output_tokens=None if tokens is None else tokens.output_tokens,
        total_tokens=None if tokens is None else tokens.total_tokens,
    )


class RunPermitBroker:
    """Reserves and settles one benchmark run's provider spending, in sessions of its own.

    Its own sessions rather than the run's, and that is the load bearing part. A reservation has
    to be durable before the worker process that could spend it exists, and a settlement has to
    be durable after that process has exited; sharing the run's session would tie both to
    whatever transaction the runner happened to have open and would put a commit in the middle
    of recording a mission.

    Everything methodology-critical it needs is a value handed to it once: the run, the launch
    behind it when there is one, the provider, the requested model and the budget. It resolves
    nothing, so it cannot resolve something different halfway through a suite.

    `launch_id` is optional because a launch is not the only thing that spends. An operator
    executing one sample of a controlled experiment makes the same provider calls with the same
    money, and its permits are charged to its run exactly as a merchant's are.
    """

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        merchant_id: uuid.UUID,
        launch_id: uuid.UUID | None,
        run_id: uuid.UUID,
        provider: str,
        requested_model: str,
        budget: ExecutionBudget,
    ) -> None:
        self._sessions = sessions
        self._merchant_id = merchant_id
        self._launch_id = launch_id
        self._run_id = run_id
        self._provider = provider
        self._requested_model = requested_model
        self._budget = budget

    async def reserve(self, mission_key: str) -> ProviderGrant:
        async with self._sessions() as session:
            return await ProviderExecutionService(session).reserve(
                merchant_id=self._merchant_id,
                launch_id=self._launch_id,
                run_id=self._run_id,
                mission_key=mission_key,
                attempt=1,
                provider=self._provider,
                requested_model=self._requested_model,
                budget=self._budget,
            )

    async def reconcile(self, permit_id: uuid.UUID, *, consumed_requests: int) -> None:
        async with self._sessions() as session:
            await ProviderExecutionService(session).reconcile(
                permit_id, consumed_requests=consumed_requests
            )

    async def assume_spent(self, permit_id: uuid.UUID) -> None:
        async with self._sessions() as session:
            await ProviderExecutionService(session).assume_spent(permit_id)

    async def release(self, permit_id: uuid.UUID) -> None:
        async with self._sessions() as session:
            await ProviderExecutionService(session).release(permit_id)
