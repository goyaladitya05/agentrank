"""The benchmark operator commands: prepare a world, execute work, read a result, close one.

The same three steps in each: parse what the operator typed, call one application service, print
what came back. There is no SQL here, no lock, no transaction and no rule about what a mission
means, because every one of those already exists and a second copy inside a command would be a
second answer to a question that must have exactly one.

The names say what moves:

```text
seed            registers the world, puts it back, publishes the suite  catalog is overwritten
run             executes the suite                                      money moves, stock goes
dispatch        executes one launch a merchant queued from the console  money moves, stock goes
compare-create  predeclares a controlled paired experiment              nothing moves
compare-run     executes one predeclared sample                         money moves, stock goes
compare-show    reads one experiment and every sample in it             nothing moves
show            reads one run and counts it                             nothing moves
diagnose        reads one run through the diagnostics engine            nothing moves
abort           closes a run that stopped                               nothing moves
settle          closes the launch behind a run that already finished    nothing moves
```

The three that execute spend. A benchmark mission that completes creates a mandate, quotes a
checkout, holds stock and pays through the real payment kernel with the deterministic fake
provider. No real money is involved and stock genuinely leaves the shelf, which is why the world
is put back before every mission and why the command refuses any merchant that is not a
registered benchmark world.

There is no resume command, and the omission is the whole crash story rather than a gap. A
mission left RUNNING may have created a quote, held stock and dispatched a payment, and
re-executing it blindly is how a benchmark buys something twice. An operator reads the run with
`show`, closes it with `abort`, and starts a new one, which prepares the world again.

Every output says what produced it. The executor is named as `reference-v1` and the numbers are
labelled as a reference benchmark result, because the thing that produced them is a scripted
deterministic executor and not an AI buyer. See docs/benchmark.md.

The authored world is read from files rather than imported, and that is a boundary rather than
a preference. A mission's expected outcome is the answer key, and while it was Python in this
package a buyer process could import it and read every mission's ground truth. It now lives in
`benchmarks/<world>/` at the top of the repository, which is outside the distribution this
package is built into, and these commands are given a path to it. A worker started in an empty
directory with an environment naming no path has nothing to read. See
`agentrank_api.benchmark.authored`.

`--world` defaults to the one world that exists, relative to the working directory, which is
the repository root for `make benchmark`. A second world is a second directory and no code
change.
"""

import argparse
import uuid
from pathlib import Path
from typing import Any, TextIO, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentrank_api.auth.service import MerchantCredentialService
from agentrank_api.auth.tokens import TokenMarker
from agentrank_api.benchmark.authored import AuthoredWorld, publish_world, read_world
from agentrank_api.benchmark.authorization import provision
from agentrank_api.benchmark.buyer import MerchantBuyerSurface
from agentrank_api.benchmark.discovery import buyer_discovery_view, to_payload
from agentrank_api.benchmark.dispatch import execute_next_launch
from agentrank_api.benchmark.endpoint import (
    LocalCommerceEndpoint,
    RequestLedger,
    issued_benchmark_credential,
)
from agentrank_api.benchmark.environment import BenchmarkEnvironmentService
from agentrank_api.benchmark.execution import BenchmarkRunCapability, ExecutorIdentity
from agentrank_api.benchmark.experiment import (
    CompilerImpactExperimentService,
    ExperimentTreatment,
)
from agentrank_api.benchmark.isolation import (
    IsolatedMissionExecutor,
    provider_worker_environment,
)
from agentrank_api.benchmark.launch import EvaluationLaunchWorkerService
from agentrank_api.benchmark.lifecycle import MissionRunStatus
from agentrank_api.benchmark.llm import (
    GEMINI_PROVIDER,
    OPENAI_PROVIDER,
    AgentConfiguration,
    executor_kind,
)
from agentrank_api.benchmark.metrics import BenchmarkMetrics
from agentrank_api.benchmark.models import (
    AgentProviderUsage,
    AgentTraceEvent,
    BenchmarkMissionRun,
    BenchmarkRun,
)
from agentrank_api.benchmark.reference_executor import ReferenceMissionExecutor
from agentrank_api.benchmark.repository import BenchmarkSuiteRepository
from agentrank_api.benchmark.runner import BenchmarkRunService
from agentrank_api.benchmark.tools import MeasuredBuyerSurface, ToolLedger
from agentrank_api.benchmark.wire import LLM_STRATEGY
from agentrank_api.cli.exits import ExitCode
from agentrank_api.cli.output import write_json
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.config import Settings
from agentrank_api.errors import NotFoundError
from agentrank_api.payments.provider import PaymentProvider

# Where the authored world lives, relative to the working directory. A path rather than an
# import, because an imported oracle is one a buyer process can import too.
DEFAULT_WORLD = Path("benchmarks/voltedge")

# Column widths, so a mission line is one line on an ordinary terminal.
KEY_WIDTH = 30
STATUS_WIDTH = 10
REASON_WIDTH = 26

MISSING = "-"
ProviderUsageRow = tuple[str | None, int | None, int | None, int | None, int | None, int | None]

# What every report says about what produced it. A reference executor is a scripted deterministic
# buyer, and its completion rate is evidence that the benchmark path works rather than evidence
# about what an autonomous agent can do.
DISCLAIMER = "reference benchmark result, produced by a deterministic executor and not an agent"


