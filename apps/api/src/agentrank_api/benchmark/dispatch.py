"""Executing one admitted re-evaluation, in a process the browser has no part in.

A benchmark run takes as long as a suite takes. Holding a browser request open across one would
be operationally fragile and would make the merchant's network the thing that decides whether a
measurement survives, so admission and execution are separate: the API writes a durable queued
launch and answers, and this claims one and runs it.

What this trusts is the row. By the time it runs there is no request, no session and no
credential from the merchant's browser left to consult, so the merchant, the representation, the
suite, the world and the buyer configuration all come from what admission froze. Nothing here
takes an identity from a caller.

The frozen buyer configuration is verified rather than adapted. `AgentConfiguration.from_payload`
refuses a configuration this build cannot reproduce exactly, so a launch admitted by one build
and executed by another fails by name instead of quietly running a different buyer. A provider
credential that is not present fails the same way. Neither is substituted, because a run whose
identity does not match what the merchant was shown is a measurement of something nobody asked
for.

Execution reuses the same isolated boundary the operator command line uses: a loopback commerce
endpoint, a short lived merchant credential bound to this run, and one worker process per
mission with no database. Nothing about that changes because the launch came from a console.
"""

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentrank_api.auth.service import MerchantCredentialService
from agentrank_api.auth.tokens import TokenMarker
from agentrank_api.benchmark.authored import AuthoredWorld
from agentrank_api.benchmark.authorization import provision
from agentrank_api.benchmark.buyer import MerchantBuyerSurface
from agentrank_api.benchmark.discovery import buyer_discovery_view, to_payload
from agentrank_api.benchmark.endpoint import (
    LocalCommerceEndpoint,
    RequestLedger,
    issued_benchmark_credential,
)
from agentrank_api.benchmark.environment import BenchmarkEnvironmentService
from agentrank_api.benchmark.execution import BenchmarkRunCapability, ExecutorIdentity
from agentrank_api.benchmark.isolation import (
    IsolatedMissionExecutor,
    provider_worker_environment,
)
from agentrank_api.benchmark.launch import ReevaluationWorkerService
from agentrank_api.benchmark.lifecycle import BenchmarkRunStatus
from agentrank_api.benchmark.llm import (
    GEMINI_PROVIDER,
    OPENAI_PROVIDER,
    AgentConfiguration,
    executor_kind,
)
from agentrank_api.benchmark.models import BenchmarkRun
from agentrank_api.benchmark.reevaluation import BenchmarkReevaluation, BuyerProfile
from agentrank_api.benchmark.repository import BenchmarkSuiteRepository
from agentrank_api.benchmark.runner import BenchmarkRunService
from agentrank_api.benchmark.wire import LLM_STRATEGY
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.config import Settings
from agentrank_api.errors import ConflictError, NotFoundError
from agentrank_api.payments.provider import PaymentProvider
from agentrank_api.representation.models import CommerceRepresentation
from agentrank_api.representation.projection import compiled_projection

log = logging.getLogger(__name__)

# Why a launch could not produce a finished run, in this repository's own words. A provider's
# error text and an exception's message never reach a merchant through these.
FAILURE_NO_PROVIDER = "provider_credential_unavailable"
FAILURE_WORLD_MISMATCH = "benchmark_world_mismatch"
FAILURE_SUITE_UNAVAILABLE = "benchmark_suite_unavailable"
FAILURE_BUYER_CONFIGURATION = "buyer_configuration_invalid"
FAILURE_REPRESENTATION_MISSING = "representation_unavailable"


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    """What one dispatch did, for an operator reading the command's output."""

    reevaluation_id: uuid.UUID
    run_id: uuid.UUID | None
    status: str
    failure_code: str | None
    detail: str | None = None


class ReevaluationDispatchError(Exception):
    """A launch this process refuses to execute, with the code it is settled under."""

    def __init__(self, failure_code: str, detail: str) -> None:
        super().__init__(detail)
        self.failure_code = failure_code
        self.detail = detail


