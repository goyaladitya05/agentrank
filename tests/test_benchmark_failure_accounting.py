"""Agent failure, infrastructure failure, and the denominators they cannot game.

The distinction has to survive the evaluator and the metrics together. A unit test that only
checks `FaultOrigin` could leave an agent crash as ERRORED, which is exactly the flattering route
this feature closes: ERRORED carries no failure reason and simulated demand classifies it as not
measured.
"""

import uuid

import pytest
from benchmark_support import BLACK, fixture, mission, suite
from commerce_support import PRICE, build_shop
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.benchmark.definitions import ExpectedOutcome
from agentrank_api.benchmark.environment import BenchmarkEnvironmentService
from agentrank_api.benchmark.execution import ExecutorIdentity
from agentrank_api.benchmark.failures import FailureReason
from agentrank_api.benchmark.faults import ExecutionFault, FaultOrigin
from agentrank_api.benchmark.lifecycle import MissionRunStatus
from agentrank_api.benchmark.report import ExecutorReport
from agentrank_api.benchmark.runner import BenchmarkRunService
from agentrank_api.benchmark.suites import BenchmarkSuiteService
from agentrank_api.benchmark.tools import ToolLedger

pytestmark = pytest.mark.anyio

SLUG = "test-merchant"
WORLD = fixture()


async def _published(session: AsyncSession) -> None:
    await BenchmarkSuiteService(session).publish(
        suite(
            mission("one", budget_minor=PRICE, constraints=(BLACK,)),
            mission("two", budget_minor=PRICE, constraints=(BLACK,)),
            merchant_slug=SLUG,
        )
    )


async def test_an_agent_failure_is_failed_lost_demand_and_stays_in_the_denominator(
    session: AsyncSession,
) -> None:
    """The exact crash-to-zero-conversion attack, asserted against persisted result and metrics."""
    built = await build_shop(session, SLUG)
    await _published(session)
    service = BenchmarkRunService(session)
    run = await service.start_run(suite_key="test-suite", suite_version=1, merchant_slug=SLUG)

    result = await service.record_result(
        run.id,
        "one",
        ExecutorReport(merchant_id=built.merchant_id),
        merchant_id=built.merchant_id,
        fault=ExecutionFault(origin=FaultOrigin.AGENT, detail="the buyer process timed out"),
    )
    metrics = await service.metrics(run.id, merchant_id=built.merchant_id)
    demand = metrics.simulated_demand.single_currency()

    assert result.status is MissionRunStatus.FAILED
    assert result.primary_failure_reason is FailureReason.AGENT_EXECUTION_ERROR
    # No selection is a discovery failure too, but the trusted fault is primary. A report reader
    # can tell "the buyer never found anything" from "the buyer process failed" without either
    # changing the demand accounting.
    assert FailureReason.AGENT_EXECUTION_ERROR in result.failure_reasons
    # Two available missions fixed the denominator. The buyer failed one and did not start the
    # other, so it cannot make its completion rate look like one out of one.
    assert metrics.purchase_missions == 2
    assert metrics.task_completion_rate == pytest.approx(0.0)
    assert metrics.missions_failed == 1
    assert metrics.missions_errored == 0
    assert demand.lost_amount_minor == PRICE
    assert demand.not_measured_amount_minor == PRICE


async def test_a_harness_failure_is_not_measured_and_is_never_relabelled_as_agent_failure(
    session: AsyncSession,
) -> None:
    """The opposite direction: an infrastructure failure does not make a merchant lose demand."""
    built = await build_shop(session, SLUG)
    await _published(session)
    service = BenchmarkRunService(session)
    run = await service.start_run(suite_key="test-suite", suite_version=1, merchant_slug=SLUG)

    result = await service.record_result(
        run.id,
        "one",
        ExecutorReport(merchant_id=built.merchant_id),
        merchant_id=built.merchant_id,
        fault=ExecutionFault(origin=FaultOrigin.HARNESS, detail="the runner lost its database"),
    )
    metrics = await service.metrics(run.id, merchant_id=built.merchant_id)
    demand = metrics.simulated_demand.single_currency()

    assert result.status is MissionRunStatus.ERRORED
    assert result.failure_reasons == ()
    assert metrics.purchase_missions == 2
    assert metrics.task_completion_rate == pytest.approx(0.0)
    assert metrics.missions_failed == 0
    assert metrics.missions_errored == 1
    assert demand.lost_amount_minor == 0
    assert demand.not_measured_amount_minor == PRICE * 2


async def test_an_executor_exception_crosses_the_runner_as_an_agent_failure(
    session: AsyncSession,
) -> None:
    """An untrusted runner seam cannot turn a raised model error into unfinished demand."""
    built = await build_shop(session, SLUG)
    await _published(session)
    await BenchmarkEnvironmentService(session).register(WORLD)

    class CrashingExecutor:
        identity = ExecutorIdentity(kind="crashing", version=1)

        async def __call__(self, *args: object, **kwargs: object) -> ExecutorReport:
            del args, kwargs
            raise RuntimeError("the configured buyer stopped")

    run = await BenchmarkRunService(session).run_suite(
        CrashingExecutor(),
        suite_key="test-suite",
        suite_version=1,
        fixture=WORLD,
        witness=ToolLedger(),
    )
    metrics = await BenchmarkRunService(session).metrics(run.id, merchant_id=built.merchant_id)
    demand = metrics.simulated_demand.single_currency()

    assert run.status.value == "COMPLETED"
    assert metrics.purchase_missions == 2
    assert metrics.missions_failed == 2
    assert metrics.missions_errored == 0
    assert metrics.primary_failure_counts == {FailureReason.AGENT_EXECUTION_ERROR: 2}
    assert demand.lost_amount_minor == PRICE * 2
    assert demand.not_measured_amount_minor == 0


async def test_a_crash_on_a_control_mission_is_not_a_correct_abstention(
    session: AsyncSession,
) -> None:
    """A buyer cannot turn a control mission into a flattering abstention by failing on it."""
    await build_shop(session, SLUG)
    await BenchmarkSuiteService(session).publish(
        suite(
            mission("buy", budget_minor=PRICE, constraints=(BLACK,)),
            mission(
                "control",
                budget_minor=PRICE,
                constraints=(BLACK,),
                outcome=ExpectedOutcome.NO_ACCEPTABLE_PURCHASE,
            ),
            merchant_slug=SLUG,
        )
    )
    await BenchmarkEnvironmentService(session).register(WORLD)

    class CrashingExecutor:
        identity = ExecutorIdentity(kind="crashing-control", version=1)

        async def __call__(self, *args: object, **kwargs: object) -> ExecutorReport:
            del args, kwargs
            raise RuntimeError("the buyer stopped on a control")

    run = await BenchmarkRunService(session).run_suite(
        CrashingExecutor(),
        suite_key="test-suite",
        suite_version=1,
        fixture=WORLD,
        witness=ToolLedger(),
    )
    metrics = await BenchmarkRunService(session).metrics(run.id, merchant_id=run.merchant_id)

    assert metrics.correct_abstentions == 0
    assert metrics.missions_abstained == 0
    assert metrics.missions_failed == 2


def test_agent_failure_is_not_an_executor_chosen_label() -> None:
    """Only trusted origins select the reason, and a report has no origin to supply."""
    report = ExecutorReport(merchant_id=uuid.uuid7())

    assert not hasattr(report, "origin")