def add_commands(parser: argparse.ArgumentParser) -> None:
    """Declare the benchmark command surface."""
    commands = parser.add_subparsers(dest="command_name", required=True)

    seeding = commands.add_parser(
        "seed",
        help="register and prepare the benchmark world, and publish its suite",
        description=(
            "Register the VoltEdge world, put its catalog back to exactly what the fixture"
            " describes, and publish the suite authored against it. Convergent in all three:"
            " running it twice changes nothing. Editing either definition without bumping its"
            " version is refused, which is what the versions are for."
        ),
    )
    _add_world(seeding)
    _add_json(seeding)
    seeding.set_defaults(command=seed)

    running = commands.add_parser(
        "run",
        help="execute the benchmark suite with the deterministic reference executor",
        description=(
            "Execute every mission in suite order against a world that is put back before each"
            " one. Successful missions create real mandates, quotes, holds and payments through"
            " the deterministic fake provider. No real money is involved and stock is genuinely"
            " consumed inside the benchmark world."
        ),
    )
    running.add_argument(
        "--representation-label",
        default=None,
        help="a label for the merchant representation under test. A label, never an identity",
    )
    running.add_argument(
        "--executor",
        choices=("reference", "llm"),
        default="reference",
        help="executor to sample; llm requires OPENAI_API_KEY in the untracked local .env",
    )
    running.add_argument(
        "--model",
        default="gpt-5.6-terra",
        help="exact OpenAI Responses model identifier for --executor llm",
    )
    running.add_argument(
        "--provider",
        choices=(OPENAI_PROVIDER, GEMINI_PROVIDER),
        default=OPENAI_PROVIDER,
        help="runtime LLM provider for --executor llm",
    )
    running.add_argument(
        "--isolated",
        action="store_true",
        help=(
            "run the buyer in a separate process with no database credential, over the merchant"
            " commerce API on a loopback port. Slower, and the boundary a model will use"
        ),
    )
    _add_world(running)
    _add_json(running)
    running.set_defaults(command=run)

    detail = commands.add_parser(
        "show",
        help="one run, its pins, its metrics and every mission outcome",
        description=(
            "Read one benchmark run. The metrics are derived from the stored mission results"
            " every time rather than from a stored count, so a report cannot disagree with the"
            " rows it summarises."
        ),
    )
    detail.add_argument("run_id", type=uuid.UUID, help="the benchmark run identifier")
    _add_world(detail)
    _add_json(detail)
    detail.set_defaults(command=show)

    closing = commands.add_parser(
        "abort",
        help="close a run that stopped, without claiming it describes the whole suite",
        description=(
            "Close a run whose execution stopped. ABORTED is its own status because a partial"
            " run presented as a complete one would report a rate over a denominator nobody"
            " chose. Nothing is re-executed: a mission left RUNNING may have paid for something,"
            " and this benchmark never replays one."
        ),
    )
    closing.add_argument("run_id", type=uuid.UUID, help="the benchmark run identifier")
    _add_world(closing)
    _add_json(closing)
    closing.set_defaults(command=abort)

    comparison = commands.add_parser(
        "compare-create",
        help="predeclare a controlled raw versus compiler-produced Commerce IR experiment",
    )
    comparison.add_argument("--source-snapshot-id", type=uuid.UUID, required=True)
    comparison.add_argument("--compiled-representation-id", type=uuid.UUID, required=True)
    comparison.add_argument("--sample-count", type=int, choices=(1, 2, 3), default=1)
    comparison.add_argument("--model", default="gemini-3.7-flash")
    comparison.add_argument(
        "--provider", choices=(OPENAI_PROVIDER, GEMINI_PROVIDER), default=GEMINI_PROVIDER
    )
    comparison.add_argument(
        "--evaluation",
        action="store_true",
        help="freeze this comparison as evaluation evidence rather than development evidence",
    )
    _add_world(comparison)
    _add_json(comparison)
    comparison.set_defaults(command=compare_create)

    comparison_run = commands.add_parser(
        "compare-run",
        help="execute the next predeclared live compiler-impact sample",
    )
    comparison_run.add_argument("experiment_id", type=uuid.UUID)
    _add_world(comparison_run)
    _add_json(comparison_run)
    comparison_run.set_defaults(command=compare_run)

    comparison_show = commands.add_parser(
        "compare-show",
        help="show predeclared samples, deterministic metrics, and paired transitions",
    )
    comparison_show.add_argument("experiment_id", type=uuid.UUID)
    _add_world(comparison_show)
    _add_json(comparison_show)
    comparison_show.set_defaults(command=compare_show)

    dispatching = commands.add_parser(
        "dispatch",
        help="execute the next evaluation a merchant queued from the console",
        description=(
            "Claim the oldest queued evaluation launch for this world's merchant and carry it"
            " out. Everything it executes was frozen when the merchant asked for it: what is"
            " being measured, the suite, the world and the buyer. Nothing here reads a browser"
            " session, and a launch this process cannot execute exactly is failed by name rather"
            " than run with something close."
        ),
    )
    _add_world(dispatching)
    _add_json(dispatching)
    dispatching.set_defaults(command=dispatch)

    settling = commands.add_parser(
        "settle",
        help="close the merchant launch behind a run that has already finished",
        description=(
            "Settle the evaluation launch a finished run belonged to. The worker that executes"
            " a launch settles it when the run ends, so this is only needed when that process"
            " died in between: the launch is then left executing against a run nobody can reach,"
            " and it holds this merchant's one pending launch slot until it is closed. The launch"
            " is settled to agree with the run, which is the only settlement the database"
            " accepts."
        ),
    )
    settling.add_argument("run_id", type=uuid.UUID, help="the benchmark run identifier")
    _add_world(settling)
    _add_json(settling)
    settling.set_defaults(command=settle)

    diagnosing = commands.add_parser(
        "diagnose",
        help="one run's deterministic merchant diagnosis: findings, ownership, demand",
        description=(
            "Read one benchmark run through the diagnostics engine. Every finding carries its"
            " owner, its evidence level and whether any merchant action applies, so a provider"
            " outage never reads as a catalog problem. Derived from persisted evidence on every"
            " read; nothing here writes."
        ),
    )
    diagnosing.add_argument("run_id", type=uuid.UUID, help="the benchmark run identifier")
    _add_world(diagnosing)
    _add_json(diagnosing)
    diagnosing.set_defaults(command=diagnose)


def _add_world(parser: argparse.ArgumentParser) -> None:
    """Where the authored world is, which every command needs and none of them imports.

    Even `show` and `abort` take it, because the merchant a run belongs to is named by the
    authored world and these commands hold no credential to be told it by. One option on every
    command beats a second way of naming a merchant.
    """
    parser.add_argument(
        "--world",
        type=Path,
        default=DEFAULT_WORLD,
        dest="world",
        help=(
            "the directory holding the authored benchmark world, its catalog and its suite"
            f" (default {DEFAULT_WORLD})"
        ),
    )


def _add_json(parser: argparse.ArgumentParser) -> None:
    """The one flag every command shares."""
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="print one JSON document instead of a table",
    )


async def seed(
    session: AsyncSession,
    sessions: async_sessionmaker[AsyncSession],
    provider: PaymentProvider,
    arguments: argparse.Namespace,
    out: TextIO,
    settings: Settings,
) -> int:
    """Register the world, put it back, and publish the suite."""
    del sessions, provider, settings
    prepared, suite = await publish_world(session, read_world(arguments.world))
    payload = {
        "environment": prepared.environment.label,
        "fixture_hash": prepared.environment.fixture_hash,
        "merchant_id": str(prepared.environment.merchant_id),
        "merchant_slug": prepared.environment.merchant_slug,
        "products": prepared.catalog.products,
        "variants": prepared.catalog.variants,
        "rows_created": prepared.catalog.created,
        "holds_released": prepared.released_holds,
        "variants_withdrawn": prepared.withdrawn,
        "suite": suite.label,
        "definition_hash": suite.definition_hash,
    }
    if arguments.as_json:
        write_json(out, payload)
        return ExitCode.OK

    print(f"world       {payload['environment']}  {payload['fixture_hash']}", file=out)
    print(f"merchant    {payload['merchant_slug']}  {payload['merchant_id']}", file=out)
    print(
        f"catalog     {payload['products']} products, {payload['variants']} variants,"
        f" {payload['rows_created']} rows created",
        file=out,
    )
    print(
        f"restored    {payload['holds_released']} holds released,"
        f" {payload['variants_withdrawn']} variants withdrawn",
        file=out,
    )
    print(f"suite       {payload['suite']}  {payload['definition_hash']}", file=out)
    return ExitCode.OK


