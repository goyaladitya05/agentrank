"""The benchmark command line, run for real against PostgreSQL and a configured fake provider.

Nothing is mocked away. `main` is called with the arguments an operator would type, it opens its
own engine against the test database, and every command reaches the real environment service, the
real run service, the real commerce path and the real payment kernel. The only injected things
are the settings, so the commands hit the test database, and the provider, because a declined
payment cannot be asked for from the outside.

The assertions are about behavior and about honesty of output. A report that did not say what
produced its numbers would invite them being read as agent performance, which they are not, so
the executor label and the disclaimer are asserted rather than left to a docstring.
"""

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from io import StringIO
from typing import cast

import pytest
from benchmark_support import VOLTEDGE, VOLTEDGE_DIRECTORY
from launch_support import with_openai, without_providers
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.auth.models import MerchantApiCredential
from agentrank_api.benchmark.dispatch import UNSERVICEABLE
from agentrank_api.benchmark.endpoint import CREDENTIAL_LABEL
from agentrank_api.benchmark.evaluation_launch import (
    BenchmarkEvaluationLaunch,
    EvaluationLaunchStatus,
)
from agentrank_api.benchmark.launch import MerchantEvaluationLaunchService
from agentrank_api.benchmark.lifecycle import BenchmarkRunStatus, MissionRunStatus
from agentrank_api.benchmark.models import BenchmarkRun
from agentrank_api.benchmark.runner import BenchmarkRunService
from agentrank_api.cli import ExitCode, main
from agentrank_api.cli.benchmark import DISCLAIMER
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.config import Settings
from agentrank_api.payments.fake import FakeOutcome, FakePaymentProvider
from agentrank_api.representation.fixtures import read_source
from agentrank_api.representation.service import MerchantRepresentationService

pytestmark = pytest.mark.anyio

MERCHANT_SLUG = VOLTEDGE.merchant_slug
SUITE_KEY = VOLTEDGE.suite.key
SUITE_VERSION = VOLTEDGE.suite.version


@dataclass(frozen=True, slots=True)
class Run:
    """What one command invocation produced: its exit code and both streams."""

    code: int
    out: str
    err: str

    def json(self) -> dict[str, object]:
        parsed: dict[str, object] = json.loads(self.out)
        return parsed


@pytest.fixture
def provider() -> FakePaymentProvider:
    return FakePaymentProvider()


async def cli(settings: Settings, provider: FakePaymentProvider, *arguments: str) -> Run:
    """Invoke the command line exactly as a shell would, and capture both streams.

    In a thread, because `main` owns its event loop: it calls `asyncio.run`, which is what a
    process entry point should do and what cannot be done from inside the loop a test is already
    running on.

    Every benchmark command that takes a world is given the authored one by absolute path unless
    the test names one itself. The default is relative to the working directory, which is the
    repository root for an operator and is whatever pytest was started from here, and a suite of
    tests that depended on that would be a suite that passes for a reason nobody chose. That the
    default resolves is asserted separately, once.

    `queue` reads the launch table deployment wide and `cancel` names one launch by identifier,
    so neither has a world and neither is given one: a flag added for uniformity would be a flag
    the command would have to accept and ignore.
    """
    named = list(arguments)
    worldless = (["queue"], ["cancel"], ["provider"], ["provider-set"], ["usage"])
    takes_world = named[:1] == ["benchmark"] and named[1:2] not in worldless
    if takes_world and "--world" not in named:
        named += ["--world", str(VOLTEDGE_DIRECTORY)]
    out, err = StringIO(), StringIO()
    code = await asyncio.to_thread(
        main, named, settings=settings, provider=provider, out=out, err=err
    )
    return Run(code=code, out=out.getvalue(), err=err.getvalue())


@pytest.fixture(autouse=True)
async def isolated(session: AsyncSession) -> AsyncSession:
    """Every test here leaves the database empty behind it.

    The commands open their own engine against the same database, so a test that did not take
    the session fixture would leave its run for the next one to find. Requested by every test
    rather than by the ones that read rows, because which of those is which is not something to
    have to remember.
    """
    return session