async def execute_next_reevaluation(
    session: AsyncSession,
    sessions: async_sessionmaker[AsyncSession],
    *,
    world: AuthoredWorld,
    provider: PaymentProvider,
    settings: Settings,
) -> DispatchOutcome | None:
    """Claim the oldest queued launch for this world's merchant and carry it out.

    None when nothing is queued, which is the ordinary answer and not an error.

    Two workers cannot both execute one launch. The run table already allows at most one
    executing run per merchant, and preparing a world a run owns is refused, so the second
    worker is stopped before it writes anything. `bind_run` then refuses a launch that is no
    longer queued, so a stale claim cannot attach a second run to it either.
    """
    merchant = await MerchantRepository(session).get_by_slug(world.merchant_slug)
    if merchant is None:
        raise NotFoundError("merchant", world.merchant_slug)
    worker = ReevaluationWorkerService(session)
    launch = await worker.claim_next(merchant.id)
    if launch is None:
        await session.rollback()
        return None
    reevaluation_id = launch.id

    try:
        plan = await _dispatch_plan(session, launch, world=world, settings=settings)
    except ReevaluationDispatchError as refused:
        await worker.settle_failed(reevaluation_id, failure_code=refused.failure_code)
        log.warning(
            "benchmark re-evaluation refused",
            extra={"reevaluation_id": str(reevaluation_id), "failure": refused.failure_code},
        )
        return DispatchOutcome(
            reevaluation_id=reevaluation_id,
            run_id=None,
            status="FAILED",
            failure_code=refused.failure_code,
            detail=refused.detail,
        )

    try:
        finished = await _execute(
            session,
            sessions,
            launch=launch,
            plan=plan,
            world=world,
            provider=provider,
            settings=settings,
        )
    except ConflictError as refused:
        # A world somebody else's run already owns. The launch has executed nothing, so it stays
        # queued and honest rather than being failed for a condition that will pass.
        await session.rollback()
        return DispatchOutcome(
            reevaluation_id=reevaluation_id,
            run_id=None,
            status="QUEUED",
            failure_code=None,
            detail=refused.detail,
        )

    if finished.status is BenchmarkRunStatus.COMPLETED:
        await worker.settle_completed(reevaluation_id)
        return DispatchOutcome(
            reevaluation_id=reevaluation_id,
            run_id=finished.id,
            status="COMPLETED",
            failure_code=None,
        )
    # An execution that stopped leaves the run exactly where it stopped. Nothing is replayed and
    # nothing is tidied away: a mission that may have paid for something is an operator's to
    # close, and closing it settles this launch.
    return DispatchOutcome(
        reevaluation_id=reevaluation_id,
        run_id=finished.id,
        status="EXECUTING",
        failure_code=None,
        detail="the run did not complete and is waiting on an operator",
    )


@dataclass(frozen=True, slots=True)
class _Plan:
    """The verified execution identity of one claimed launch."""

    suite_key: str
    suite_version: int
    identity: ExecutorIdentity
    configuration: AgentConfiguration | None
    representation: CommerceRepresentation | None


async def _dispatch_plan(
    session: AsyncSession,
    launch: BenchmarkReevaluation,
    *,
    world: AuthoredWorld,
    settings: Settings,
) -> _Plan:
    """Verify that this process can execute exactly what was admitted, or refuse by name."""
    environment = await BenchmarkEnvironmentService(session).require_registered(world.fixture)
    if environment.id != launch.environment_id:
        raise ReevaluationDispatchError(
            FAILURE_WORLD_MISMATCH,
            f"this worker holds {world.fixture.label}, which is not the world this launch froze",
        )
    suite = await BenchmarkSuiteRepository(session).get_by_id(launch.suite_id)
    if suite is None:
        raise ReevaluationDispatchError(
            FAILURE_SUITE_UNAVAILABLE, "the benchmark suite this launch froze is not published"
        )

    if launch.buyer_profile is BuyerProfile.REFERENCE_BUYER:
        return _Plan(
            suite_key=suite.suite_key,
            suite_version=suite.version,
            identity=IsolatedMissionExecutor.identity,
            configuration=None,
            representation=None,
        )

    assert launch.buyer_configuration is not None  # the schema pairs profile and configuration
    try:
        configuration = AgentConfiguration.from_payload(launch.buyer_configuration)
    except (TypeError, ValueError) as malformed:
        raise ReevaluationDispatchError(
            FAILURE_BUYER_CONFIGURATION,
            "the frozen buyer configuration is not one this build can reproduce",
        ) from malformed
    if configuration.configuration_digest != launch.buyer_configuration_digest:
        raise ReevaluationDispatchError(
            FAILURE_BUYER_CONFIGURATION, "the frozen buyer configuration digest does not verify"
        )
    if not _provider_credential(settings, configuration.provider):
        raise ReevaluationDispatchError(
            FAILURE_NO_PROVIDER,
            f"this worker has no {configuration.provider} credential to run the frozen buyer",
        )
    representation = await session.get(CommerceRepresentation, launch.representation_id)
    if representation is None or representation.merchant_id != launch.merchant_id:
        raise ReevaluationDispatchError(
            FAILURE_REPRESENTATION_MISSING, "the representation this launch froze is unreadable"
        )
    return _Plan(
        suite_key=suite.suite_key,
        suite_version=suite.version,
        identity=ExecutorIdentity(
            kind=executor_kind(configuration),
            version=1,
            revision=configuration.configuration_digest,
        ),
        configuration=configuration,
        representation=representation,
    )