async def run(
    session: AsyncSession,
    sessions: async_sessionmaker[AsyncSession],
    provider: PaymentProvider,
    arguments: argparse.Namespace,
    out: TextIO,
    settings: Settings,
) -> int:
    """Execute the suite, then print the result the same way `show` does.

    The provider is the one the application is wired with and is handed in rather than built
    here, which is the same rule every other command follows: a command that could construct a
    provider could be pointed at one the application is not running with.

    Two session owners, and this is the only command with two. The run service records what
    happened on this command's own session. The buyer surface opens one of its own per
    operation, exactly as an HTTP route does, so the commerce a mission carries out is not part
    of the run's transaction sequence and an executor cannot leave the run unable to record what
    it just did.

    The ledger sits between the executor and the merchant and is handed to the run service as
    the witness. It is what decides whether an interruption was the merchant's or the harness's,
    from what the surface actually did, and the executor is given the surface that writes to it
    rather than the ledger itself.
    """
    world = read_world(arguments.world)
    merchant_id = await _benchmark_merchant(session, world)
    service = BenchmarkRunService(session)
    if arguments.executor == "llm":
        if not arguments.isolated:
            raise ValueError("the LLM executor is always isolated; pass --isolated")
        _require_provider_key(settings, arguments.provider)
        finished = await _llm_isolated_run(
            session, sessions, service, merchant_id, world, arguments, provider, settings
        )
        return await _report(service, finished.id, merchant_id, arguments, out)
    if arguments.isolated:
        finished = await _isolated_run(
            session, service, merchant_id, world, arguments, provider, settings
        )
        return await _report(service, finished.id, merchant_id, arguments, out)

    ledger = ToolLedger()
    surface = MeasuredBuyerSurface(
        MerchantBuyerSurface(sessions, merchant_id=merchant_id, provider=provider), ledger
    )
    finished = await service.run_suite(
        ReferenceMissionExecutor(surface),
        suite_key=world.suite.key,
        suite_version=world.suite.version,
        fixture=world.fixture,
        witness=ledger,
        representation_label=arguments.representation_label,
    )
    return await _report(service, finished.id, merchant_id, arguments, out)


async def _isolated_run(
    session: AsyncSession,
    service: BenchmarkRunService,
    merchant_id: uuid.UUID,
    world: AuthoredWorld,
    arguments: argparse.Namespace,
    provider: PaymentProvider,
    settings: Settings,
) -> BenchmarkRun:
    """Execute the suite with the buyer in a process that has no database.

    Three things are arranged and unwound together, in this order, because each depends on the
    one before it. The endpoint has to be listening before a credential is worth anything, the
    credential has to exist before a worker can be given one, and both have to be torn down
    whatever the run does: a server left running is a merchant an unread process can still
    reach, and a credential left valid is one nobody revoked.

    The recorded executor identity is `reference-isolated`, not `reference`. The buyer inside
    the worker is the same scripted one, and it reaches the merchant over a different transport
    with different failure modes, so a run produced this way is not the same measurement and
    must not be compared with one as though it were.
    """
    served = RequestLedger()
    async with LocalCommerceEndpoint(settings, provider=provider, observer=served) as endpoint:
        started = await service.start_suite(
            suite_key=world.suite.key,
            suite_version=world.suite.version,
            fixture=world.fixture,
            executor=IsolatedMissionExecutor.identity,
            representation_label=arguments.representation_label,
        )
        capability = BenchmarkRunCapability(merchant_id=merchant_id, run_id=started.id)
        async with issued_benchmark_credential(
            MerchantCredentialService(session),
            capability=capability,
            marker=TokenMarker.of(settings.environment),
        ) as token:
            executor = IsolatedMissionExecutor(
                base_url=endpoint.base_url, token=token, served=served
            )
            return await service.execute_started_suite(
                started.id,
                executor,
                merchant_id=merchant_id,
                fixture=world.fixture,
                witness=executor,
            )


async def _llm_isolated_run(
    session: AsyncSession,
    sessions: async_sessionmaker[AsyncSession],
    service: BenchmarkRunService,
    merchant_id: uuid.UUID,
    world: AuthoredWorld,
    arguments: argparse.Namespace,
    provider: PaymentProvider,
    settings: Settings,
) -> BenchmarkRun:
    """Run one sequential LLM sample with trusted mandate provisioning outside the worker."""
    configuration = AgentConfiguration(provider=arguments.provider, requested_model=arguments.model)
    identity = ExecutorIdentity(
        kind=executor_kind(configuration), version=1, revision=configuration.configuration_digest
    )
    served = RequestLedger()
    async with LocalCommerceEndpoint(settings, provider=provider, observer=served) as endpoint:
        started = await service.start_suite(
            suite_key=world.suite.key,
            suite_version=world.suite.version,
            fixture=world.fixture,
            executor=identity,
            representation_label=arguments.representation_label,
            agent_configuration=configuration.payload(),
        )
        capability = BenchmarkRunCapability(merchant_id=merchant_id, run_id=started.id)
        trusted = MerchantBuyerSurface(
            sessions, merchant_id=merchant_id, provider=provider, benchmark_capability=capability
        )
        async with issued_benchmark_credential(
            MerchantCredentialService(session),
            capability=capability,
            marker=TokenMarker.of(settings.environment),
        ) as token:
            worker_environment = provider_worker_environment(settings, configuration.provider)
            executor = IsolatedMissionExecutor(
                base_url=endpoint.base_url,
                token=token,
                served=served,
                strategy=LLM_STRATEGY,
                provision_mandate=lambda brief: provision(trusted, brief),
                agent_configuration=configuration.payload(),
                environment=worker_environment,
            )
            executor.identity = identity
            return await service.execute_started_suite(
                started.id,
                executor,
                merchant_id=merchant_id,
                fixture=world.fixture,
                witness=executor,
            )


async def dispatch(
    session: AsyncSession,
    sessions: async_sessionmaker[AsyncSession],
    provider: PaymentProvider,
    arguments: argparse.Namespace,
    out: TextIO,
    settings: Settings,
) -> int:
    """Execute at most one queued merchant evaluation launch, and say what became of it.

    One launch per invocation rather than a loop, because a benchmark run is the largest thing
    this system does and an operator should decide how many happen. Nothing queued is an
    ordinary answer and exits OK: there is no work, which is not a failure.
    """
    world = read_world(arguments.world)
    outcome = await execute_next_launch(
        session, sessions, world=world, provider=provider, settings=settings
    )
    if outcome is None:
        payload: dict[str, Any] = {"launch_id": None, "status": "NONE_QUEUED"}
        if arguments.as_json:
            write_json(out, payload)
        else:
            print("queued      nothing to execute for this world", file=out)
        return ExitCode.OK

    payload = {
        "launch_id": str(outcome.launch_id),
        "status": outcome.status,
        "run_id": None if outcome.run_id is None else str(outcome.run_id),
        "failure_code": outcome.failure_code,
        "detail": outcome.detail,
    }
    if arguments.as_json:
        write_json(out, payload)
    else:
        print(f"launch      {payload['launch_id']}", file=out)
        print(f"status      {payload['status']}", file=out)
        print(f"run         {payload['run_id'] or MISSING}", file=out)
        if outcome.failure_code is not None:
            print(f"failure     {outcome.failure_code}", file=out)
        if outcome.detail is not None:
            print(f"detail      {outcome.detail}", file=out)
    return ExitCode.OK if outcome.failure_code is None else ExitCode.REFUSED


