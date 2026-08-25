"""Executing one admitted evaluation launch, in a process the browser has no part in.

A benchmark run takes as long as a suite takes. Holding a browser request open across one would
be operationally fragile and would make the merchant's network the thing that decides whether a
measurement survives, so admission and execution are separate: the API writes a durable queued
launch and answers, and this claims one and runs it.

What this trusts is the row. By the time it runs there is no request, no session and no
credential from the merchant's browser left to consult, so the merchant, the purpose, whatever
that purpose measures, the suite, the world and the buyer configuration all come from what
admission froze. Nothing here takes an identity from a caller.

The purpose decides what the model buyer is given and nothing else about how the run is carried
out:

```text
INITIAL        the ordinary storefront discovery boundary, and the merchant's own information
               as recorded in the frozen source snapshot. No Commerce IR is constructed, and
               the run it produces pins no representation, because none was read
REEVALUATION   the frozen representation as an agent-ready discovery surface, and that
               representation's buyer projection as its merchant information
```

Every frozen value is verified rather than adapted. `AgentConfiguration.from_payload` refuses a
configuration this build cannot reproduce exactly, its digest is checked against the frozen one,
and the executor kind this build would record is compared with the frozen kind. None of them is
substituted, because a run whose identity does not match what the merchant was shown is a
measurement of something nobody asked for.

A launch is claimed only for the world this process holds and only for an executor this process
is configured to run. A settled launch is terminal and cannot be deleted, so a worker that
claimed one it could only refuse would destroy a merchant's request that a differently configured
worker could have served. That is why the provider credential is a claim predicate rather than a
refusal: a worker with no key for a launch's frozen provider leaves it queued for one that has
it. The refusal below it stays as a fail closed backstop and is unreachable through the claim.

A launch nobody is currently configured to run does not disappear into the queue silently. A
dispatch that claimed nothing reports UNSERVICEABLE and names the executor being waited on, so
"there is no work" and "there is work no worker here can do" are different answers.

Execution reuses the same isolated boundary the operator command line uses: a loopback commerce
endpoint, a short lived merchant credential bound to this run, and one worker process per
mission with no database. Nothing about that changes because the launch came from a console, and
nothing about it changes because the launch was a merchant's first.
"""

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentrank_api.auth.service import MerchantCredentialService
from agentrank_api.auth.tokens import TokenMarker
from agentrank_api.benchmark.authorization import provision
from agentrank_api.benchmark.buyer import MerchantBuyerSurface
from agentrank_api.benchmark.capacity import ExecutionBudget
from agentrank_api.benchmark.discovery import buyer_discovery_view, to_payload
from agentrank_api.benchmark.endpoint import (
    LocalCommerceEndpoint,
    RequestLedger,
    issued_benchmark_credential,
)
from agentrank_api.benchmark.environment import BenchmarkEnvironmentService
from agentrank_api.benchmark.evaluation_launch import (
    BenchmarkEvaluationLaunch,
    BuyerProfile,
    EvaluationLaunchStatus,
    EvaluationPurpose,
)
from agentrank_api.benchmark.execution import BenchmarkRunCapability, ExecutorIdentity
from agentrank_api.benchmark.fixtures import BenchmarkFixture
from agentrank_api.benchmark.isolation import (
    IsolatedMissionExecutor,
    provider_worker_environment,
)
from agentrank_api.benchmark.launch import (
    EvaluationLaunchWorkerService,
    worker_executor_kinds,
)
from agentrank_api.benchmark.llm import (
    GEMINI_PROVIDER,
    OPENAI_PROVIDER,
    AgentConfiguration,
    executor_kind,
)
from agentrank_api.benchmark.models import BenchmarkEnvironment, BenchmarkRun
from agentrank_api.benchmark.permits import (
    WAIT_SENTENCES,
    ExecutionWaitReason,
    ProviderExecutionHaltedError,
    ProviderExecutionService,
    RunPermitBroker,
)
from agentrank_api.benchmark.repository import BenchmarkSuiteRepository
from agentrank_api.benchmark.runner import BenchmarkRunService
from agentrank_api.benchmark.wire import LLM_STRATEGY
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.config import Settings
from agentrank_api.errors import ConflictError, NotFoundError
from agentrank_api.payments.provider import PaymentProvider
from agentrank_api.representation.models import CommerceRepresentation, MerchantSourceSnapshot
from agentrank_api.representation.projection import compiled_projection, raw_projection

