"""The diagnostics read service against real persisted evidence.

These tests drive real runs through the real runner, the real payment kernel and the real
trace repository, then assert what the read layer says about them. A diagnosis assembled from
rows a test wrote by hand would prove nothing about assembly, which is the part most likely
to silently misattribute.
"""

import uuid
from pathlib import Path

import pytest
from benchmark_support import VOLTEDGE, fixture, mission, suite
from commerce_support import PRICE, Shop, build_shop
from executor_support import Buy, scripted
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentrank_api.benchmark.agent_trace import AgentExecutionEvidence
from agentrank_api.benchmark.buyer import MerchantBuyerSurface
from agentrank_api.benchmark.definitions import ExpectedOutcome
from agentrank_api.benchmark.environment import BenchmarkEnvironmentService
from agentrank_api.benchmark.experiment import CompilerImpactExperimentService
from agentrank_api.benchmark.faults import ExecutionFault, FaultOrigin
from agentrank_api.benchmark.llm import GEMINI_PROVIDER, AgentConfiguration
from agentrank_api.benchmark.models import BenchmarkRun
from agentrank_api.benchmark.reference_executor import ReferenceMissionExecutor
from agentrank_api.benchmark.report import ExecutorReport
from agentrank_api.benchmark.repository import AgentEvidenceRepository, BenchmarkSuiteRepository
from agentrank_api.benchmark.runner import BenchmarkRunService
from agentrank_api.benchmark.suites import BenchmarkSuiteService
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.compiler.service import MerchantCompilerService
from agentrank_api.diagnostics.codes import DiagnosticCode, DiagnosticOwner
from agentrank_api.diagnostics.service import DiagnosticsService
from agentrank_api.errors import NotFoundError
from agentrank_api.payments.fake import FakePaymentProvider
from agentrank_api.representation.fixtures import read_source
from agentrank_api.representation.service import MerchantRepresentationService

pytestmark = pytest.mark.anyio

SLUG = "test-merchant"
WORLD = fixture()


async def registered_shop(session: AsyncSession) -> uuid.UUID:
    built = await build_shop(session, SLUG)
    await BenchmarkEnvironmentService(session).register(WORLD)
    return built.merchant_id


async def publish(session: AsyncSession, *missions: object) -> None:
    await BenchmarkSuiteService(session).publish(
        suite(*missions, merchant_slug=SLUG)  # type: ignore[arg-type]
    )


async def reference_run(
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    merchant_id: uuid.UUID,
) -> BenchmarkRun:
    """A completed run by the deterministic reference executor over the real kernel."""
    surface = MerchantBuyerSurface(factory, merchant_id=merchant_id, provider=FakePaymentProvider())
    return await BenchmarkRunService(session).run_suite(
        ReferenceMissionExecutor(surface),
        suite_key="test-suite",
        suite_version=1,
        fixture=WORLD,
    )


async def append_evidence(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    mission_run_id: uuid.UUID,
    merchant_id: uuid.UUID,
    events: list[tuple[str, dict[str, object]]],
) -> None:
    evidence = AgentExecutionEvidence()
    for event_type, payload in events:
        evidence.add(event_type, payload)
    await AgentEvidenceRepository(session).append(
        evidence,
        mission_run_id=mission_run_id,
        run_id=run_id,
        merchant_id=merchant_id,
    )
    await session.commit()