async def settle(
    session: AsyncSession,
    sessions: async_sessionmaker[AsyncSession],
    provider: PaymentProvider,
    arguments: argparse.Namespace,
    out: TextIO,
    settings: Settings,
) -> int:
    """Close the merchant launch behind a run that has already finished.

    Idempotent: a launch that is already settled is reported as it stands. Refused while the run
    is still going, because a launch that names a run nobody has closed is not stranded, it is
    running.
    """
    del sessions, provider, settings
    merchant_id = await _benchmark_merchant(session, read_world(arguments.world))
    run = await BenchmarkRunService(session).load(arguments.run_id, merchant_id=merchant_id)
    if not run.is_terminal:
        print(
            f"refused     benchmark run {run.id} is {run.status.value} and has not finished",
            file=out,
        )
        return ExitCode.REFUSED
    settled = await EvaluationLaunchWorkerService(session).settle_for_terminal_run(run.id)
    payload = {
        "run_id": str(run.id),
        "status": run.status.value,
        "launch_id": None if settled is None else str(settled.id),
        "launch_status": None if settled is None else settled.status.value,
    }
    if arguments.as_json:
        write_json(out, payload)
        return ExitCode.OK

    print(f"run         {payload['run_id']}  {payload['status']}", file=out)
    if settled is None:
        print("launch      this run belongs to no merchant evaluation launch", file=out)
    else:
        print(
            f"launch      {payload['launch_id']} is {str(payload['launch_status']).lower()}",
            file=out,
        )
    return ExitCode.OK


async def compare_create(
    session: AsyncSession,
    sessions: async_sessionmaker[AsyncSession],
    provider: PaymentProvider,
    arguments: argparse.Namespace,
    out: TextIO,
    settings: Settings,
) -> int:
    """Persist all paired sample identities before any live model call is possible."""
    del sessions, provider, settings
    world = read_world(arguments.world)
    merchant_id = await _benchmark_merchant(session, world)
    suite = await BenchmarkSuiteRepository(session).get(world.suite.key, world.suite.version)
    if suite is None:
        raise NotFoundError("benchmark_suite", world.suite.label)
    configuration = AgentConfiguration(provider=arguments.provider, requested_model=arguments.model)
    environment = await BenchmarkEnvironmentService(session).require_registered(world.fixture)
    experiment = await CompilerImpactExperimentService(session).create(
        merchant_id=merchant_id,
        suite_id=suite.id,
        environment=environment,
        source_snapshot_id=arguments.source_snapshot_id,
        compiled_representation_id=arguments.compiled_representation_id,
        buyer_configuration=configuration.payload(),
        buyer_configuration_digest=configuration.configuration_digest,
        sample_count=arguments.sample_count,
        development_benchmark=not arguments.evaluation,
    )
    payload = {
        "experiment_id": str(experiment.id),
        "benchmark": world.suite.label,
        "source_snapshot_id": str(experiment.source_snapshot_id),
        "compiled_representation_id": str(experiment.compiled_representation_id),
        "buyer_configuration_digest": experiment.buyer_configuration_digest,
        "buyer_configuration": experiment.buyer_configuration,
        "sample_count_per_representation": experiment.sample_count,
        "benchmark_designation": experiment.methodology["benchmark_designation"],
        "pair_order": experiment.methodology["pair_order"],
    }
    if arguments.as_json:
        write_json(out, payload)
    else:
        print(f"experiment  {payload['experiment_id']}", file=out)
        print(
            f"benchmark   {payload['benchmark']} {payload['benchmark_designation'].lower()}",
            file=out,
        )
        print(f"raw source  {payload['source_snapshot_id']}", file=out)
        print(f"compiled    {payload['compiled_representation_id']}", file=out)
        print(f"buyer       {payload['buyer_configuration_digest']}", file=out)
        print(
            f"samples     {payload['sample_count_per_representation']} per representation", file=out
        )
        print(f"pair order  {payload['pair_order']}", file=out)
    return ExitCode.OK


def _treatment_discovery(treatment: ExperimentTreatment) -> dict[str, Any]:
    """The discovery surface one frozen sample must be run against.

    Decided by the sample's persisted arm identity and nothing else, so neither arm can be
    built with the other's surface by accident or by a caller's preference. A raw sample gets
    the ordinary storefront; a compiled sample gets exactly its pinned representation.
    """
    return to_payload(
        buyer_discovery_view(
            representation_kind=treatment.sample.representation_kind.value,
            representation_id=treatment.sample.representation_id,
            representation_payload=(
                None if treatment.representation is None else treatment.representation.payload
            ),
        )
    )


async def compare_run(
    session: AsyncSession,
    sessions: async_sessionmaker[AsyncSession],
    provider: PaymentProvider,
    arguments: argparse.Namespace,
    out: TextIO,
    settings: Settings,
) -> int:
    """Execute exactly one next sample, preserving the persisted alternating order."""
    world = read_world(arguments.world)
    merchant_id = await _benchmark_merchant(session, world)
    experiments = CompilerImpactExperimentService(session)
    treatment = await experiments.next_treatment(merchant_id, arguments.experiment_id)
    environment = await BenchmarkEnvironmentService(session).require_registered(world.fixture)
    if environment.id != treatment.experiment.environment_id:
        raise ValueError(
            "benchmark world does not match the predeclared compiler impact experiment"
        )
    configuration = AgentConfiguration.from_payload(treatment.experiment.buyer_configuration)
    _require_provider_key(settings, configuration.provider)
    if configuration.configuration_digest != treatment.experiment.buyer_configuration_digest:
        raise ValueError("compiler impact experiment buyer configuration digest is invalid")
    identity = ExecutorIdentity(
        kind=executor_kind(configuration), version=1, revision=configuration.configuration_digest
    )
    service = BenchmarkRunService(session)
    served = RequestLedger()
    async with LocalCommerceEndpoint(settings, provider=provider, observer=served) as endpoint:
        started = await service.start_suite(
            suite_key=world.suite.key,
            suite_version=world.suite.version,
            fixture=world.fixture,
            executor=identity,
            representation_label="merchant-information",
            agent_configuration=configuration.payload(),
            representation=treatment.representation,
        )
        await experiments.bind_run(treatment, started.id)
        capability = BenchmarkRunCapability(merchant_id=merchant_id, run_id=started.id)
        trusted = MerchantBuyerSurface(
            sessions, merchant_id=merchant_id, provider=provider, benchmark_capability=capability
        )
        async with issued_benchmark_credential(
            MerchantCredentialService(session),
            capability=capability,
            marker=TokenMarker.of(settings.environment),
        ) as token:
            worker_environment = provider_worker_environment(settings, configuration.provider)
            executor = IsolatedMissionExecutor(
                base_url=endpoint.base_url,
                token=token,
                served=served,
                strategy=LLM_STRATEGY,
                provision_mandate=lambda brief: provision(trusted, brief),
                agent_configuration=configuration.payload(),
                merchant_information=treatment.projection,
                discovery=_treatment_discovery(treatment),
                environment=worker_environment,
            )
            executor.identity = identity
            finished = await service.execute_started_suite(
                started.id,
                executor,
                merchant_id=merchant_id,
                fixture=world.fixture,
                witness=executor,
            )
    representation_kind = treatment.sample.representation_kind.value
    payload: dict[str, str | int] = {
        "experiment_id": str(treatment.experiment.id),
        "sample_id": str(treatment.sample.id),
        "pair_ordinal": treatment.sample.pair_ordinal,
        "representation_kind": representation_kind,
        "run_id": str(finished.id),
    }
    if arguments.as_json:
        write_json(out, payload)
    else:
        print(f"experiment  {payload['experiment_id']}", file=out)
        print(
            f"sample      pair {payload['pair_ordinal']} {representation_kind.lower()}",
            file=out,
        )
        print(f"run         {payload['run_id']}", file=out)
    return ExitCode.OK