log = logging.getLogger(__name__)

# Why a launch could not produce a finished run, in this repository's own words. A provider's
# error text and an exception's message never reach a merchant through these.
FAILURE_NO_PROVIDER = "provider_credential_unavailable"
FAILURE_WORLD_MISMATCH = "benchmark_world_mismatch"
FAILURE_SUITE_UNAVAILABLE = "benchmark_suite_unavailable"
FAILURE_BUYER_CONFIGURATION = "buyer_configuration_invalid"
FAILURE_REPRESENTATION_MISSING = "representation_unavailable"
FAILURE_SOURCE_MISSING = "merchant_source_unavailable"
# A model buyer launch admitted before execution budgets existed, which nothing can execute
# under this build: there is no allowance to reserve against and inventing one here would be
# spending on a bound the merchant was never shown.
FAILURE_BUDGET_MISSING = "execution_budget_unavailable"

# The one refusal that means this launch has executed nothing and may still execute later. The
# run service raises it when another run owns this merchant's world.
RUN_ALREADY_ACTIVE = "run_already_active"


# The status a dispatch reports when it claimed nothing because everything queued for this world
# is frozen to an executor this process cannot run. Distinct from claiming nothing because there
# is nothing queued, which stays the ordinary `None`: an operator who cannot tell those apart
# cannot tell a quiet system from a misconfigured one.
UNSERVICEABLE = "UNSERVICEABLE"


class BenchmarkWorld(Protocol):
    """The two things a dispatch needs to know about a world, and deliberately not a third.

    Which merchant it is, and the catalog a run puts them back to before every mission. The
    workload is not here: a dispatch reads the suite from the launch it claimed and the runner
    reads missions from rows, so a world carrying one would be handing an oracle to a caller
    with no use for it.

    A protocol rather than a class, because there are now two kinds of world and neither is the
    other's special case. `AuthoredWorld` is an operator's two JSON documents on disk; a
    workspace world is the catalog a merchant's own frozen evidence generated, read back out of
    the row that stored it. Everything downstream is identical, which is the point: there is one
    benchmark execution path and this phase did not add a second.
    """

    @property
    def merchant_slug(self) -> str: ...

    @property
    def fixture(self) -> BenchmarkFixture: ...


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    """What one dispatch did, for an operator reading the command's output.

    `wait_reason` is present exactly when execution governance is why nothing happened or why
    the launch stopped. It is AgentRank's own vocabulary rather than a provider's, so an
    operator reading "this deployment paused Gemini" is never reading it as "Gemini failed".
    """

    launch_id: uuid.UUID
    run_id: uuid.UUID | None
    status: str
    failure_code: str | None
    detail: str | None = None
    wait_reason: ExecutionWaitReason | None = None


def _unserviceable(launch: BenchmarkEvaluationLaunch) -> DispatchOutcome:
    """Report a queued launch this worker is not configured to execute, without touching it.

    No failure code, because nothing failed and nothing is settled. The launch is still queued
    and a worker holding the right provider credential will claim it. What the operator gets is
    the executor it is waiting for, which is the one fact that says what to configure.
    """
    return DispatchOutcome(
        launch_id=launch.id,
        run_id=None,
        status=UNSERVICEABLE,
        failure_code=None,
        detail=(
            f"this worker cannot run {launch.executor_kind};"
            " the launch stays queued for a worker that can"
        ),
    )


class LaunchDispatchError(Exception):
    """A launch this process refuses to execute, with the code it is settled under."""

    def __init__(self, failure_code: str, detail: str) -> None:
        super().__init__(detail)
        self.failure_code = failure_code
        self.detail = detail