async def only_run(session: AsyncSession) -> BenchmarkRun:
    return (await session.execute(select(BenchmarkRun))).scalars().one()


# Seeding.


async def test_seed_registers_the_world_and_publishes_the_suite(
    catalog_settings: Settings, provider: FakePaymentProvider, session: AsyncSession
) -> None:
    result = await cli(catalog_settings, provider, "benchmark", "seed", "--json")

    assert result.code == ExitCode.OK
    payload = result.json()
    assert payload["environment"] == "voltedge-catalog@1"
    assert payload["merchant_slug"] == MERCHANT_SLUG
    assert payload["suite"] == f"{SUITE_KEY}@{SUITE_VERSION}"
    assert str(payload["fixture_hash"]).startswith("sha256:")
    assert payload["products"] == 7
    assert payload["variants"] == 13
    merchant = await MerchantRepository(session).get_by_slug(MERCHANT_SLUG)
    assert merchant is not None


async def test_seeding_twice_creates_nothing_the_second_time(
    catalog_settings: Settings, provider: FakePaymentProvider
) -> None:
    await cli(catalog_settings, provider, "benchmark", "seed")
    result = await cli(catalog_settings, provider, "benchmark", "seed", "--json")

    payload = result.json()
    assert payload["rows_created"] == 0
    assert payload["holds_released"] == 0
    assert payload["variants_withdrawn"] == 0


# Running.


async def test_run_isolated_executes_the_whole_suite_in_separate_processes(
    catalog_settings: Settings, provider: FakePaymentProvider, session: AsyncSession
) -> None:
    """The reference result, produced through the boundary a model will use.

    Fourteen missions, fourteen worker processes, each with no database credential, reaching the
    merchant only over its own commerce API on a loopback port with an ephemeral credential. The
    outcome is the same as the in process path, which is the point: the boundary is a
    substitution rather than a different measurement.

    The executor is recorded as `reference-isolated` rather than `reference`, because two
    transports with different failure modes are two measurements even when they agree.
    """
    await cli(catalog_settings, provider, "benchmark", "seed")

    result = await cli(catalog_settings, provider, "benchmark", "run", "--isolated", "--json")

    assert result.code == ExitCode.OK
    payload = result.json()
    assert payload["executor"] == "reference-isolated-v1"
    assert payload["status"] == "COMPLETED"
    metrics = payload["metrics"]
    assert isinstance(metrics, dict)
    assert metrics["missions_total"] == 14
    assert metrics["missions_succeeded"] == 8
    assert metrics["missions_abstained"] == 6
    assert metrics["missions_failed"] == 0
    assert metrics["missions_errored"] == 0
    assert metrics["unsafe_attempts"] == 0
    assert metrics["unsafe_completions"] == 0
    finished = await only_run(session)
    assert finished.executor_kind == "reference-isolated"


async def test_an_isolated_run_leaves_no_usable_credential_behind(
    catalog_settings: Settings, provider: FakePaymentProvider, session: AsyncSession
) -> None:
    """The credential exists for the length of one run and is revoked whatever happens."""
    await cli(catalog_settings, provider, "benchmark", "seed")

    await cli(catalog_settings, provider, "benchmark", "run", "--isolated", "--json")

    session.expire_all()
    credentials = list((await session.execute(select(MerchantApiCredential))).scalars())
    minted = [issued for issued in credentials if issued.label == CREDENTIAL_LABEL]
    assert minted
    assert all(issued.revoked_at is not None for issued in minted)


async def test_run_executes_the_suite_and_says_what_produced_the_numbers(
    catalog_settings: Settings, provider: FakePaymentProvider, session: AsyncSession
) -> None:
    """The output honesty rule. These numbers came from a deterministic executor, not an agent."""
    await cli(catalog_settings, provider, "benchmark", "seed")

    result = await cli(
        catalog_settings, provider, "benchmark", "run", "--representation-label", "baseline"
    )

    assert result.code == ExitCode.OK
    assert "reference-v1" in result.out
    assert DISCLAIMER in result.out
    assert "AI agent" not in result.out
    assert "COMPLETED" in result.out
    assert f"{SUITE_KEY}@{SUITE_VERSION}" in result.out
    assert "voltedge-catalog@1" in result.out
    finished = await only_run(session)
    assert finished.status is BenchmarkRunStatus.COMPLETED
    assert finished.representation_label == "baseline"