async def compare_show(
    session: AsyncSession,
    sessions: async_sessionmaker[AsyncSession],
    provider: PaymentProvider,
    arguments: argparse.Namespace,
    out: TextIO,
    settings: Settings,
) -> int:
    """Report every persisted sample without merging away stochastic executions."""
    del sessions, provider, settings
    world = read_world(arguments.world)
    merchant_id = await _benchmark_merchant(session, world)
    experiments = CompilerImpactExperimentService(session)
    experiment = await experiments.get(merchant_id, arguments.experiment_id)
    samples = await experiments.samples(merchant_id, experiment.id)
    runs = BenchmarkRunService(session)
    reports: list[dict[str, Any]] = []
    for sample in samples:
        report: dict[str, Any] = {
            "sample_id": str(sample.id),
            "pair_ordinal": sample.pair_ordinal,
            "representation_kind": sample.representation_kind.value,
            "source_snapshot_id": _optional(sample.source_snapshot_id),
            "representation_id": _optional(sample.representation_id),
            "run_id": _optional(sample.run_id),
        }
        if sample.run_id is not None:
            loaded = await runs.load(sample.run_id, merchant_id=merchant_id)
            metrics = await runs.metrics(sample.run_id, merchant_id=merchant_id)
            suite, environment = await _pins(runs, loaded)
            report["run"] = _run_json(loaded, metrics, suite, environment)
            usage_rows = await session.execute(
                select(
                    AgentProviderUsage.actual_model,
                    AgentProviderUsage.input_tokens,
                    AgentProviderUsage.output_tokens,
                    AgentProviderUsage.reasoning_tokens,
                    AgentProviderUsage.total_tokens,
                    AgentProviderUsage.provider_latency_ms,
                ).where(
                    AgentProviderUsage.run_id == sample.run_id,
                    AgentProviderUsage.merchant_id == merchant_id,
                )
            )
            usage = cast(list[ProviderUsageRow], list(usage_rows))
            report["resolved_models"] = sorted({row[0] for row in usage if row[0] is not None})
            report["provider_usage"] = _provider_usage_summary(usage)
            # A provider-health diagnostic beside the semantic metrics, never inside them. A
            # mission the model never got to reason about is a throttled sample, and reading it
            # as a raw or compiled outcome would be a fact about quota rather than commerce.
            report["provider_failure_missions"] = await _provider_failed_missions(
                session, run_id=sample.run_id, merchant_id=merchant_id
            )
        reports.append(report)
    aggregates = _comparison_aggregates(reports)
    transitions = _mission_transitions(reports)
    designation = experiment.methodology.get("benchmark_designation", "DEVELOPMENT")
    # The same deterministic reading the insights API serves, so an operator and a merchant
    # never disagree about what an experiment's own caveats are.
    from agentrank_api.diagnostics.service import DiagnosticsService

    diagnosed = await DiagnosticsService(session).experiment_diagnosis(
        arguments.experiment_id, merchant_id=merchant_id
    )
    payload: dict[str, Any] = {
        "title": "Compiler Impact Experiment",
        "benchmark_designation": designation,
        "pair_order": experiment.methodology.get("pair_order", "raw_then_compiled"),
        "experiment_id": str(experiment.id),
        "buyer_configuration_digest": experiment.buyer_configuration_digest,
        "source_snapshot_id": str(experiment.source_snapshot_id),
        "compiled_representation_id": str(experiment.compiled_representation_id),
        "environment_id": str(experiment.environment_id),
        "samples": reports,
        "aggregates": aggregates,
        "delta": _comparison_delta(aggregates),
        "mission_transitions": transitions,
        "methodology_warnings": [
            {"code": warning.code, "message": warning.message}
            for warning in diagnosed.diagnosis.warnings
        ],
        "conclusion": {
            "kind": diagnosed.diagnosis.conclusion.kind,
            "statement": diagnosed.diagnosis.conclusion.statement,
        },
    }
    if arguments.as_json:
        write_json(out, payload)
    else:
        print("Compiler Impact Experiment", file=out)
        print(f"benchmark   {str(designation).lower()}", file=out)
        print(f"experiment  {payload['experiment_id']}", file=out)
        print(f"buyer       {payload['buyer_configuration_digest']}", file=out)
        for warning in payload["methodology_warnings"]:
            print(f"warning     [{warning['code']}] {warning['message']}", file=out)
        print(
            f"conclusion  [{payload['conclusion']['kind']}] {payload['conclusion']['statement']}",
            file=out,
        )
        for report in reports:
            print(
                f"sample      pair {report['pair_ordinal']}"
                f" {str(report['representation_kind']).lower()}"
                f" run {report['run_id'] or MISSING}",
                file=out,
            )
        for kind, aggregate in aggregates.items():
            print(
                f"{kind.lower():<11}{aggregate['completed_samples']}/{aggregate['planned_samples']}"
                f" samples, completion mean {aggregate['task_completion_rate_mean']}",
                file=out,
            )
        for demand in payload["delta"]["simulated_demand_by_currency"]:
            print(
                f"delta       {demand['currency']} simulated captured"
                f" {demand['captured_amount_minor']}",
                file=out,
            )
    return ExitCode.OK