async def execute_next_launch(
    session: AsyncSession,
    sessions: async_sessionmaker[AsyncSession],
    *,
    world: BenchmarkWorld,
    provider: PaymentProvider,
    settings: Settings,
) -> DispatchOutcome | None:
    """Claim the oldest queued launch for this world's merchant and carry it out.

    None when nothing is queued, which is the ordinary answer and not an error. An UNSERVICEABLE
    outcome when something is queued that this process is not configured to execute, which is
    also not an error and is not the same answer: the launch stays queued and untouched, and the
    operator is told which executor it is waiting for.

    The claim is scoped to the world this process holds and to the executor kinds its settings
    can honour, so a worker that could only refuse a launch never takes it away from one that
    could execute it. The claim's row lock is released
    before anything long starts: the loopback server takes as long as an application boot, and
    holding a row lock across one would be an idle transaction bounded by a server start rather
    than by a query.

    Two workers therefore cannot both execute one launch, and neither can strand the other. The
    run table allows at most one executing run per merchant and preparing a world a run owns is
    refused, so the second worker is stopped before it writes anything. If a second worker does
    reach `bind_run` first, the loser aborts the run it just created rather than leaving one
    executing against a merchant with no launch naming it.
    """
    merchant = await MerchantRepository(session).get_by_slug(world.merchant_slug)
    if merchant is None:
        raise NotFoundError("merchant", world.merchant_slug)
    environment = await BenchmarkEnvironmentService(session).require_registered(world.fixture)
    worker = EvaluationLaunchWorkerService(session)
    kinds = worker_executor_kinds(settings)
    launch = await worker.claim_next(
        merchant.id, environment_id=environment.id, executor_kinds=kinds
    )
    if launch is None:
        waiting = await worker.unclaimable_next(
            merchant.id, environment_id=environment.id, executor_kinds=kinds
        )
        outcome = None if waiting is None else _unserviceable(waiting)
        await session.rollback()
        return outcome
    launch_id = launch.id
    merchant_id = launch.merchant_id

    try:
        plan = await _dispatch_plan(session, launch, environment=environment, settings=settings)

        # The claim has done its work: this worker knows which launch it is executing and has
        # verified it can. Releasing the lock here is what keeps the server start below out of a
        # transaction, and it is safe because nothing this worker does next depends on the row
        # staying locked. `bind_run` re-locks it and refuses a launch that moved.
        #
        # The rollback expires every instance loaded under the claim, so the plan carries
        # identifiers and the documents are read again below. Reading an attribute off an
        # expired row here would be a lazy load in the middle of an orchestration with no
        # transaction open.
        await session.rollback()
        # An advisory look before anything long starts. The authoritative capacity gate runs
        # inside the transaction that makes this launch one of the executing ones, and it has
        # to: this read can be stale the instant it returns. What it buys is not correctness, it
        # is not booting a loopback server and starting a run only to abort it when a provider
        # is plainly paused.
        early = await _capacity_wait_reason(session, plan)
        measured = await measured_documents(session, plan)
        # Reading them opened a transaction of its own, and the server boot below is exactly
        # what the claim was released to keep out of one. A commit rather than a rollback: a
        # rollback would expire the two rows that were just read for the sake of reading them.
        await session.commit()
    except LaunchDispatchError as refused:
        await worker.settle_failed(launch_id, failure_code=refused.failure_code)
        log.warning(
            "benchmark evaluation launch refused",
            extra={"launch_id": str(launch_id), "failure": refused.failure_code},
        )
        return DispatchOutcome(
            launch_id=launch_id,
            run_id=None,
            status="FAILED",
            failure_code=refused.failure_code,
            detail=refused.detail,
        )

    if early is not None:
        return DispatchOutcome(
            launch_id=launch_id,
            run_id=None,
            status="QUEUED",
            failure_code=None,
            detail=WAIT_SENTENCES[early],
            wait_reason=early,
        )

    try:
        finished = await _execute(
            session,
            sessions,
            launch_id=launch_id,
            merchant_id=merchant_id,
            plan=plan,
            measured=measured,
            world=world,
            provider=provider,
            settings=settings,
        )
    except ProviderExecutionHaltedError as halted:
        return await _halted(session, worker, launch_id=launch_id, halted=halted)
    except ConflictError as refused:
        # Exactly one conflict is an ordinary answer: a world somebody else's run already owns.
        # The launch has executed nothing then, so it stays queued and honest rather than being
        # failed for a condition that will pass.
        #
        # Every other conflict is deliberately not caught. A mission whose payment cannot be
        # accounted for raises one from inside execution, and reporting that as "still queued"
        # would tell an operator nothing had started while a run was executing and money may
        # have moved. Those propagate, the command exits non zero with the evidence, and the run
        # and its launch are closed the way a stopped run is always closed.
        if refused.reason != RUN_ALREADY_ACTIVE:
            raise
        await session.rollback()
        return DispatchOutcome(
            launch_id=launch_id,
            run_id=None,
            status="QUEUED",
            failure_code=None,
            detail=refused.detail,
        )

    # `execute_started_suite` either completes the run or raises. There is no third answer to
    # report here, and inventing a branch for one would be describing a state this path cannot
    # produce.
    await worker.settle_completed(launch_id)
    return DispatchOutcome(
        launch_id=launch_id,
        run_id=finished.id,
        status="COMPLETED",
        failure_code=None,
    )