async def test_run_reports_every_mission_and_its_outcome(
    catalog_settings: Settings, provider: FakePaymentProvider
) -> None:
    await cli(catalog_settings, provider, "benchmark", "seed")

    result = await cli(catalog_settings, provider, "benchmark", "run", "--json")

    payload = result.json()
    assert payload["disclaimer"] == DISCLAIMER
    assert payload["executor"] == "reference-v1"
    # Every pin the comparison rule names, including the two an earlier version of this report
    # left out while printing the label the same rule calls never evidence.
    assert payload["suite"] == f"{SUITE_KEY}@{SUITE_VERSION}"
    assert payload["environment"] == "voltedge-catalog@1"
    assert payload["catalog_hash"] is not None
    assert payload["evaluator_version"] is not None
    missions = payload["missions"]
    assert isinstance(missions, list)
    assert len(missions) == 14
    assert all(entry["status"] != MissionRunStatus.PENDING.value for entry in missions)


async def test_a_run_can_be_read_back_by_identifier(
    catalog_settings: Settings, provider: FakePaymentProvider, session: AsyncSession
) -> None:
    await cli(catalog_settings, provider, "benchmark", "seed")
    await cli(catalog_settings, provider, "benchmark", "run")
    finished = await only_run(session)

    result = await cli(catalog_settings, provider, "benchmark", "show", str(finished.id), "--json")

    assert result.code == ExitCode.OK
    payload = result.json()
    assert payload["run_id"] == str(finished.id)
    assert payload["status"] == BenchmarkRunStatus.COMPLETED.value
    assert payload["executor"] == "reference-v1"


async def test_a_run_that_does_not_exist_is_a_not_found_exit_code(
    catalog_settings: Settings, provider: FakePaymentProvider
) -> None:
    """The exit codes are part of the contract, because a script has to act on them."""
    await cli(catalog_settings, provider, "benchmark", "seed")

    result = await cli(catalog_settings, provider, "benchmark", "show", str(uuid.uuid7()))

    assert result.code == ExitCode.NOT_FOUND
    assert "not found" in result.err


async def test_a_declined_provider_still_produces_a_complete_run(
    catalog_settings: Settings, session: AsyncSession
) -> None:
    """A suite in which every payment declined is a finding, not a broken run.

    The run describes the whole workload, and the failures are reported as payment failures
    rather than as anything about the harness.
    """
    declining = FakePaymentProvider(default=FakeOutcome.DECLINE)
    await cli(catalog_settings, declining, "benchmark", "seed")

    result = await cli(catalog_settings, declining, "benchmark", "run", "--json")

    payload = result.json()
    metrics = payload["metrics"]
    assert isinstance(metrics, dict)
    assert metrics["missions_succeeded"] == 0
    assert metrics["missions_failed"] == 8
    assert metrics["correct_abstentions"] == 6
    assert metrics["missions_errored"] == 0
    finished = await only_run(session)
    assert finished.status is BenchmarkRunStatus.COMPLETED


# Closing a run that stopped.


async def test_abort_closes_a_stopped_run_and_warns_about_what_it_started(
    catalog_settings: Settings, provider: FakePaymentProvider, session: AsyncSession
) -> None:
    """A mission left RUNNING may have paid for something, and nothing here replays one."""
    await cli(catalog_settings, provider, "benchmark", "seed")
    merchant = await MerchantRepository(session).get_by_slug(MERCHANT_SLUG)
    assert merchant is not None
    service = BenchmarkRunService(session)
    started = await service.start_run(
        suite_key=SUITE_KEY, suite_version=SUITE_VERSION, merchant_slug=MERCHANT_SLUG
    )
    await service.start_mission(started.id, "black-100w-charger", merchant_id=merchant.id)

    result = await cli(catalog_settings, provider, "benchmark", "abort", str(started.id), "--json")

    assert result.code == ExitCode.OK
    payload = result.json()
    assert payload["status"] == BenchmarkRunStatus.ABORTED.value
    assert payload["missions_unfinished"] == 14
    assert payload["missions_started_and_unfinished"] == 1