class TestRunDiagnosis:
    async def test_a_clean_reference_run_has_no_findings(
        self, session: AsyncSession, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        merchant_id = await registered_shop(session)
        await publish(
            session,
            mission("buy-one"),
            mission("buy-two"),
            mission(
                "skip-cheap", outcome=ExpectedOutcome.NO_ACCEPTABLE_PURCHASE, budget_minor=1000
            ),
        )
        finished = await reference_run(session, factory, merchant_id)

        diagnosis = await DiagnosticsService(session).run_diagnostics(
            finished.id, merchant_id=merchant_id
        )

        assert diagnosis.status == "COMPLETED"
        assert diagnosis.findings == ()
        assert diagnosis.catalog_pin_verified is True
        statuses = {mission.status.value for mission in diagnosis.missions}
        assert statuses <= {"SUCCEEDED", "ABSTAINED"}
        captured = [
            effect
            for entry in diagnosis.missions
            for effect in entry.simulated_demand
            if effect.bucket == "CAPTURED"
        ]
        assert captured and all(effect.amount_minor > 0 for effect in captured)

    async def test_an_unsafe_purchase_diagnoses_as_an_escape(
        self, session: AsyncSession, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        built: Shop = await build_shop(session, SLUG)
        await BenchmarkEnvironmentService(session).register(WORLD)
        await publish(
            session,
            mission(
                "control-underpriced",
                outcome=ExpectedOutcome.NO_ACCEPTABLE_PURCHASE,
                budget_minor=100000,
            ),
        )
        buyer, ledger = scripted(
            factory,
            built.merchant_id,
            {"control-underpriced": Buy(built.variant_id, mandate_amount_minor=PRICE)},
            provider=FakePaymentProvider(),
        )
        finished = await BenchmarkRunService(session).run_suite(
            buyer,
            suite_key="test-suite",
            suite_version=1,
            fixture=WORLD,
            witness=ledger,
        )

        diagnosis = await DiagnosticsService(session).run_diagnostics(
            finished.id, merchant_id=built.merchant_id
        )

        codes = [finding.code for finding in diagnosis.findings]
        assert codes, "a purchase past ground truth must produce findings"
        assert codes[0] is DiagnosticCode.SAFETY_ESCAPE
        assert diagnosis.findings[0].owner is DiagnosticOwner.COMMERCE_RUNTIME
        assert diagnosis.findings[0].severity.value == "CRITICAL"
        # The breach itself stays visible beside the escape.
        assert DiagnosticCode.SELECTION_VIOLATED_REQUIREMENTS in codes

    async def test_provider_outage_traces_override_the_agent_error_label(
        self, session: AsyncSession
    ) -> None:
        built = await build_shop(session, SLUG)
        await publish(session, mission("buy-one"))
        service = BenchmarkRunService(session)
        started = await service.start_run(
            suite_key="test-suite", suite_version=1, merchant_slug=SLUG
        )
        mission_run_id = await service.start_mission(
            started.id, "buy-one", merchant_id=built.merchant_id
        )
        result = await service.record_result(
            started.id,
            "buy-one",
            ExecutorReport(built.merchant_id),
            merchant_id=built.merchant_id,
            fault=ExecutionFault(origin=FaultOrigin.AGENT, detail="LLM provider failed"),
        )
        await service.complete_run(started.id, merchant_id=built.merchant_id)
        await append_evidence(
            session,
            run_id=started.id,
            mission_run_id=mission_run_id,
            merchant_id=built.merchant_id,
            events=[
                ("MODEL_REQUEST", {"invocation_sequence": 1}),
                ("PROVIDER_ERROR", {"kind": "TimeoutError", "detail": "provider timed out"}),
                ("AGENT_ABORT", {"reason": "provider_unavailable", "turn": 1}),
            ],
        )

        diagnosis = await DiagnosticsService(session).run_diagnostics(
            started.id, merchant_id=built.merchant_id
        )

        entry = next(m for m in diagnosis.missions if m.mission_run_id == result.id)
        assert entry.primary is not None
        assert entry.primary.code is DiagnosticCode.PROVIDER_OUTAGE_TERMINATED_MISSION
        assert entry.primary.owner is DiagnosticOwner.MODEL_PROVIDER
        assert diagnosis.provider_health.terminated_outages == 1
        assert diagnosis.provider_health.missions_with_provider_errors == 1

    async def test_recovered_throttles_stay_secondary_operational_history(
        self, session: AsyncSession, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        merchant_id = await registered_shop(session)
        await publish(session, mission("buy-one"))
        finished = await reference_run(session, factory, merchant_id)
        target = finished.mission_runs[0]

        await append_evidence(
            session,
            run_id=finished.id,
            mission_run_id=target.id,
            merchant_id=merchant_id,
            events=[
                ("MODEL_REQUEST", {"invocation_sequence": 1}),
                (
                    "PROVIDER_ERROR",
                    {"kind": "ProviderThrottledError", "detail": "rate limited", "attempt": 1},
                ),
                ("MODEL_RESPONSE", {"invocation_sequence": 1}),
                ("TOOL_CALL", {"name": "search_products"}),
            ],
        )

        diagnosis = await DiagnosticsService(session).run_diagnostics(
            finished.id, merchant_id=merchant_id
        )
        throttled = next(m for m in diagnosis.missions if m.mission_run_id == target.id)
        recovered = [
            f for f in throttled.findings if f.code is DiagnosticCode.PROVIDER_THROTTLE_RECOVERED
        ]
        assert len(recovered) == 1
        # A secondary observation never leads a successful mission's diagnosis.
        assert throttled.status.value == "SUCCEEDED"
        assert throttled.primary is None
        assert diagnosis.provider_health.recovered_throttles == 1
        assert diagnosis.provider_health.terminated_outages == 0


class TestTraceProjection:
    async def test_trace_pages_are_bounded_ordered_and_counted(
        self, session: AsyncSession, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        merchant_id = await registered_shop(session)
        await publish(session, mission("buy-one"))
        finished = await reference_run(session, factory, merchant_id)
        target = finished.mission_runs[0]
        await append_evidence(
            session,
            run_id=finished.id,
            mission_run_id=target.id,
            merchant_id=merchant_id,
            events=[
                ("MODEL_REQUEST", {"invocation_sequence": 1}),
                ("MODEL_RESPONSE", {"invocation_sequence": 1}),
                ("TOOL_CALL", {"name": "search_products"}),
            ],
        )
        service = DiagnosticsService(session)

        page = await service.mission_trace(finished.id, target.id, merchant_id=merchant_id, limit=2)
        assert page.total_events == 3
        assert [event.sequence for event in page.events] == [1, 2]

        second = await service.mission_trace(
            finished.id, target.id, merchant_id=merchant_id, limit=2, offset=2
        )
        assert [event.sequence for event in second.events] == [3]

    async def test_a_foreign_mission_is_not_projected(
        self, session: AsyncSession, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        merchant_id = await registered_shop(session)
        await publish(session, mission("buy-one"))
        finished = await reference_run(session, factory, merchant_id)

        with pytest.raises(NotFoundError):
            await DiagnosticsService(session).mission_trace(
                finished.id, uuid.uuid7(), merchant_id=merchant_id
            )


class TestIsolation:
    async def test_another_merchants_run_is_not_found(self, session: AsyncSession) -> None:
        mine = await registered_shop(session)
        await build_shop(session, "other-shop")
        await BenchmarkEnvironmentService(session).register(
            fixture(merchant_slug="other-shop", key="other-catalog")
        )
        await BenchmarkSuiteService(session).publish(
            suite(mission("buy-one"), merchant_slug="other-shop", key="other-suite")
        )
        started = await BenchmarkRunService(session).start_run(
            suite_key="other-suite", suite_version=1, merchant_slug="other-shop"
        )

        with pytest.raises(NotFoundError):
            await DiagnosticsService(session).run_diagnostics(started.id, merchant_id=mine)

    async def test_an_unknown_experiment_is_not_found(self, session: AsyncSession) -> None:
        mine = await registered_shop(session)

        with pytest.raises(NotFoundError):
            await DiagnosticsService(session).experiment_diagnosis(uuid.uuid7(), merchant_id=mine)


class TestOverview:
    async def test_overview_summarizes_runs_and_demand(
        self, session: AsyncSession, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        merchant_id = await registered_shop(session)
        await publish(session, mission("buy-one"), mission("buy-two"))
        await reference_run(session, factory, merchant_id)

        overview = await DiagnosticsService(session).merchant_overview(merchant_id)

        assert len(overview.runs) == 1
        summary = overview.runs[0]
        assert summary.status == "COMPLETED"
        assert summary.missions_succeeded == 2
        assert summary.task_completion_rate == 1.0
        assert summary.provider_failure_missions == 0
        totals = {
            entry["currency"]: entry for entry in overview.simulated_demand_totals_by_currency
        }
        assert totals["INR"]["simulated_potential_demand_amount_minor"] == 999800
        assert totals["INR"]["simulated_captured_demand_amount_minor"] == 999800
        assert overview.top_findings == ()
        assert overview.representation_state.source_snapshot_label is None

    async def test_overview_carries_the_latest_experiment_conclusion(
        self, session: AsyncSession
    ) -> None:
        source_definition = read_source(Path("benchmarks/voltedge/source.json"))
        merchant = await MerchantRepository(session).create(slug="voltedge", name="VoltEdge")
        await session.commit()
        representations = MerchantRepresentationService(session)
        source = await representations.publish_source(source_definition)
        compiler = MerchantCompilerService(session)
        compiler_run = await compiler.run(merchant.id, source.id)
        compiled = await compiler.publish(merchant.id, compiler_run.id)
        stored_suite = await BenchmarkSuiteRepository(session).create(
            suite(merchant_slug="voltedge")
        )
        await session.commit()
        environment = await BenchmarkEnvironmentService(session).register(VOLTEDGE.fixture)

        config = AgentConfiguration(provider=GEMINI_PROVIDER, requested_model="test-model")
        experiment = await CompilerImpactExperimentService(session).create(
            merchant_id=merchant.id,
            suite_id=stored_suite.id,
            environment=environment,
            source_snapshot_id=source.id,
            compiled_representation_id=compiled.id,
            buyer_configuration=config.payload(),
            buyer_configuration_digest=config.configuration_digest,
            sample_count=1,
            development_benchmark=False,
        )

        overview = await DiagnosticsService(session).merchant_overview(merchant.id)
        assert overview.latest_experiment is not None
        assert overview.latest_experiment.experiment_id == experiment.id
        assert overview.latest_experiment.benchmark_designation == "EVALUATION"
        assert overview.latest_experiment.completed_sample_pairs == 0
        assert overview.latest_experiment.conclusion_kind == "INCOMPLETE"