@dataclass(frozen=True, slots=True)
class _Plan:
    """The verified execution identity of one claimed launch.

    Identifiers and plain values only, and that is load bearing rather than tidy. The claim's
    transaction is rolled back before anything long starts, which expires every instance loaded
    inside it, so a plan holding an ORM row would hand execution an object whose first attribute
    read is a lazy load with no transaction open and no greenlet context to run it in. The
    documents are read again in `_execute`, where a session is live.

    At most one of `representation_id` and `source_snapshot_id` is ever set, and which one
    follows from the launch's purpose. Both are None for the reference buyer, which reads neither.
    """

    suite_key: str
    suite_version: int
    identity: ExecutorIdentity
    configuration: AgentConfiguration | None
    representation_id: uuid.UUID | None
    source_snapshot_id: uuid.UUID | None = None
    # The execution budget this launch was admitted with, read back off the frozen columns
    # rather than recomputed from today's policy. A run executes under what the merchant was
    # shown, and recomputing it here would silently move the bound whenever a policy changed.
    budget: ExecutionBudget | None = None


async def _dispatch_plan(
    session: AsyncSession,
    launch: BenchmarkEvaluationLaunch,
    *,
    environment: BenchmarkEnvironment,
    settings: Settings,
) -> _Plan:
    """Verify that this process can execute exactly what was admitted, or refuse by name.

    The claim is already scoped to this world and to the executors this process can run, so the
    world check and the provider credential check here are fail closed backstops rather than the
    things that prevent a cross-world or cross-provider dispatch. Reaching either means the claim
    predicate and the capability derivation disagreed, which is a defect in this repository and
    not a deployment an operator can fix, so it settles the launch rather than leaving it to be
    claimed and refused again forever.
    """
    if environment.id != launch.environment_id:
        raise LaunchDispatchError(
            FAILURE_WORLD_MISMATCH,
            f"this worker holds {environment.label}, which is not the world this launch froze",
        )
    suite = await BenchmarkSuiteRepository(session).get_by_id(launch.suite_id)
    if suite is None:
        raise LaunchDispatchError(
            FAILURE_SUITE_UNAVAILABLE, "the benchmark suite this launch froze is not published"
        )

    if launch.buyer_profile is BuyerProfile.REFERENCE_BUYER:
        _require_frozen_kind(launch, IsolatedMissionExecutor.identity.kind)
        return _Plan(
            suite_key=suite.suite_key,
            suite_version=suite.version,
            identity=IsolatedMissionExecutor.identity,
            configuration=None,
            representation_id=None,
        )

    assert launch.buyer_configuration is not None  # the schema pairs profile and configuration
    try:
        configuration = AgentConfiguration.from_payload(launch.buyer_configuration)
    except (TypeError, ValueError) as malformed:
        raise LaunchDispatchError(
            FAILURE_BUYER_CONFIGURATION,
            "the frozen buyer configuration is not one this build can reproduce",
        ) from malformed
    if configuration.configuration_digest != launch.buyer_configuration_digest:
        raise LaunchDispatchError(
            FAILURE_BUYER_CONFIGURATION, "the frozen buyer configuration digest does not verify"
        )
    if not _provider_credential(settings, configuration.provider):
        raise LaunchDispatchError(
            FAILURE_NO_PROVIDER,
            f"this worker has no {configuration.provider} credential to run the frozen buyer",
        )
    representation, source = await _measured(session, launch)
    kind = executor_kind(configuration)
    _require_frozen_kind(launch, kind)
    return _Plan(
        suite_key=suite.suite_key,
        suite_version=suite.version,
        identity=ExecutorIdentity(
            kind=kind, version=1, revision=configuration.configuration_digest
        ),
        configuration=configuration,
        representation_id=None if representation is None else representation.id,
        source_snapshot_id=None if source is None else source.id,
        budget=_frozen_budget(launch, configuration, missions=len(suite.missions)),
    )