async def test_aborting_a_finished_run_is_refused_rather_than_silently_ignored(
    catalog_settings: Settings, provider: FakePaymentProvider, session: AsyncSession
) -> None:
    await cli(catalog_settings, provider, "benchmark", "seed")
    await cli(catalog_settings, provider, "benchmark", "run")
    finished = await only_run(session)

    result = await cli(catalog_settings, provider, "benchmark", "abort", str(finished.id))

    assert result.code == ExitCode.REFUSED
    assert "run_already_finished" in result.err


async def test_a_mission_line_never_carries_how_the_mission_was_marked(
    catalog_settings: Settings, provider: FakePaymentProvider, caplog: pytest.LogCaptureFixture
) -> None:
    """A status and a failure reason are the oracle decoded, and an executor can read a log.

    An abstention with a reason means the ground truth said a purchase was available and one
    without means it said none was, so fourteen labelled mission lines are fourteen answers.
    Counts after the last mission cannot inform the run they describe and do not say which
    mission was which.
    """
    await cli(catalog_settings, provider, "benchmark", "seed")

    with caplog.at_level(logging.INFO, logger="agentrank_api.benchmark.runner"):
        await cli(catalog_settings, provider, "benchmark", "run")

    missions = [record for record in caplog.records if record.msg == "benchmark mission recorded"]
    assert len(missions) == 14
    for record in missions:
        assert not hasattr(record, "status")
        assert not hasattr(record, "primary_failure_reason")
        assert getattr(record, "mission_key", None)
    completed = [record for record in caplog.records if record.msg == "benchmark run completed"]
    assert [getattr(record, "missions_total", None) for record in completed] == [14]


# Diagnosing.


async def test_diagnose_reports_findings_ownership_and_provider_health(
    catalog_settings: Settings, provider: FakePaymentProvider, session: AsyncSession
) -> None:
    """The clean reference run reads as clean: no findings, no provider faults.

    The engine identity travels in the output, because a diagnosis without the version that
    produced it could not be interpreted after the engine's rules change.
    """
    await cli(catalog_settings, provider, "benchmark", "seed")
    executed = await cli(catalog_settings, provider, "benchmark", "run", "--json")
    run_id = str(executed.json()["run_id"])

    result = await cli(catalog_settings, provider, "benchmark", "diagnose", run_id, "--json")

    assert result.code == ExitCode.OK
    payload = result.json()
    health = cast(dict[str, object], payload["provider_health"])
    missions = cast(list[object], payload["missions"])
    assert str(payload["engine_identity"]).startswith("sha256:")
    assert payload["findings"] == []
    assert health["terminated_outages"] == 0
    assert health["recovered_throttles"] == 0
    assert len(missions) == 14


async def test_diagnose_prints_a_table_that_says_when_nothing_was_found(
    catalog_settings: Settings, provider: FakePaymentProvider, session: AsyncSession
) -> None:
    await cli(catalog_settings, provider, "benchmark", "seed")
    executed = await cli(catalog_settings, provider, "benchmark", "run", "--json")
    run_id = str(executed.json()["run_id"])

    result = await cli(catalog_settings, provider, "benchmark", "diagnose", run_id)

    assert result.code == ExitCode.OK
    assert "findings    none" in result.out
    assert "PRIMARY DIAGNOSIS" in result.out
    # A clean run attributes no demand to findings, and says so rather than printing a bare 0.
    assert "no simulated demand attributed to this finding's lead diagnosis" not in result.out


async def test_diagnose_answers_not_found_for_a_foreign_run(
    catalog_settings: Settings, provider: FakePaymentProvider
) -> None:
    await cli(catalog_settings, provider, "benchmark", "seed")

    result = await cli(
        catalog_settings,
        provider,
        "benchmark",
        "diagnose",
        "01900000-0000-7000-8000-000000000000",
        "--json",
    )

    assert result.code == ExitCode.NOT_FOUND


