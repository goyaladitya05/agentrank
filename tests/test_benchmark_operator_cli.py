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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.auth.models import MerchantApiCredential
from agentrank_api.benchmark.endpoint import CREDENTIAL_LABEL
from agentrank_api.benchmark.lifecycle import BenchmarkRunStatus, MissionRunStatus
from agentrank_api.benchmark.models import BenchmarkRun
from agentrank_api.benchmark.runner import BenchmarkRunService
from agentrank_api.cli import ExitCode, main
from agentrank_api.cli.benchmark import DISCLAIMER
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.config import Settings
from agentrank_api.payments.fake import FakeOutcome, FakePaymentProvider

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

    Every benchmark command is given the authored world by absolute path unless the test names
    one itself. The default is relative to the working directory, which is the repository root
    for an operator and is whatever pytest was started from here, and a suite of tests that
    depended on that would be a suite that passes for a reason nobody chose. That the default
    resolves is asserted separately, once.
    """
    named = list(arguments)
    if named[:1] == ["benchmark"] and "--world" not in named:
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