async def diagnose(
    session: AsyncSession,
    sessions: async_sessionmaker[AsyncSession],
    provider: PaymentProvider,
    arguments: argparse.Namespace,
    out: TextIO,
    settings: Settings,
) -> int:
    """One run's deterministic diagnosis, for the operator and the merchant alike."""
    del sessions, provider, settings
    from agentrank_api.diagnostics.schemas import (
        RunDiagnosticsView,
    )
    from agentrank_api.diagnostics.service import DiagnosticsService

    merchant_id = await _benchmark_merchant(session, read_world(arguments.world))
    diagnostics = await DiagnosticsService(session).run_diagnostics(
        arguments.run_id, merchant_id=merchant_id
    )

    if arguments.as_json:
        write_json(out, RunDiagnosticsView.from_domain(diagnostics).model_dump(mode="json"))
        return ExitCode.OK

    health = diagnostics.provider_health
    print(f"run         {diagnostics.run_id}", file=out)
    print(f"status      {diagnostics.status}   suite {diagnostics.suite_label}", file=out)
    print(f"engine      {diagnostics.engine_identity}", file=out)
    verified = diagnostics.catalog_pin_verified
    pin = MISSING if verified is None else str(verified).lower()
    print(f"catalog pin verified: {pin}", file=out)
    print(
        f"provider    {health.missions_with_provider_errors} mission(s) with provider errors,"
        f" {health.terminated_outages} outage(s) ended missions,"
        f" {health.recovered_throttles} throttle(s) recovered",
        file=out,
    )
    if not diagnostics.findings:
        print("findings    none", file=out)
    for finding in diagnostics.findings:
        demand = (
            ", ".join(
                f"{effect.bucket.lower()} simulated demand"
                f" {effect.amount_minor} minor units {effect.currency}"
                for effect in finding.simulated_demand
            )
            or "no simulated demand attributed to this finding's lead diagnosis"
        )
        print(
            f"finding     [{finding.severity.value}] {finding.title}"
            f" owner={finding.owner.value} action={finding.actionability.value}",
            file=out,
        )
        print(f"            demand: {demand}", file=out)
        if finding.recommendation is not None:
            print(f"            action: {finding.recommendation}", file=out)
    print("", file=out)
    diagnosis_width = 24
    print(
        f"{'MISSION':<{KEY_WIDTH}} {'STATUS':<{STATUS_WIDTH}}"
        f" {'PRIMARY DIAGNOSIS':<{diagnosis_width}}",
        file=out,
    )
    for entry in diagnostics.missions:
        lead = MISSING if entry.primary is None else entry.primary.code.value
        # Truncated rather than padded past: a code longer than the column breaks the table.
        print(
            f"{entry.mission_key:<{KEY_WIDTH}} {entry.status.value:<{STATUS_WIDTH}}"
            f" {lead[:diagnosis_width]:<{diagnosis_width}}",
            file=out,
        )
    return ExitCode.OK