async def test_settle_reports_a_run_that_belongs_to_no_launch(
    catalog_settings: Settings, provider: FakePaymentProvider, session: AsyncSession
) -> None:
    """The command answers honestly about a run nobody launched from the console.

    What it settles is covered where the settlement lives; what this pins is that the command
    exists, resolves a run, and says plainly when there is no launch behind it rather than
    inventing one.
    """
    await cli(catalog_settings, provider, "benchmark", "seed")
    await cli(catalog_settings, provider, "benchmark", "run")
    finished = await only_run(session)

    result = await cli(
        catalog_settings, provider, "benchmark", "settle", str(finished.id), "--json"
    )

    assert result.code == ExitCode.OK
    payload = result.json()
    assert payload["status"] == BenchmarkRunStatus.COMPLETED.value
    assert payload["launch_id"] is None


async def test_settle_refuses_a_run_that_has_not_finished(
    catalog_settings: Settings, provider: FakePaymentProvider, session: AsyncSession
) -> None:
    """A launch naming a run nobody has closed is not stranded, it is running."""
    await cli(catalog_settings, provider, "benchmark", "seed")
    merchant = await MerchantRepository(session).get_by_slug(VOLTEDGE.merchant_slug)
    assert merchant is not None
    started = await BenchmarkRunService(session).start_run(
        suite_key=VOLTEDGE.suite.key,
        suite_version=VOLTEDGE.suite.version,
        merchant_slug=VOLTEDGE.merchant_slug,
    )

    result = await cli(catalog_settings, provider, "benchmark", "settle", str(started.id))

    assert result.code == ExitCode.REFUSED
    assert "has not finished" in result.out


# Dispatch.


async def test_dispatch_with_nothing_queued_is_an_ordinary_answer(
    catalog_settings: Settings, provider: FakePaymentProvider
) -> None:
    """An empty work list is not a failure, and a script has to be able to tell.

    The settings are pinned rather than inherited: a developer machine with a provider key in
    `.env` and a CI runner without one would otherwise report different executors and this would
    be asserting whatever the environment happened to hold.
    """
    settings = without_providers(catalog_settings)
    await cli(settings, provider, "benchmark", "seed", "--json")

    run = await cli(settings, provider, "benchmark", "dispatch", "--json")

    assert run.code == ExitCode.OK
    assert run.json() == {
        "launch_id": None,
        "status": "NONE_QUEUED",
        # What this worker could have run, said even when there was nothing to run. An operator
        # whose launches are not moving reads this before anything else.
        "executors": "reference-isolated",
    }


async def test_dispatch_says_when_queued_work_needs_a_worker_it_is_not(
    catalog_settings: Settings, provider: FakePaymentProvider, session: AsyncSession
) -> None:
    """Queued work this process cannot run is neither "nothing to do" nor a failed launch.

    The launch stays queued for a worker that holds the provider credential it froze, and this
    process exits REFUSED so an operator loop reading exit codes finds out that its deployment
    has work nobody it knows of is configured to execute.
    """
    await cli(catalog_settings, provider, "benchmark", "seed", "--json")
    merchant = await MerchantRepository(session).get_by_slug(MERCHANT_SLUG)
    assert merchant is not None
    await MerchantRepresentationService(session).publish_source(
        read_source(VOLTEDGE_DIRECTORY / "source.json")
    )
    configured = with_openai(catalog_settings)
    service = MerchantEvaluationLaunchService(session, configured)
    plan = await service.plan(merchant.id)
    launch = await service.request(
        merchant.id,
        purpose=plan.purpose,
        representation_id=plan.representation_id,
        request_key="cli-unserviceable",
        plan_digest=plan.digest,
    )
    launch_id = launch.id

    run = await cli(
        without_providers(catalog_settings), provider, "benchmark", "dispatch", "--json"
    )

    assert run.code == ExitCode.REFUSED
    payload = run.json()
    assert payload["status"] == UNSERVICEABLE
    assert payload["executors"] == "reference-isolated"
    assert payload["launch_id"] == str(launch_id)
    assert payload["failure_code"] is None
    assert "llm-openai" in cast(str, payload["detail"])

    still_queued = await session.get(BenchmarkEvaluationLaunch, launch_id)
    assert still_queued is not None
    await session.refresh(still_queued)
    assert still_queued.status is EvaluationLaunchStatus.QUEUED
    assert still_queued.run_id is None