def _frozen_budget(
    launch: BenchmarkEvaluationLaunch, configuration: AgentConfiguration, *, missions: int
) -> ExecutionBudget:
    """The execution budget this launch froze, rebuilt from its own columns.

    Refused rather than recomputed when the columns are absent. A model buyer launch admitted
    before execution budgets existed has no allowance anybody agreed to, and running it under
    one this build invented would spend against a bound the merchant never saw.
    """
    if (
        launch.max_provider_requests is None
        or launch.max_requests_per_mission is None
        or launch.execution_budget_version is None
    ):
        raise LaunchDispatchError(
            FAILURE_BUDGET_MISSING,
            "this launch was admitted without a provider execution budget",
        )
    return ExecutionBudget(
        policy_version=launch.execution_budget_version,
        mission_count=missions,
        max_model_turns=configuration.max_model_turns,
        max_provider_requests=launch.max_provider_requests,
        max_requests_per_mission=launch.max_requests_per_mission,
    )


async def _measured(
    session: AsyncSession, launch: BenchmarkEvaluationLaunch
) -> tuple[CommerceRepresentation | None, MerchantSourceSnapshot | None]:
    """The artifact this launch froze, read back and proved to belong to its merchant.

    Which one it is follows from the purpose and never from which column happens to be filled.
    A document that cannot be read fails the launch by name: the alternative would be running
    the buyer against whatever else is around, which is a measurement of something the merchant
    was not shown.
    """
    if launch.purpose is EvaluationPurpose.INITIAL:
        source = await session.get(MerchantSourceSnapshot, launch.source_snapshot_id)
        if source is None or source.merchant_id != launch.merchant_id:
            raise LaunchDispatchError(
                FAILURE_SOURCE_MISSING,
                "the merchant information this launch froze is unreadable",
            )
        return None, source
    representation = await session.get(CommerceRepresentation, launch.representation_id)
    if representation is None or representation.merchant_id != launch.merchant_id:
        raise LaunchDispatchError(
            FAILURE_REPRESENTATION_MISSING, "the representation this launch froze is unreadable"
        )
    return representation, None


async def measured_documents(
    session: AsyncSession, plan: _Plan
) -> tuple[CommerceRepresentation | None, MerchantSourceSnapshot | None]:
    """The documents this plan's buyer will read, loaded where a session is live.

    Read again rather than carried, because the claim's transaction was rolled back before this
    point and every instance loaded inside it is expired. `_dispatch_plan` already proved both
    that the artifact exists and that it belongs to this launch's merchant, and both tables are
    immutable and RESTRICT protected, so what this can fail on is a database that has gone away
    rather than a launch that should not run.
    """
    representation = (
        None
        if plan.representation_id is None
        else await session.get(CommerceRepresentation, plan.representation_id)
    )
    source = (
        None
        if plan.source_snapshot_id is None
        else await session.get(MerchantSourceSnapshot, plan.source_snapshot_id)
    )
    if plan.representation_id is not None and representation is None:
        raise LaunchDispatchError(
            FAILURE_REPRESENTATION_MISSING, "the representation this launch froze is unreadable"
        )
    if plan.source_snapshot_id is not None and source is None:
        raise LaunchDispatchError(
            FAILURE_SOURCE_MISSING, "the merchant information this launch froze is unreadable"
        )
    return representation, source