def _provider_credential(settings: Settings, provider: str) -> bool:
    """Whether this process holds a runtime credential for one provider, never its value."""
    if provider == OPENAI_PROVIDER:
        return settings.openai is not None
    if provider == GEMINI_PROVIDER:
        return settings.gemini is not None
    return False


async def _execute(
    session: AsyncSession,
    sessions: async_sessionmaker[AsyncSession],
    *,
    launch: BenchmarkReevaluation,
    plan: _Plan,
    world: AuthoredWorld,
    provider: PaymentProvider,
    settings: Settings,
) -> BenchmarkRun:
    """Start the frozen suite, bind the run to the launch, and execute it.

    The run is bound before a single mission runs, so a process that dies mid execution leaves a
    launch naming the run an operator has to close rather than a launch nobody can connect to
    anything.

    The suite comes from the launch and the catalog fixture comes from the operator files, which
    is the same split every other execution path uses: the workload is immutable persisted rows,
    and the world a run puts the merchant back to is trusted operator side input.
    """
    runs = BenchmarkRunService(session)
    worker = ReevaluationWorkerService(session)
    merchant_id = launch.merchant_id
    reevaluation_id = launch.id
    served = RequestLedger()
    async with LocalCommerceEndpoint(settings, provider=provider, observer=served) as endpoint:
        started = await runs.start_suite(
            suite_key=plan.suite_key,
            suite_version=plan.suite_version,
            fixture=world.fixture,
            executor=plan.identity,
            agent_configuration=(
                None if plan.configuration is None else plan.configuration.payload()
            ),
            representation=plan.representation,
        )
        await worker.bind_run(reevaluation_id, started.id)
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
                base_url=endpoint.base_url,
                token=token,
                served=served,
                trusted=trusted,
                settings=settings,
            )
            return await runs.execute_started_suite(
                started.id,
                executor,
                merchant_id=merchant_id,
                fixture=world.fixture,
                witness=executor,
            )


def _executor(
    plan: _Plan,
    *,
    base_url: str,
    token: str,
    served: RequestLedger,
    trusted: MerchantBuyerSurface,
    settings: Settings,
) -> IsolatedMissionExecutor:
    """The buyer this launch froze, in a process with no database.

    The reference buyer receives no discovery view, which is the discovery boundary's own rule
    rather than an omission here: it reads structured commerce fields by design, so there is no
    surface for a representation to change and the run it produces pins none.

    The model buyer receives exactly the pinned representation, flattened into the agent-ready
    facts the discovery boundary publishes, plus that representation's buyer projection as its
    merchant information. Both come from the frozen artifact and never from the catalog the
    evaluator marks against.
    """
    if plan.configuration is None or plan.representation is None:
        return IsolatedMissionExecutor(base_url=base_url, token=token, served=served)
    executor = IsolatedMissionExecutor(
        base_url=base_url,
        token=token,
        served=served,
        strategy=LLM_STRATEGY,
        provision_mandate=lambda brief: provision(trusted, brief),
        agent_configuration=plan.configuration.payload(),
        merchant_information=compiled_projection(plan.representation),
        discovery=to_payload(
            buyer_discovery_view(
                representation_kind="COMPILED",
                representation_id=plan.representation.id,
                representation_payload=plan.representation.payload,
            )
        ),
        environment=provider_worker_environment(settings, plan.configuration.provider),
    )
    executor.identity = plan.identity
    return executor