async def test_queue_says_what_is_waiting_and_whether_this_worker_could_serve_it(
    catalog_settings: Settings, provider: FakePaymentProvider, session: AsyncSession
) -> None:
    """The command an operator runs when a merchant says nothing is happening.

    It reads the launch table and nothing else. There is no monitoring copy of benchmark truth
    to disagree with, so what it reports is what the dispatcher would find.
    """
    await cli(catalog_settings, provider, "benchmark", "seed", "--json")
    merchant = await MerchantRepository(session).get_by_slug(MERCHANT_SLUG)
    assert merchant is not None
    await MerchantRepresentationService(session).publish_source(
        read_source(VOLTEDGE_DIRECTORY / "source.json")
    )
    service = MerchantEvaluationLaunchService(session, with_openai(catalog_settings))
    plan = await service.plan(merchant.id)
    launch = await service.request(
        merchant.id,
        purpose=plan.purpose,
        representation_id=plan.representation_id,
        request_key="cli-queue",
        plan_digest=plan.digest,
    )
    launch_id = launch.id

    run = await cli(without_providers(catalog_settings), provider, "benchmark", "queue", "--json")

    assert run.code == ExitCode.OK
    payload = run.json()
    assert payload["executors"] == "reference-isolated"
    assert payload["queued"] == 1
    assert payload["executing"] == 0
    assert payload["unserviceable"] == 1
    listed = cast(list[dict[str, object]], payload["launches"])
    assert [entry["launch_id"] for entry in listed] == [str(launch_id)]
    assert listed[0]["executor_kind"] == "llm-openai"
    assert listed[0]["serviceable"] is False
    assert listed[0]["merchant_slug"] == MERCHANT_SLUG

    # A worker that holds the frozen provider reads the same queue as serviceable.
    capable = await cli(with_openai(catalog_settings), provider, "benchmark", "queue", "--json")
    capable_listed = cast(list[dict[str, object]], capable.json()["launches"])
    assert capable_listed[0]["serviceable"] is True
    assert capable.json()["unserviceable"] == 0


async def test_queue_reports_an_empty_deployment_without_inventing_work(
    catalog_settings: Settings, provider: FakePaymentProvider
) -> None:
    await cli(catalog_settings, provider, "benchmark", "seed", "--json")

    run = await cli(without_providers(catalog_settings), provider, "benchmark", "queue", "--json")

    assert run.code == ExitCode.OK
    assert run.json()["launches"] == []
    assert run.json()["queued"] == 0
    assert run.json()["unserviceable"] == 0


async def test_cancel_closes_a_queued_launch_nothing_can_run(
    catalog_settings: Settings, provider: FakePaymentProvider, session: AsyncSession
) -> None:
    """The operator remedy for a launch frozen to an executor no worker in this deployment has.

    Without it, capability-aware claiming would trade a recoverable failure for an unrecoverable
    one: the launch holds the merchant's one pending slot and nothing settles it.
    """
    await cli(catalog_settings, provider, "benchmark", "seed", "--json")
    merchant = await MerchantRepository(session).get_by_slug(MERCHANT_SLUG)
    assert merchant is not None
    await MerchantRepresentationService(session).publish_source(
        read_source(VOLTEDGE_DIRECTORY / "source.json")
    )
    service = MerchantEvaluationLaunchService(session, with_openai(catalog_settings))
    plan = await service.plan(merchant.id)
    launch = await service.request(
        merchant.id,
        purpose=plan.purpose,
        representation_id=plan.representation_id,
        request_key="cli-cancel",
        plan_digest=plan.digest,
    )
    launch_id = launch.id

    cancelled = await cli(
        without_providers(catalog_settings), provider, "benchmark", "cancel", str(launch_id)
    )

    assert cancelled.code == ExitCode.OK
    assert "cancelled_by_operator" in cancelled.out

    # The queue is empty afterwards, which is what the operator was trying to achieve.
    queued = await cli(
        without_providers(catalog_settings), provider, "benchmark", "queue", "--json"
    )
    assert queued.json()["queued"] == 0
    assert queued.json()["unserviceable"] == 0