def _require_frozen_kind(launch: BenchmarkEvaluationLaunch, kind: str) -> None:
    """Refuse to run a buyer whose recorded kind is not the one the launch froze.

    The configuration digest covers the buyer's semantic configuration and not the mapping from
    a provider to an executor kind, so a build that changed that mapping would pass every other
    check and still record a different executor on the run than the merchant was shown. Every
    other frozen value is verified; this makes the last one no different.
    """
    if kind != launch.executor_kind:
        raise LaunchDispatchError(
            FAILURE_BUYER_CONFIGURATION,
            f"this build runs {kind} where the launch froze {launch.executor_kind}",
        )


def _provider_credential(settings: Settings, provider: str) -> bool:
    """Whether this process holds a runtime credential for one provider, never its value."""
    if provider == OPENAI_PROVIDER:
        return settings.openai is not None
    if provider == GEMINI_PROVIDER:
        return settings.gemini is not None
    return False


async def _capacity_wait_reason(session: AsyncSession, plan: _Plan) -> ExecutionWaitReason | None:
    """Whether this provider plainly cannot take another evaluation right now.

    A report and never a decision. Nothing is locked, nothing is claimed and the answer is not
    relied on: a launch this says nothing about can still be refused by the authoritative gate a
    moment later, and that refusal is the one that keeps concurrency correct.
    """
    if plan.configuration is None:
        return None
    status = await ProviderExecutionService(session).status(plan.configuration.provider)
    return status.wait_reason


async def _halted(
    session: AsyncSession,
    worker: EvaluationLaunchWorkerService,
    *,
    launch_id: uuid.UUID,
    halted: ProviderExecutionHaltedError,
) -> DispatchOutcome:
    """Report a launch that execution governance stopped, and settle it only if it started.

    The distinction is where the halt happened. A launch refused before it was bound never
    became one of the executing ones, so it is still queued and honestly so: capacity frees, a
    worker comes back and it runs. A launch halted part way through a suite has a run behind it
    that has already been aborted, and it settles under AgentRank's own failure vocabulary.
    """
    await session.rollback()
    launch = await session.get(BenchmarkEvaluationLaunch, launch_id)
    if launch is not None and launch.status is EvaluationLaunchStatus.QUEUED:
        return DispatchOutcome(
            launch_id=launch_id,
            run_id=None,
            status="QUEUED",
            failure_code=None,
            detail=halted.detail,
            wait_reason=halted.reason,
        )
    await worker.settle_failed(launch_id, failure_code=halted.failure_code)
    log.warning(
        "benchmark evaluation launch stopped by execution governance",
        extra={"launch_id": str(launch_id), "wait_reason": halted.reason.value},
    )
    return DispatchOutcome(
        launch_id=launch_id,
        run_id=None if launch is None else launch.run_id,
        status="FAILED",
        failure_code=halted.failure_code,
        detail=halted.detail,
        wait_reason=halted.reason,
    )


