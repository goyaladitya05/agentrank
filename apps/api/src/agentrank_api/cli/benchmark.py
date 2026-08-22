"""The benchmark operator commands: prepare a world, run a suite, read a result, close a run.

Four commands, and the same three steps in each: parse what the operator typed, call one
application service, print what came back. There is no SQL here, no lock, no transaction and no
rule about what a mission means, because every one of those already exists and a second copy
inside a command would be a second answer to a question that must have exactly one.

The names say what moves:

```text
seed     registers the world, puts it back, publishes the suite   catalog is overwritten
run      executes the suite                                       money moves, stock is consumed
show     reads one run and counts it                              nothing moves
abort    closes a run that stopped                                nothing moves
```

`run` is the one that spends. A benchmark mission that completes creates a mandate, quotes a
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

Only the VoltEdge world exists, so these commands take no fixture argument. A second world means
a `--fixture` flag resolving a label against the registry below, and nothing else.
"""

import argparse
import uuid
from typing import Any, TextIO

from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.benchmark.buyer import MerchantBuyerSurface
from agentrank_api.benchmark.fixtures import BenchmarkFixture
from agentrank_api.benchmark.lifecycle import MissionRunStatus
from agentrank_api.benchmark.metrics import BenchmarkMetrics
from agentrank_api.benchmark.models import BenchmarkMissionRun, BenchmarkRun
from agentrank_api.benchmark.reference_executor import ReferenceMissionExecutor
from agentrank_api.benchmark.runner import BenchmarkRunService
from agentrank_api.benchmark.voltedge import FIXTURE, SUITE_KEY, SUITE_VERSION, seed_voltedge
from agentrank_api.cli.exits import ExitCode
from agentrank_api.cli.output import write_json
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.config import Settings
from agentrank_api.errors import NotFoundError
from agentrank_api.payments.provider import PaymentProvider

# The one world that exists. A second one becomes a `--fixture` flag and a mapping, and nothing
# here has to change shape for it.
WORLD: BenchmarkFixture = FIXTURE

# Column widths, so a mission line is one line on an ordinary terminal.
KEY_WIDTH = 30
STATUS_WIDTH = 10
REASON_WIDTH = 26

MISSING = "-"

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
    _add_json(closing)
    closing.set_defaults(command=abort)


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
    provider: PaymentProvider,
    arguments: argparse.Namespace,
    out: TextIO,
    settings: Settings,
) -> int:
    """Register the world, put it back, and publish the suite."""
    del provider, settings
    prepared, suite = await seed_voltedge(session)
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
    provider: PaymentProvider,
    arguments: argparse.Namespace,
    out: TextIO,
    settings: Settings,
) -> int:
    """Execute the suite, then print the result the same way `show` does.

    The provider is the one the application is wired with and is handed in rather than built
    here, which is the same rule every other command follows: a command that could construct a
    provider could be pointed at one the application is not running with.
    """
    del settings
    merchant_id = await _benchmark_merchant(session)
    service = BenchmarkRunService(session)
    surface = MerchantBuyerSurface(session, merchant_id=merchant_id, provider=provider)
    finished = await service.run_suite(
        ReferenceMissionExecutor(surface),
        suite_key=SUITE_KEY,
        suite_version=SUITE_VERSION,
        fixture=WORLD,
        representation_label=arguments.representation_label,
    )
    return await _report(service, finished.id, merchant_id, arguments, out)


async def show(
    session: AsyncSession,
    provider: PaymentProvider,
    arguments: argparse.Namespace,
    out: TextIO,
    settings: Settings,
) -> int:
    """One run, its pins, its metrics and every mission outcome."""
    del provider, settings
    service = BenchmarkRunService(session)
    merchant_id = await _benchmark_merchant(session)
    return await _report(service, arguments.run_id, merchant_id, arguments, out)


async def abort(
    session: AsyncSession,
    provider: PaymentProvider,
    arguments: argparse.Namespace,
    out: TextIO,
    settings: Settings,
) -> int:
    """Close a run that stopped, and say what state it is being closed in."""
    del provider, settings
    service = BenchmarkRunService(session)
    merchant_id = await _benchmark_merchant(session)
    before = await service.load(arguments.run_id, merchant_id=merchant_id)
    unfinished = [result for result in before.mission_runs if not result.is_terminal]
    closed = await service.abort_run(arguments.run_id, merchant_id=merchant_id)

    payload = {
        "run_id": str(closed.id),
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


async def _benchmark_merchant(session: AsyncSession) -> uuid.UUID:
    """The merchant these commands are about, which is the one the world describes.

    Every read on the run service takes a merchant and puts it in the query, which is the rule
    that makes a run identifier worth nothing to anybody who is not its merchant. An operator
    command line is not a merchant and holds no credential, so it resolves one the only honest
    way available: from the benchmark world these commands are for. A run belonging to anybody
    else is not found here, exactly as it would not be over HTTP.
    """
    merchant = await MerchantRepository(session).get_by_slug(WORLD.merchant_slug)
    if merchant is None:
        raise NotFoundError("merchant", WORLD.merchant_slug)
    return merchant.id


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
    if arguments.as_json:
        write_json(out, _run_json(loaded, metrics))
        return ExitCode.OK
    _render(loaded, metrics, out)
    return ExitCode.OK


def _run_json(loaded: BenchmarkRun, metrics: BenchmarkMetrics) -> dict[str, Any]:
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
        "executor": loaded.executor_label,
        "representation_label": loaded.representation_label,
        "catalog_hash": loaded.catalog_hash,
        "evaluator_version": loaded.evaluator_version,
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


def _render(loaded: BenchmarkRun, metrics: BenchmarkMetrics, out: TextIO) -> None:
    """One run as a person reads it.

    The executor is on the second line and is named as what it is. A report that did not say
    what produced these numbers would invite them being read as agent performance, which they
    are not.
    """
    print(f"run         {loaded.id}", file=out)
    print(f"executor    {loaded.executor_label or MISSING}   {DISCLAIMER}", file=out)
    print(f"status      {loaded.status.value}", file=out)
    print(f"label       {loaded.representation_label or MISSING}", file=out)
    print(f"catalog     {loaded.catalog_hash or MISSING}", file=out)
    print(f"evaluator   {loaded.evaluator_version or MISSING}", file=out)
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