async def test_cancel_refuses_a_launch_that_does_not_exist(
    catalog_settings: Settings, provider: FakePaymentProvider
) -> None:
    await cli(catalog_settings, provider, "benchmark", "seed", "--json")

    missing = await cli(catalog_settings, provider, "benchmark", "cancel", str(uuid.uuid7()))

    assert missing.code == ExitCode.NOT_FOUND


# Provider execution governance, which an operator reads and writes without a database client.


async def test_provider_reports_the_policy_a_deployment_that_configured_nothing_runs_under(
    catalog_settings: Settings, provider: FakePaymentProvider
) -> None:
    """The answer to "what may be spent here", including that nobody has decided yet."""
    result = await cli(catalog_settings, provider, "benchmark", "provider", "--json")

    assert result.code == ExitCode.OK
    providers = cast(list[dict[str, object]], result.json()["providers"])
    assert {row["provider"] for row in providers} == {"openai-responses", "google-gemini"}
    assert all(row["configured"] is False for row in providers)
    assert all(row["enabled"] is True for row in providers)


async def test_provider_output_carries_no_credential_and_no_connection_string(
    catalog_settings: Settings, provider: FakePaymentProvider
) -> None:
    """A command that printed a key to explain a wait would be the worst way to explain one."""
    configured = with_openai(catalog_settings)

    result = await cli(configured, provider, "benchmark", "provider")

    assert result.code == ExitCode.OK
    assert "test-openai-key" not in result.out
    assert "postgres" not in result.out.lower()
    assert configured.postgres_password.get_secret_value() not in result.out


async def test_pausing_a_provider_is_reported_and_survives_being_read_back(
    catalog_settings: Settings, provider: FakePaymentProvider
) -> None:
    paused = await cli(
        catalog_settings, provider, "benchmark", "provider-set", "openai-responses", "--pause"
    )
    assert paused.code == ExitCode.OK
    assert "paused" in paused.out

    status = await cli(catalog_settings, provider, "benchmark", "provider", "--json")
    providers = cast(list[dict[str, object]], status.json()["providers"])
    openai = next(row for row in providers if row["provider"] == "openai-responses")
    assert openai["enabled"] is False
    assert openai["wait_reason"] == "PROVIDER_PAUSED"


async def test_resuming_a_paused_provider_admits_work_again(
    catalog_settings: Settings, provider: FakePaymentProvider
) -> None:
    await cli(
        catalog_settings, provider, "benchmark", "provider-set", "openai-responses", "--pause"
    )

    resumed = await cli(
        catalog_settings,
        provider,
        "benchmark",
        "provider-set",
        "openai-responses",
        "--resume",
        "--json",
    )

    assert resumed.json()["enabled"] is True
    assert resumed.json()["policy_version"] == 3


async def test_every_policy_write_moves_the_version_a_launch_would_freeze(
    catalog_settings: Settings, provider: FakePaymentProvider
) -> None:
    first = await cli(
        catalog_settings,
        provider,
        "benchmark",
        "provider-set",
        "openai-responses",
        "--max-concurrent-launches",
        "2",
        "--json",
    )
    second = await cli(
        catalog_settings,
        provider,
        "benchmark",
        "provider-set",
        "openai-responses",
        "--window-requests",
        "500",
        "--json",
    )

    assert first.json()["policy_version"] == 2
    assert second.json()["policy_version"] == 3
    assert second.json()["max_concurrent_launches"] == 2
    assert second.json()["max_requests_per_window"] == 500