async def _execute(
    session: AsyncSession,
    sessions: async_sessionmaker[AsyncSession],
    *,
    launch_id: uuid.UUID,
    merchant_id: uuid.UUID,
    plan: _Plan,
    measured: tuple[CommerceRepresentation | None, MerchantSourceSnapshot | None],
    world: BenchmarkWorld,
    provider: PaymentProvider,
    settings: Settings,
) -> BenchmarkRun:
    """Start the frozen suite, bind the run to the launch, and execute it.

    `measured` is the pair of documents this launch's buyer reads, already loaded and proved to
    belong to this merchant. They arrive as values rather than being read here, because the one
    place that can settle a launch that cannot be read is the caller.

    The run is bound before a single mission runs, so a process that dies mid execution leaves a
    launch naming the run an operator has to close rather than a launch nobody can connect to
    anything.

    The suite comes from the launch and the catalog fixture comes from the world this process
    holds, which is the same split every other execution path uses: the workload is immutable
    persisted rows, and the world a run puts the merchant back to is trusted operator side
    input, whether it was authored as files or generated from the merchant's own evidence.
    """
    runs = BenchmarkRunService(session)
    worker = EvaluationLaunchWorkerService(session)
    served = RequestLedger()
    representation, source = measured
    async with LocalCommerceEndpoint(settings, provider=provider, observer=served) as endpoint:
        started = await runs.start_suite(
            suite_key=plan.suite_key,
            suite_version=plan.suite_version,
            fixture=world.fixture,
            executor=plan.identity,
            agent_configuration=(
                None if plan.configuration is None else plan.configuration.payload()
            ),
            representation=representation,
            representation_label=_run_label(source),
        )
        try:
            await worker.bind_run(
                launch_id,
                run_id=started.id,
                admit=_capacity_admission(session, plan),
            )
        except ProviderExecutionHaltedError:
            # Provider capacity, not a conflict. The launch is still queued because nothing was
            # written, so the run this process created names nothing and is closed here; a
            # worker that reaches this launch when capacity frees will start a fresh one.
            await session.rollback()
            await runs.abort_run(started.id, merchant_id=merchant_id)
            raise
        except ConflictError:
            # Another worker reached this launch first. The run this process just created is
            # therefore a run no launch names, so it is closed honestly here rather than left
            # executing against a merchant with nothing to explain it. No mission has run yet,
            # so nothing is lost by closing it.
            await runs.abort_run(started.id, merchant_id=merchant_id)
            raise
        capability = BenchmarkRunCapability(merchant_id=merchant_id, run_id=started.id)
        trusted = MerchantBuyerSurface(
            sessions, merchant_id=merchant_id, provider=provider, benchmark_capability=capability
        )
        async with issued_benchmark_credential(
            MerchantCredentialService(session),
            capability=capability,
            marker=TokenMarker.of(settings.environment),
        ) as token:
            executor = _executor(
                plan,
                representation=representation,
                source=source,
                base_url=endpoint.base_url,
                token=token,
                served=served,
                trusted=trusted,
                settings=settings,
                permits=_permit_broker(
                    sessions,
                    plan,
                    merchant_id=merchant_id,
                    launch_id=launch_id,
                    run_id=started.id,
                ),
            )
            try:
                return await runs.execute_started_suite(
                    started.id,
                    executor,
                    merchant_id=merchant_id,
                    fixture=world.fixture,
                    witness=executor,
                )
            except ProviderExecutionHaltedError:
                # The run stopped part way through because AgentRank declined to spend more, so
                # it is closed here as the incomplete thing it is. Aborting before the launch is
                # settled is not tidiness: a failed launch that names a run may only name an
                # aborted one, and the database refuses the settlement otherwise.
                await session.rollback()
                await runs.abort_run(started.id, merchant_id=merchant_id)
                raise


@dataclass(frozen=True, slots=True)
class BuyerSurface:
    """Everything the model buyer is shown about this merchant, as protocol JSON.

    Two documents, and neither is the authoritative catalog. `merchant_information` is what the
    merchant says about themselves, projected neutrally; `discovery` is which discovery boundary
    the buyer's search and read answers come back through. Both are derived from the one frozen
    artifact this launch measures, so an arm cannot be enriched from the ground truth it is
    marked against.
    """

    merchant_information: dict[str, Any]
    discovery: dict[str, Any]


def buyer_surface(
    *,
    representation: CommerceRepresentation | None,
    source: MerchantSourceSnapshot | None,
) -> BuyerSurface:
    """What the model buyer reads, decided by which artifact the launch froze.

    A representation is the compiled surface: the storefront plus that document's typed,
    unit-bearing facts, which is exactly what a re-evaluation is asking about. A source snapshot
    is the merchant as they are: the ordinary storefront, which publishes no typed attribute
    dictionary at all, plus the merchant's own prose and labels.

    Exactly one of the two, and both being absent is a construction error rather than an empty
    surface. A buyer shown nothing would produce a run that measured nothing while looking like
    a measurement of this merchant.
    """
    if representation is not None:
        if source is not None:
            raise ValueError("a launch measures one artifact, not both")
        return BuyerSurface(
            merchant_information=compiled_projection(representation),
            discovery=to_payload(
                buyer_discovery_view(
                    representation_kind="COMPILED",
                    representation_id=representation.id,
                    representation_payload=representation.payload,
                )
            ),
        )
    if source is None:
        raise ValueError("a launch measures one artifact, not none")
    return BuyerSurface(
        merchant_information=raw_projection(source),
        discovery=to_payload(
            buyer_discovery_view(
                representation_kind="RAW", representation_id=None, representation_payload=None
            )
        ),
    )