def _comparison_aggregates(reports: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for kind in ("RAW", "COMPILED"):
        group = [report for report in reports if report["representation_kind"] == kind]
        completed_reports = [
            report for report in group if report.get("run", {}).get("status") == "COMPLETED"
        ]
        completed = [report["run"]["metrics"] for report in completed_reports]
        rates = [
            entry["task_completion_rate"]
            for entry in completed
            if entry["task_completion_rate"] is not None
        ]
        result[kind] = {
            "planned_samples": len(group),
            "completed_samples": len(completed),
            "task_completion_rate_mean": None if not rates else sum(rates) / len(rates),
            "task_completion_rate_min": None if not rates else min(rates),
            "task_completion_rate_max": None if not rates else max(rates),
            "metric_totals": _metric_totals(completed),
            "primary_failure_counts": _failure_totals(completed),
            "resolved_models": sorted(
                {
                    model
                    for report in completed_reports
                    for model in report.get("resolved_models", [])
                }
            ),
            "simulated_demand_by_currency": _aggregate_simulated_demand(completed_reports),
        }
    return result


def _metric_totals(completed: list[dict[str, Any]]) -> dict[str, int]:
    names = (
        "missions_total",
        "missions_succeeded",
        "missions_failed",
        "missions_abstained",
        "missions_errored",
        "missions_unfinished",
        "correct_abstentions",
        "incorrect_abstentions",
        "unsafe_attempts",
        "unverified_attempts",
        "unsafe_completions",
        "oracle_disagreements",
    )
    return {name: sum(entry[name] for entry in completed) for name in names}


def _failure_totals(completed: list[dict[str, Any]]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for entry in completed:
        for reason, count in entry["primary_failure_counts"].items():
            totals[reason] = totals.get(reason, 0) + count
    return {reason: totals[reason] for reason in sorted(totals)}


async def _provider_failed_missions(
    session: AsyncSession, *, run_id: uuid.UUID, merchant_id: uuid.UUID
) -> int:
    """How many of one run's missions recorded a provider failure in their trusted traces.

    This is a diagnostic count and never a metric input. It exists so a comparison can say how
    much of each arm's end to end result the model was actually given the chance to produce.
    """
    counted = await session.execute(
        select(func.count(func.distinct(AgentTraceEvent.mission_run_id))).where(
            AgentTraceEvent.run_id == run_id,
            AgentTraceEvent.merchant_id == merchant_id,
            AgentTraceEvent.event_type == "PROVIDER_ERROR",
        )
    )
    return int(counted.scalar_one())


def _provider_usage_summary(rows: list[ProviderUsageRow]) -> dict[str, int | None]:
    """Summarize provider usage without turning an omitted token count into zero."""

    def total(values: list[int | None]) -> int | None:
        if not values or any(value is None for value in values):
            return None
        return sum(value for value in values if value is not None)

    return {
        "invocations": len(rows),
        "input_tokens": total([row[1] for row in rows]),
        "output_tokens": total([row[2] for row in rows]),
        "reasoning_tokens": total([row[3] for row in rows]),
        "total_tokens": total([row[4] for row in rows]),
        "provider_latency_ms": total([row[5] for row in rows]),
    }


def _aggregate_simulated_demand(group: list[dict[str, Any]]) -> list[dict[str, int | str]]:
    totals: dict[str, dict[str, int]] = {}
    for report in group:
        if "run" not in report:
            continue
        for entry in report["run"]["simulated_demand"]:
            currency = entry["currency"]
            current = totals.setdefault(
                currency,
                {
                    "potential_amount_minor": 0,
                    "captured_amount_minor": 0,
                    "lost_amount_minor": 0,
                    "not_measured_amount_minor": 0,
                },
            )
            for key in current:
                current[key] += entry[key]
    return [{"currency": currency, **totals[currency]} for currency in sorted(totals)]


def _comparison_delta(aggregates: dict[str, dict[str, Any]]) -> dict[str, Any]:
    raw, compiled = aggregates["RAW"], aggregates["COMPILED"]
    raw_demand = {entry["currency"]: entry for entry in raw["simulated_demand_by_currency"]}
    compiled_demand = {
        entry["currency"]: entry for entry in compiled["simulated_demand_by_currency"]
    }
    demand_delta = []
    for currency in sorted(raw_demand.keys() | compiled_demand.keys()):
        baseline, treatment = raw_demand.get(currency, {}), compiled_demand.get(currency, {})
        demand_delta.append(
            {
                "currency": currency,
                "potential_amount_minor": treatment.get("potential_amount_minor", 0)
                - baseline.get("potential_amount_minor", 0),
                "captured_amount_minor": treatment.get("captured_amount_minor", 0)
                - baseline.get("captured_amount_minor", 0),
                "lost_amount_minor": treatment.get("lost_amount_minor", 0)
                - baseline.get("lost_amount_minor", 0),
                "not_measured_amount_minor": treatment.get("not_measured_amount_minor", 0)
                - baseline.get("not_measured_amount_minor", 0),
            }
        )
    raw_rate, compiled_rate = (
        raw["task_completion_rate_mean"],
        compiled["task_completion_rate_mean"],
    )
    return {
        "task_completion_rate_mean": (
            None if raw_rate is None or compiled_rate is None else compiled_rate - raw_rate
        ),
        "simulated_demand_by_currency": demand_delta,
        "metric_totals": {
            name: compiled["metric_totals"][name] - raw["metric_totals"][name]
            for name in raw["metric_totals"]
        },
        "resolved_model_mismatch": raw["resolved_models"] != compiled["resolved_models"],
    }


def _mission_transitions(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_pair: dict[int, dict[str, dict[str, Any]]] = {}
    for report in reports:
        if report.get("run", {}).get("status") == "COMPLETED":
            by_pair.setdefault(report["pair_ordinal"], {})[report["representation_kind"]] = report
    transitions: list[dict[str, Any]] = []
    for pair, arms in sorted(by_pair.items()):
        if set(arms) != {"RAW", "COMPILED"}:
            continue
        raw = {mission["mission_key"]: mission for mission in arms["RAW"]["run"]["missions"]}
        compiled = {
            mission["mission_key"]: mission for mission in arms["COMPILED"]["run"]["missions"]
        }
        transitions.extend(
            {
                "pair_ordinal": pair,
                "mission_key": key,
                "raw": {
                    "status": raw[key]["status"],
                    "failure": raw[key]["primary_failure_reason"],
                },
                "compiled": {
                    "status": compiled[key]["status"],
                    "failure": compiled[key]["primary_failure_reason"],
                },
            }
            for key in sorted(raw.keys() & compiled.keys())
        )
    return transitions


async def show(
    session: AsyncSession,
    sessions: async_sessionmaker[AsyncSession],
    provider: PaymentProvider,
    arguments: argparse.Namespace,
    out: TextIO,
    settings: Settings,
) -> int:
    """One run, its pins, its metrics and every mission outcome."""
    del sessions, provider, settings
    service = BenchmarkRunService(session)
    merchant_id = await _benchmark_merchant(session, read_world(arguments.world))
    return await _report(service, arguments.run_id, merchant_id, arguments, out)


async def abort(
    session: AsyncSession,
    sessions: async_sessionmaker[AsyncSession],
    provider: PaymentProvider,
    arguments: argparse.Namespace,
    out: TextIO,
    settings: Settings,
) -> int:
    """Close a run that stopped, and say what state it is being closed in.

    A run that has already finished is refused rather than closed again, which is the existing
    guarantee and stays. The launch behind a run that finished without being settled has its own
    command: see `settle`.
    """
    del sessions, provider, settings
    service = BenchmarkRunService(session)
    merchant_id = await _benchmark_merchant(session, read_world(arguments.world))
    before = await service.load(arguments.run_id, merchant_id=merchant_id)
    unfinished = [result for result in before.mission_runs if not result.is_terminal]
    closed = await service.abort_run(arguments.run_id, merchant_id=merchant_id)
    # A run an operator has closed is not coming back, so the merchant launch behind it is over
    # too. Leaving it executing would hold this merchant's one pending slot against nothing.
    settled = await EvaluationLaunchWorkerService(session).settle_for_terminal_run(closed.id)

    payload = {
        "run_id": str(closed.id),
        "launch_id": None if settled is None else str(settled.id),
        "launch_status": None if settled is None else settled.status.value,
        "status": closed.status.value,
        "missions_unfinished": len(unfinished),
        "missions_started_and_unfinished": sum(
            1 for result in unfinished if result.status is MissionRunStatus.RUNNING
        ),
    }
    if arguments.as_json:
        write_json(out, payload)
        return ExitCode.OK

    print(f"run         {payload['run_id']}", file=out)
    print(f"status      {payload['status']}", file=out)
    if payload["launch_id"] is not None:
        print(
            f"launch      {payload['launch_id']} is {str(payload['launch_status']).lower()}",
            file=out,
        )
    print(
        f"unfinished  {payload['missions_unfinished']} missions never reached an outcome", file=out
    )
    started = payload["missions_started_and_unfinished"]
    if started:
        print(
            f"warning     {started} missions were started and never finished. They may have"
            " created a quote, held stock or dispatched a payment, and nothing here replays"
            " one. Inspect them with `agentrank_api.cli payments`",
            file=out,
        )
    return ExitCode.OK


async def _benchmark_merchant(session: AsyncSession, world: AuthoredWorld) -> uuid.UUID:
    """The merchant these commands are about, which is the one the world describes.

    Every read on the run service takes a merchant and puts it in the query, which is the rule
    that makes a run identifier worth nothing to anybody who is not its merchant. An operator
    command line is not a merchant and holds no credential, so it resolves one the only honest
    way available: from the benchmark world these commands are for. A run belonging to anybody
    else is not found here, exactly as it would not be over HTTP.
    """
    merchant = await MerchantRepository(session).get_by_slug(world.merchant_slug)
    if merchant is None:
        raise NotFoundError("merchant", world.merchant_slug)
    return merchant.id


def _require_provider_key(settings: Settings, provider: str) -> None:
    if provider == OPENAI_PROVIDER and settings.openai is None:
        raise ValueError("OPENAI_API_KEY is required for an OpenAI LLM benchmark sample")
    if provider == GEMINI_PROVIDER and settings.gemini is None:
        raise ValueError("GEMINI_API_KEY is required for a Gemini LLM benchmark sample")


async def _report(
    service: BenchmarkRunService,
    run_id: uuid.UUID,
    merchant_id: uuid.UUID,
    arguments: argparse.Namespace,
    out: TextIO,
) -> int:
    """Print one run, whether it was just executed or read back."""
    loaded = await service.load(run_id, merchant_id=merchant_id)
    metrics = await service.metrics(run_id, merchant_id=merchant_id)
    suite, environment = await _pins(service, loaded)
    if arguments.as_json:
        write_json(out, _run_json(loaded, metrics, suite, environment))
        return ExitCode.OK
    _render(loaded, metrics, suite, environment, out)
    return ExitCode.OK


async def _pins(service: BenchmarkRunService, loaded: BenchmarkRun) -> tuple[str, str | None]:
    """The suite this run executed and the world it ran against, as labels.

    Read rather than assumed from the command's own defaults. A run this command did not start
    could name a suite version these commands no longer default to, and a report that printed
    what it expected instead of what the row says would be the wrong kind of report entirely.
    """
    suite = await service.suite_label(loaded)
    environment = await service.environment_label(loaded)
    return suite, environment


def _run_json(
    loaded: BenchmarkRun, metrics: BenchmarkMetrics, suite: str, environment: str | None
) -> dict[str, Any]:
    demand = [
        {
            "currency": entry.currency,
            "potential_amount_minor": entry.potential_amount_minor,
            "captured_amount_minor": entry.captured_amount_minor,
            "lost_amount_minor": entry.lost_amount_minor,
            "not_measured_amount_minor": entry.not_measured_amount_minor,
        }
        for entry in metrics.simulated_demand.by_currency
    ]
    return {
        "disclaimer": DISCLAIMER,
        "run_id": str(loaded.id),
        "status": loaded.status.value,
        # Every pin the comparison rule in docs/benchmark.md names, and in the order it names
        # them. The first version of this report printed the executor, the catalog hash and the
        # representation label, which is the one the same document calls never evidence, and
        # omitted the two that say which missions ran and against which world. An operator
        # following that rule with this command could check half of it.
        "suite": suite,
        "environment": environment,
        "executor": loaded.executor_label,
        "executor_revision": loaded.executor_revision,
        "catalog_hash": loaded.catalog_hash,
        "evaluator_version": loaded.evaluator_version,
        "representation_label": loaded.representation_label,
        "metrics": {
            "missions_total": metrics.missions_total,
            "missions_succeeded": metrics.missions_succeeded,
            "missions_failed": metrics.missions_failed,
            "missions_abstained": metrics.missions_abstained,
            "missions_errored": metrics.missions_errored,
            "missions_unfinished": metrics.missions_unfinished,
            "purchase_missions": metrics.purchase_missions,
            "control_missions": metrics.control_missions,
            "correct_abstentions": metrics.correct_abstentions,
            "incorrect_abstentions": metrics.incorrect_abstentions,
            "task_completion_rate": metrics.task_completion_rate,
            "correct_abstention_rate": metrics.correct_abstention_rate,
            "unsafe_attempts": metrics.unsafe_attempts,
            "unverified_attempts": metrics.unverified_attempts,
            "unsafe_completions": metrics.unsafe_completions,
            "oracle_disagreements": metrics.oracle_disagreements,
            "primary_failure_counts": {
                reason.value: count for reason, count in metrics.primary_failure_counts.items()
            },
        },
        # Simulated buyer demand, authored with the suite. Never revenue, never a forecast and
        # never a measured business result. See docs/benchmark.md.
        "simulated_demand": demand,
        "missions": [_mission_json(result) for result in loaded.mission_runs],
    }


def _mission_json(result: BenchmarkMissionRun) -> dict[str, Any]:
    return {
        "mission_key": result.mission.mission_key,
        "status": result.status.value,
        "primary_failure_reason": (
            None if result.primary_failure_reason is None else result.primary_failure_reason.value
        ),
        "additional_failure_reasons": [reason.value for reason in result.failure_reasons[1:]],
        "unsafe_attempt": result.unsafe_attempt,
        "unverified_attempt": result.unverified_attempt,
        "unsafe_completion": result.unsafe_completion,
        "oracle_confirmed": result.oracle_confirmed,
        "selected_variant_id": _optional(result.selected_variant_id),
        "selected_quantity": result.selected_quantity,
        "checkout_id": _optional(result.checkout_id),
        "payment_attempt_id": _optional(result.payment_attempt_id),
    }


def _optional(value: uuid.UUID | None) -> str | None:
    return None if value is None else str(value)


def _render(
    loaded: BenchmarkRun,
    metrics: BenchmarkMetrics,
    suite: str,
    environment: str | None,
    out: TextIO,
) -> None:
    """One run as a person reads it.

    The executor is on the second line and is named as what it is. A report that did not say
    what produced these numbers would invite them being read as agent performance, which they
    are not.
    """
    print(f"run         {loaded.id}", file=out)
    print(f"executor    {loaded.executor_label or MISSING}   {DISCLAIMER}", file=out)
    # The digest beside the declared version, because a version is a promise a person keeps and
    # this moves whether or not anybody remembered to. It says two runs came from different code
    # and never that the behavior changed.
    print(f"revision    {loaded.executor_revision or MISSING}", file=out)
    print(f"status      {loaded.status.value}", file=out)
    print(f"suite       {suite}", file=out)
    print(f"world       {environment or MISSING}", file=out)
    print(f"catalog     {loaded.catalog_hash or MISSING}", file=out)
    print(f"evaluator   {loaded.evaluator_version or MISSING}", file=out)
    print(f"label       {loaded.representation_label or MISSING}", file=out)
    print("", file=out)
    print(
        f"missions    {metrics.missions_total} total,"
        f" {metrics.missions_succeeded} succeeded,"
        f" {metrics.missions_failed} failed,"
        f" {metrics.missions_abstained} abstained,"
        f" {metrics.missions_errored} errored,"
        f" {metrics.missions_unfinished} unfinished",
        file=out,
    )
    print(
        f"completion  {metrics.missions_succeeded}/{metrics.purchase_missions} purchasable"
        f" missions, {_rate(metrics.task_completion_rate)}",
        file=out,
    )
    print(
        f"abstention  {metrics.correct_abstentions}/{metrics.control_missions} control"
        f" missions, {_rate(metrics.correct_abstention_rate)}",
        file=out,
    )
    print(
        f"safety      {metrics.unsafe_attempts} unsafe attempts,"
        f" {metrics.unverified_attempts} unverified,"
        f" {metrics.unsafe_completions} escapes",
        file=out,
    )
    print(f"oracle      {metrics.oracle_disagreements} disagreements with the catalog", file=out)
    for entry in metrics.simulated_demand.by_currency:
        print(
            f"demand      {entry.currency} simulated:"
            f" {entry.captured_amount_minor} captured of"
            f" {entry.potential_amount_minor} potential,"
            f" {entry.lost_amount_minor} lost,"
            f" {entry.not_measured_amount_minor} not measured",
            file=out,
        )
    print("", file=out)
    print(
        f"{'MISSION':<{KEY_WIDTH}} {'STATUS':<{STATUS_WIDTH}} {'REASON':<{REASON_WIDTH}}",
        file=out,
    )
    for result in loaded.mission_runs:
        reason = (
            MISSING
            if result.primary_failure_reason is None
            else result.primary_failure_reason.value
        )
        print(
            f"{result.mission.mission_key:<{KEY_WIDTH}}"
            f" {result.status.value:<{STATUS_WIDTH}}"
            f" {reason:<{REASON_WIDTH}}",
            file=out,
        )


def _rate(value: float | None) -> str:
    """A rate, or an honest absence.

    None means the denominator was zero, which is not the same as zero and must not print as
    it: a suite with no control missions has no correct abstention rate at all.
    """
    return MISSING if value is None else f"{value:.2f}"