async def test_a_deployment_ceiling_can_be_removed_without_pausing_the_provider(
    catalog_settings: Settings, provider: FakePaymentProvider
) -> None:
    await cli(
        catalog_settings,
        provider,
        "benchmark",
        "provider-set",
        "google-gemini",
        "--window-requests",
        "100",
    )

    cleared = await cli(
        catalog_settings,
        provider,
        "benchmark",
        "provider-set",
        "google-gemini",
        "--no-window-cap",
        "--json",
    )

    assert cleared.json()["max_requests_per_window"] is None
    assert cleared.json()["enabled"] is True


async def test_the_queue_says_a_launch_is_waiting_on_a_paused_provider(
    catalog_settings: Settings, provider: FakePaymentProvider, session: AsyncSession
) -> None:
    """The operator's answer when a merchant says nothing is happening and a worker is capable.

    Two different waits, told apart: a launch nobody is configured to run names the worker it is
    waiting for, and a launch nobody is currently paying for names the policy that stopped it.
    """
    configured = with_openai(catalog_settings)
    await _queue_one(session, configured, provider)
    await cli(
        catalog_settings, provider, "benchmark", "provider-set", "openai-responses", "--pause"
    )

    result = await cli(configured, provider, "benchmark", "queue", "--json")

    launches = cast(list[dict[str, object]], result.json()["launches"])
    assert [row["wait_reason"] for row in launches] == ["PROVIDER_PAUSED"]


async def test_the_queue_says_a_launch_is_waiting_on_a_worker_nobody_configured(
    catalog_settings: Settings, provider: FakePaymentProvider, session: AsyncSession
) -> None:
    configured = with_openai(catalog_settings)
    await _queue_one(session, configured, provider)

    result = await cli(
        without_providers(catalog_settings), provider, "benchmark", "queue", "--json"
    )

    launches = cast(list[dict[str, object]], result.json()["launches"])
    assert [row["wait_reason"] for row in launches] == ["NO_CAPABLE_WORKER"]


async def test_usage_reports_an_allowance_that_has_been_spent_on_nothing_yet(
    catalog_settings: Settings, provider: FakePaymentProvider, session: AsyncSession
) -> None:
    """A queued launch has spent nothing, and the allowance it was admitted with is the whole
    of what it may spend."""
    configured = with_openai(catalog_settings)
    launch_id = await _queue_one(session, configured, provider)

    result = await cli(configured, provider, "benchmark", "usage", str(launch_id), "--json")

    payload = result.json()
    assert result.code == ExitCode.OK
    assert payload["requests_charged"] == 0
    assert payload["requests_remaining"] == payload["max_provider_requests"]
    assert payload["provider"] == "openai-responses"


async def test_usage_says_unknown_rather_than_zero_where_a_provider_reported_no_tokens(
    catalog_settings: Settings, provider: FakePaymentProvider, session: AsyncSession
) -> None:
    """No provider has answered, so no token count exists, and none is invented."""
    configured = with_openai(catalog_settings)
    launch_id = await _queue_one(session, configured, provider)

    result = await cli(configured, provider, "benchmark", "usage", str(launch_id))

    assert "tokens      input unknown  output unknown  total unknown" in result.out
    assert "0 tokens" not in result.out


async def _queue_one(
    session: AsyncSession, settings: Settings, provider: FakePaymentProvider
) -> uuid.UUID:
    """One queued evaluation for the seeded world, through the service the console calls."""
    await cli(settings, provider, "benchmark", "seed", "--json")
    merchant = await MerchantRepository(session).get_by_slug(MERCHANT_SLUG)
    assert merchant is not None
    await MerchantRepresentationService(session).publish_source(
        read_source(VOLTEDGE_DIRECTORY / "source.json")
    )
    service = MerchantEvaluationLaunchService(session, settings)
    plan = await service.plan(merchant.id)
    launch = await service.request(
        merchant.id,
        purpose=plan.purpose,
        representation_id=plan.representation_id,
        request_key="operator-queue-key",
        plan_digest=plan.digest,
    )
    return launch.id