def _run_label(source: MerchantSourceSnapshot | None) -> str | None:
    """What the run records about what its buyer was shown.

    `merchant-information` is the token the controlled experiment records on every sample it
    executes, whichever arm. It says the buyer was given the merchant's own information rather
    than nothing, which is true of a first evaluation and true of both experiment arms, and it
    distinguishes none of them from each other. Null for a re-evaluation, whose run pins the
    representation itself, and for the reference buyer, which read neither.

    A label and never an identity, which matters most exactly here: an initial run shows this
    label above a null representation identifier, and the identity of what its buyer read is the
    source snapshot the launch froze rather than anything on the run.
    """
    return "merchant-information" if source is not None else None


def _executor(
    plan: _Plan,
    *,
    representation: CommerceRepresentation | None,
    source: MerchantSourceSnapshot | None,
    base_url: str,
    token: str,
    served: RequestLedger,
    trusted: MerchantBuyerSurface,
    settings: Settings,
    permits: RunPermitBroker | None = None,
) -> IsolatedMissionExecutor:
    """The buyer this launch froze, in a process with no database.

    The reference buyer receives no discovery view, which is the discovery boundary's own rule
    rather than an omission here: it reads structured commerce fields by design, so there is no
    surface for either artifact to change and the run it produces pins none.

    The model buyer receives what the purpose says. A re-evaluation gives it exactly the pinned
    representation, flattened into the agent-ready facts the discovery boundary publishes, plus
    that representation's buyer projection as its merchant information. A first evaluation gives
    it the ordinary storefront, which carries no typed attribute dictionaries at all, plus the
    frozen source snapshot's neutral projection. Both come from the frozen artifact and never
    from the catalog the evaluator marks against, so an arm's own information cannot be the
    ground truth it is marked against.
    """
    if plan.configuration is None:
        return IsolatedMissionExecutor(base_url=base_url, token=token, served=served)
    surface = buyer_surface(representation=representation, source=source)
    executor = IsolatedMissionExecutor(
        base_url=base_url,
        token=token,
        served=served,
        strategy=LLM_STRATEGY,
        provision_mandate=lambda brief: provision(trusted, brief),
        agent_configuration=plan.configuration.payload(),
        merchant_information=surface.merchant_information,
        discovery=surface.discovery,
        environment=provider_worker_environment(settings, plan.configuration.provider),
        permits=permits,
    )
    executor.identity = plan.identity
    return executor


def _permit_broker(
    sessions: async_sessionmaker[AsyncSession],
    plan: _Plan,
    *,
    merchant_id: uuid.UUID,
    launch_id: uuid.UUID,
    run_id: uuid.UUID,
) -> RunPermitBroker | None:
    """The spending broker for a model buyer, and nothing at all for the reference buyer.

    None rather than a broker with a zero budget, because the reference buyer calls no provider:
    a permit for it would be a reservation nothing could ever spend and a row that made the
    execution ledger describe work that never touched a provider.
    """
    if plan.configuration is None or plan.budget is None:
        return None
    return RunPermitBroker(
        sessions,
        merchant_id=merchant_id,
        launch_id=launch_id,
        run_id=run_id,
        provider=plan.configuration.provider,
        requested_model=plan.configuration.requested_model,
        budget=plan.budget,
    )


def _capacity_admission(
    session: AsyncSession, plan: _Plan
) -> Callable[[BenchmarkEvaluationLaunch], Awaitable[None]] | None:
    """The provider capacity gate that runs inside the transaction which starts a launch.

    None for the reference buyer, which consumes no provider capacity at all and would otherwise
    be made to wait on a limit about somebody else's spending.
    """
    if plan.configuration is None:
        return None
    provider = plan.configuration.provider

    async def admit(launch: BenchmarkEvaluationLaunch) -> None:
        await ProviderExecutionService(session).admit_launch(provider, excluding=launch.id)

    return admit
