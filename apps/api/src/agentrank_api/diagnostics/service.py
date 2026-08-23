"""The read side of the diagnostics layer: trusted rows in, diagnoses out.

Everything in the pure modules below this one works on flattened facts, so something has to
walk the persisted rows and build those facts honestly. That is this service, and its rules
are the ones the rest of the repository already lives by.

Merchant scope is structural. Every read takes the merchant and puts it in the query; there
is no unscoped load anywhere in the call graph. A run identifier, a mission identifier or an
experiment identifier that belongs to somebody else is indistinguishable from one that never
existed.

The catalog pin is verified before today's shelf is used as evidence about yesterday's
mission. Attribute specifics are only attached to a diagnosis when the run's recorded catalog
hash still matches the merchant's current rows; otherwise the diagnosis keeps its findings at
reason level rather than quoting attributes from a catalog that has moved on.

Queries are batched. One run's diagnosis costs a fixed number of statements regardless of how
many missions or trace events it covers, because the evidence is fetched per run and grouped
in memory. The overview reads a bounded window of runs and aggregates in single grouped
queries rather than one query per run.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from agentrank_api.benchmark.catalog import catalog_content_hash
from agentrank_api.benchmark.experiment import (
    CompilerImpactExperiment,
    CompilerImpactSample,
    RepresentationKind,
)
from agentrank_api.benchmark.lifecycle import MissionRunStatus
from agentrank_api.benchmark.metrics import BenchmarkMetrics, compute_metrics
from agentrank_api.benchmark.models import (
    AgentProviderUsage,
    AgentTraceEvent,
    AgentUsageKind,
    BenchmarkMissionRun,
    BenchmarkRun,
)
from agentrank_api.benchmark.runner import BenchmarkRunService, outcomes_of
from agentrank_api.commerce.models import Product, Variant
from agentrank_api.diagnostics.codes import engine_identity
from agentrank_api.diagnostics.experiment import (
    ARM_COMPILED,
    ARM_RAW,
    ExperimentFacts,
    ExperimentSampleFacts,
    MissionOutcomeFacts,
    diagnose_experiment,
)
from agentrank_api.diagnostics.findings import MerchantFinding, aggregate_findings
from agentrank_api.diagnostics.mission import (
    MissionDiagnosis,
    MissionDiagnosisInput,
    RequiredAttributeFact,
    SelectionFacts,
    diagnose_mission,
)
from agentrank_api.diagnostics.traces import (
    ProviderUsageRecord,
    TraceEventRecord,
    TraceFacts,
    trace_facts,
)
from agentrank_api.errors import NotFoundError
from agentrank_api.mandates.intent import AllowedCategory, RequiredAttribute
from agentrank_api.representation.definitions import FactConfidence, RepresentationProducer
from agentrank_api.representation.models import CommerceRepresentation, MerchantSourceSnapshot

RUN_RESOURCE = "benchmark_run"
MISSION_RESOURCE = "benchmark_mission_run"
EXPERIMENT_RESOURCE = "compiler_impact_experiment"

DEFAULT_OVERVIEW_RUNS = 10
MAX_OVERVIEW_RUNS = 50

# A trace projection is a bounded page, not an export.
DEFAULT_TRACE_LIMIT = 100
MAX_TRACE_LIMIT = 500


@dataclass(frozen=True, slots=True)
class _VariantRow:
    """The identity of one selected variant, with readable facts when the pin allows."""

    variant_id: uuid.UUID
    sku: str
    product_id: uuid.UUID
    category: str | None
    attributes: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class RunProviderHealth:
    """What one run's traces say about provider availability."""

    missions_with_provider_errors: int
    terminated_outages: int
    recovered_throttles: int
    requested_model: str | None
    resolved_models: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RunDiagnostics:
    """One benchmark run with its complete deterministic reading attached."""

    engine_identity: str
    run_id: uuid.UUID
    merchant_id: uuid.UUID
    status: str
    suite_label: str
    environment_label: str | None
    representation_id: uuid.UUID | None
    representation_label: str | None
    catalog_hash: str | None
    evaluator_version: str | None
    executor_label: str | None
    executor_revision: str | None
    agent_implementation_version: int | None
    benchmark_designation: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    metrics: BenchmarkMetrics
    findings: tuple[MerchantFinding, ...]
    missions: tuple[MissionDiagnosis, ...]
    provider_health: RunProviderHealth
    # None when the run carries no catalog pin to verify; otherwise whether the merchant's
    # current rows still hash to what the run was measured against.
    catalog_pin_verified: bool | None


@dataclass(frozen=True, slots=True)
class TraceEventView:
    """One projected trace event: ordered, typed and already redacted at capture time."""

    sequence: int
    event_type: str
    recorded_at: datetime
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class TraceProjection:
    """A bounded page of one mission's trace, oldest first."""

    total_events: int
    events: tuple[TraceEventView, ...]


@dataclass(frozen=True, slots=True)
class OverviewRunSummary:
    """One recent run's headline, enough for a list without opening it."""

    run_id: uuid.UUID
    status: str
    suite_label: str
    executor_label: str | None
    started_at: datetime | None
    completed_at: datetime | None
    missions_total: int
    missions_succeeded: int
    missions_failed: int
    missions_abstained: int
    missions_errored: int
    task_completion_rate: float | None
    correct_abstention_rate: float | None
    unsafe_attempts: int
    unsafe_completions: int
    provider_failure_missions: int
    simulated_demand_by_currency: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class OverviewExperimentSummary:
    """The most recent controlled experiment's identity and conclusion, if one exists."""

    experiment_id: uuid.UUID
    benchmark_designation: str
    completed_sample_pairs: int
    conclusion_kind: str
    conclusion_statement: str


@dataclass(frozen=True, slots=True)
class RepresentationState:
    """The merchant's source and compiled representation identities, as published."""

    source_snapshot_id: uuid.UUID | None
    source_snapshot_label: str | None
    compiled_representation_id: uuid.UUID | None
    compiled_representation_label: str | None
    review_required_facts: int


@dataclass(frozen=True, slots=True)
class MerchantOverview:
    """The narrow product-oriented read model behind a merchant dashboard."""

    engine_identity: str
    merchant_id: uuid.UUID
    runs: tuple[OverviewRunSummary, ...]
    top_findings: tuple[MerchantFinding, ...]
    top_findings_run_id: uuid.UUID | None
    simulated_demand_totals_by_currency: tuple[dict[str, Any], ...]
    latest_experiment: OverviewExperimentSummary | None
    representation_state: RepresentationState


@dataclass(frozen=True, slots=True)
class ExperimentDiagnosisResult:
    """One experiment's labels beside its deterministic reading."""

    experiment_id: uuid.UUID
    benchmark_designation: str
    pair_order: str
    buyer_configuration_digest: str
    diagnosis: Any


class DiagnosticsService:
    """Read-only assembly of diagnostics from persisted evidence."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._runs = BenchmarkRunService(session)

    async def run_diagnostics(self, run_id: uuid.UUID, *, merchant_id: uuid.UUID) -> RunDiagnostics:
        run = await self._runs.load(run_id, merchant_id=merchant_id)
        suite_label = await self._runs.suite_label(run)
        environment_label = await self._runs.environment_label(run)
        designation = await self._designation(run)

        events = await self._trace_events(run.id, merchant_id=merchant_id)
        usages = await self._provider_usage(run.id, merchant_id=merchant_id)
        variants = await self._selected_variants(run, merchant_id=merchant_id)
        pin_verified = await self._catalog_pin_verified(run, merchant_id=merchant_id)

        diagnoses: list[MissionDiagnosis] = []
        outages = 0
        throttles = 0
        provider_error_missions = 0
        for result in run.mission_runs:
            mission_events = [
                TraceEventRecord(str(event.id), event.event_type.value, event.payload)
                for event in events.get(result.id, [])
            ]
            mission_usages = [
                ProviderUsageRecord(str(usage.id), usage.requested_model, usage.actual_model)
                for usage in usages.get(result.id, [])
            ]
            facts = trace_facts(mission_events, mission_usages)
            if any(record.event_type == "PROVIDER_ERROR" for record in mission_events):
                provider_error_missions += 1
            if facts.provider_faults is not None:
                if facts.provider_faults.outage_terminated_mission:
                    outages += 1
                else:
                    throttles += facts.provider_faults.throttles_recovered
            diagnoses.append(self._diagnose_mission(result, variants, facts, pin_verified))

        resolved_models = sorted(
            {
                model
                for mission_usages in usages.values()
                for usage in mission_usages
                for model in [usage.actual_model]
                if model
            }
        )
        requested_models = sorted(
            {
                model
                for mission_usages in usages.values()
                for usage in mission_usages
                for model in [usage.requested_model]
                if model
            }
        )
        health = RunProviderHealth(
            missions_with_provider_errors=provider_error_missions,
            terminated_outages=outages,
            recovered_throttles=throttles,
            requested_model=requested_models[0] if len(requested_models) == 1 else None,
            resolved_models=tuple(resolved_models),
        )
        return RunDiagnostics(
            engine_identity=engine_identity(),
            run_id=run.id,
            merchant_id=run.merchant_id,
            status=run.status.value,
            suite_label=suite_label,
            environment_label=environment_label,
            representation_id=run.representation_id,
            representation_label=run.representation_label,
            catalog_hash=run.catalog_hash,
            evaluator_version=run.evaluator_version,
            executor_label=run.executor_label,
            executor_revision=run.executor_revision,
            agent_implementation_version=_implementation_version(run),
            benchmark_designation=designation,
            created_at=run.created_at,
            started_at=run.started_at,
            completed_at=run.completed_at,
            metrics=compute_metrics(outcomes_of(run.mission_runs)),
            findings=aggregate_findings(diagnoses),
            missions=tuple(diagnoses),
            provider_health=health,
            catalog_pin_verified=pin_verified,
        )

    async def mission_trace(
        self,
        run_id: uuid.UUID,
        mission_run_id: uuid.UUID,
        *,
        merchant_id: uuid.UUID,
        limit: int = DEFAULT_TRACE_LIMIT,
        offset: int = 0,
    ) -> TraceProjection:
        """A bounded ordered page of one mission's trace events."""
        await self._runs.load(run_id, merchant_id=merchant_id)
        bound = (
            await self._session.execute(
                select(BenchmarkMissionRun).where(
                    BenchmarkMissionRun.id == mission_run_id,
                    BenchmarkMissionRun.run_id == run_id,
                    BenchmarkMissionRun.merchant_id == merchant_id,
                )
            )
        ).scalar_one_or_none()
        if bound is None:
            raise NotFoundError(MISSION_RESOURCE, str(mission_run_id))
        total = (
            await self._session.execute(
                select(func.count())
                .select_from(AgentTraceEvent)
                .where(
                    AgentTraceEvent.mission_run_id == mission_run_id,
                    AgentTraceEvent.merchant_id == merchant_id,
                )
            )
        ).scalar_one()
        clamped_limit = min(max(limit, 1), MAX_TRACE_LIMIT)
        clamped_offset = max(offset, 0)
        rows = await self._session.execute(
            select(AgentTraceEvent)
            .where(
                AgentTraceEvent.mission_run_id == mission_run_id,
                AgentTraceEvent.merchant_id == merchant_id,
            )
            .order_by(AgentTraceEvent.sequence)
            .offset(clamped_offset)
            .limit(clamped_limit)
        )
        events = tuple(
            TraceEventView(
                sequence=row.sequence,
                event_type=row.event_type.value,
                recorded_at=row.recorded_at,
                payload=dict(row.payload),
            )
            for row in rows.scalars()
        )
        return TraceProjection(total_events=int(total), events=events)

    async def experiment_diagnosis(
        self, experiment_id: uuid.UUID, *, merchant_id: uuid.UUID
    ) -> ExperimentDiagnosisResult:
        """One controlled experiment's product-facing reading."""
        experiment = await self._experiment(experiment_id, merchant_id=merchant_id)
        samples = await self._samples(experiment, merchant_id=merchant_id)
        sample_facts = [
            await self._sample_facts(sample, merchant_id=merchant_id) for sample in samples
        ]
        diagnosis = diagnose_experiment(
            ExperimentFacts(
                experiment_id=experiment.id,
                benchmark_designation=str(
                    experiment.methodology.get("benchmark_designation", "DEVELOPMENT")
                ),
                pair_order=str(experiment.methodology.get("pair_order", "raw_then_compiled")),
                declared_sample_pairs=experiment.sample_count,
                buyer_configuration_digest=experiment.buyer_configuration_digest,
                samples=tuple(sample_facts),
            ),
            engine_identity=engine_identity(),
        )
        return ExperimentDiagnosisResult(
            experiment_id=experiment.id,
            benchmark_designation=diagnosis.benchmark_designation,
            pair_order=diagnosis.pair_order,
            buyer_configuration_digest=experiment.buyer_configuration_digest,
            diagnosis=diagnosis,
        )

    async def recent_run_summaries(
        self, merchant_id: uuid.UUID, *, limit: int = DEFAULT_OVERVIEW_RUNS
    ) -> tuple[OverviewRunSummary, ...]:
        """Bounded run headlines, newest first, for a listing or an overview."""
        clamped = min(max(limit, 1), MAX_OVERVIEW_RUNS)
        return tuple(await self._run_summaries(merchant_id, clamped))

    async def merchant_overview(self, merchant_id: uuid.UUID) -> MerchantOverview:
        summaries = await self._run_summaries(merchant_id, DEFAULT_OVERVIEW_RUNS)
        demand_totals: dict[str, dict[str, int]] = {}
        if summaries:
            grouped = await self._mission_groups(
                [summary.run_id for summary in summaries], merchant_id=merchant_id
            )
            for summary in summaries:
                metrics = compute_metrics(outcomes_of(grouped.get(summary.run_id, [])))
                for entry in metrics.simulated_demand.by_currency:
                    buckets = demand_totals.setdefault(
                        entry.currency,
                        {"potential": 0, "captured": 0, "lost": 0, "not_measured": 0},
                    )
                    buckets["potential"] += entry.potential_amount_minor
                    buckets["captured"] += entry.captured_amount_minor
                    buckets["lost"] += entry.lost_amount_minor
                    buckets["not_measured"] += entry.not_measured_amount_minor

        top_findings_run_id = next(
            (summary.run_id for summary in summaries if summary.status in {"COMPLETED", "ABORTED"}),
            None,
        )
        top_findings: tuple[MerchantFinding, ...] = ()
        if top_findings_run_id is not None:
            detailed = await self.run_diagnostics(top_findings_run_id, merchant_id=merchant_id)
            top_findings = detailed.findings

        return MerchantOverview(
            engine_identity=engine_identity(),
            merchant_id=merchant_id,
            runs=tuple(summaries),
            top_findings=top_findings,
            top_findings_run_id=top_findings_run_id,
            simulated_demand_totals_by_currency=tuple(
                {
                    "currency": currency,
                    "simulated_potential_demand_amount_minor": buckets["potential"],
                    "simulated_captured_demand_amount_minor": buckets["captured"],
                    "simulated_lost_demand_amount_minor": buckets["lost"],
                    "simulated_not_measured_demand_amount_minor": buckets["not_measured"],
                }
                for currency, buckets in sorted(demand_totals.items())
            ),
            latest_experiment=await self._latest_experiment(merchant_id),
            representation_state=await self._representation_state(merchant_id),
        )

    async def _run_summaries(self, merchant_id: uuid.UUID, limit: int) -> list[OverviewRunSummary]:
        runs = await self._recent_runs(merchant_id, limit)
        if not runs:
            return []
        grouped = await self._mission_groups([run.id for run in runs], merchant_id=merchant_id)
        failure_counts = await self._provider_failure_counts(
            [run.id for run in runs], merchant_id=merchant_id
        )
        summaries = []
        for run in runs:
            metrics = compute_metrics(outcomes_of(grouped.get(run.id, [])))
            summaries.append(await self._summary(run, metrics, failure_counts))
        return summaries

    async def _recent_runs(self, merchant_id: uuid.UUID, limit: int) -> list[BenchmarkRun]:
        rows = await self._session.execute(
            select(BenchmarkRun)
            .where(BenchmarkRun.merchant_id == merchant_id)
            .order_by(BenchmarkRun.id.desc())
            .limit(limit)
        )
        return list(rows.scalars())

    async def _mission_groups(
        self, run_ids: list[uuid.UUID], *, merchant_id: uuid.UUID
    ) -> dict[uuid.UUID, list[BenchmarkMissionRun]]:
        mission_rows = await self._session.execute(
            select(BenchmarkMissionRun)
            .options(joinedload(BenchmarkMissionRun.mission))
            .where(
                BenchmarkMissionRun.run_id.in_(run_ids),
                BenchmarkMissionRun.merchant_id == merchant_id,
            )
        )
        grouped: dict[uuid.UUID, list[BenchmarkMissionRun]] = {}
        for row in mission_rows.scalars().unique():
            grouped.setdefault(row.run_id, []).append(row)
        return grouped

    async def _summary(
        self,
        run: BenchmarkRun,
        metrics: BenchmarkMetrics,
        failure_counts: dict[uuid.UUID, int],
    ) -> OverviewRunSummary:
        return OverviewRunSummary(
            run_id=run.id,
            status=run.status.value,
            suite_label=await self._runs.suite_label(run),
            executor_label=run.executor_label,
            started_at=run.started_at,
            completed_at=run.completed_at,
            missions_total=metrics.missions_total,
            missions_succeeded=metrics.missions_succeeded,
            missions_failed=metrics.missions_failed,
            missions_abstained=metrics.missions_abstained,
            missions_errored=metrics.missions_errored,
            task_completion_rate=metrics.task_completion_rate,
            correct_abstention_rate=metrics.correct_abstention_rate,
            unsafe_attempts=metrics.unsafe_attempts,
            unsafe_completions=metrics.unsafe_completions,
            provider_failure_missions=failure_counts.get(run.id, 0),
            simulated_demand_by_currency=tuple(
                {
                    "currency": entry.currency,
                    "simulated_potential_demand_amount_minor": entry.potential_amount_minor,
                    "simulated_captured_demand_amount_minor": entry.captured_amount_minor,
                    "simulated_lost_demand_amount_minor": entry.lost_amount_minor,
                    "simulated_not_measured_demand_amount_minor": entry.not_measured_amount_minor,
                }
                for entry in metrics.simulated_demand.by_currency
            ),
        )

    def _diagnose_mission(
        self,
        result: BenchmarkMissionRun,
        variants: dict[uuid.UUID, _VariantRow],
        facts: TraceFacts,
        pin_verified: bool | None,
    ) -> MissionDiagnosis:
        brief = result.mission.to_brief()
        selection: SelectionFacts | None = None
        if result.selected_variant_id is not None:
            row = variants.get(result.selected_variant_id)
            if row is not None:
                readable = pin_verified is True
                selection = SelectionFacts(
                    variant_id=result.selected_variant_id,
                    sku=row.sku,
                    product_id=row.product_id,
                    # Attributes and category are values, not identities: they are quoted
                    # only while the catalog pin says today's rows are yesterday's.
                    category=row.category if readable else None,
                    attributes=row.attributes if readable else None,
                )
        evidence = MissionDiagnosisInput(
            run_id=result.run_id,
            mission_run_id=result.id,
            mission_key=result.mission.mission_key,
            status=result.status,
            expected_outcome=result.mission.expected_outcome,
            simulated_value_amount_minor=result.mission.simulated_value_amount_minor,
            currency=result.mission.currency,
            failure_reasons=result.failure_reasons,
            unsafe_attempt=result.unsafe_attempt,
            unverified_attempt=result.unverified_attempt,
            unsafe_completion=result.unsafe_completion,
            oracle_confirmed=result.oracle_confirmed,
            selected_quantity=result.selected_quantity,
            checkout_id=result.checkout_id,
            payment_attempt_id=result.payment_attempt_id,
            selection=selection,
            required_attributes=tuple(
                RequiredAttributeFact(name=c.name, operator=c.operator, value=c.value)
                for c in brief.hard_constraints
                if isinstance(c, RequiredAttribute)
            ),
            required_categories=tuple(
                c.category for c in brief.hard_constraints if isinstance(c, AllowedCategory)
            ),
            trace=facts,
        )
        return diagnose_mission(evidence)

    async def _trace_events(
        self, run_id: uuid.UUID, *, merchant_id: uuid.UUID
    ) -> dict[uuid.UUID, list[AgentTraceEvent]]:
        rows = await self._session.execute(
            select(AgentTraceEvent)
            .where(AgentTraceEvent.run_id == run_id, AgentTraceEvent.merchant_id == merchant_id)
            .order_by(AgentTraceEvent.mission_run_id, AgentTraceEvent.sequence)
        )
        grouped: dict[uuid.UUID, list[AgentTraceEvent]] = {}
        for event in rows.scalars():
            grouped.setdefault(event.mission_run_id, []).append(event)
        return grouped

    async def _provider_usage(
        self, run_id: uuid.UUID, *, merchant_id: uuid.UUID
    ) -> dict[uuid.UUID, list[AgentProviderUsage]]:
        rows = await self._session.execute(
            select(AgentProviderUsage).where(
                AgentProviderUsage.run_id == run_id,
                AgentProviderUsage.merchant_id == merchant_id,
            )
        )
        grouped: dict[uuid.UUID, list[AgentProviderUsage]] = {}
        for usage in rows.scalars():
            grouped.setdefault(usage.mission_run_id, []).append(usage)
        return grouped

    async def _selected_variants(
        self, run: BenchmarkRun, *, merchant_id: uuid.UUID
    ) -> dict[uuid.UUID, _VariantRow]:
        identifiers = sorted(
            {
                result.selected_variant_id
                for result in run.mission_runs
                if result.selected_variant_id is not None
            }
        )
        if not identifiers:
            return {}
        rows = await self._session.execute(
            select(Variant.id, Variant.sku, Product.id, Product.category, Variant.attributes)
            .join(Product, Product.id == Variant.product_id)
            .where(Variant.merchant_id == merchant_id, Variant.id.in_(identifiers))
        )
        return {
            row[0]: _VariantRow(
                variant_id=row[0],
                sku=row[1],
                product_id=row[2],
                category=row[3],
                attributes=dict(row[4]) if row[4] is not None else {},
            )
            for row in rows.all()
        }

    async def _catalog_pin_verified(
        self, run: BenchmarkRun, *, merchant_id: uuid.UUID
    ) -> bool | None:
        if run.catalog_hash is None:
            return None
        entries = await self._runs.catalog(merchant_id)
        return catalog_content_hash(entries) == run.catalog_hash

    async def _designation(self, run: BenchmarkRun) -> str | None:
        methodology = (
            await self._session.execute(
                select(CompilerImpactExperiment.methodology)
                .join(
                    CompilerImpactSample,
                    CompilerImpactSample.experiment_id == CompilerImpactExperiment.id,
                )
                .where(
                    CompilerImpactSample.run_id == run.id,
                    CompilerImpactSample.merchant_id == run.merchant_id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        value = methodology.get("benchmark_designation") if isinstance(methodology, dict) else None
        return str(value) if value else None

    async def _experiment(
        self, experiment_id: uuid.UUID, *, merchant_id: uuid.UUID
    ) -> CompilerImpactExperiment:
        row = (
            await self._session.execute(
                select(CompilerImpactExperiment).where(
                    CompilerImpactExperiment.id == experiment_id,
                    CompilerImpactExperiment.merchant_id == merchant_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError(EXPERIMENT_RESOURCE, str(experiment_id))
        return row

    async def _samples(
        self, experiment: CompilerImpactExperiment, *, merchant_id: uuid.UUID
    ) -> list[CompilerImpactSample]:
        rows = await self._session.execute(
            select(CompilerImpactSample)
            .where(
                CompilerImpactSample.experiment_id == experiment.id,
                CompilerImpactSample.merchant_id == merchant_id,
            )
            .order_by(CompilerImpactSample.execution_ordinal)
        )
        return list(rows.scalars())

    async def _sample_facts(
        self, sample: CompilerImpactSample, *, merchant_id: uuid.UUID
    ) -> ExperimentSampleFacts:
        arm = ARM_RAW if sample.representation_kind is RepresentationKind.RAW else ARM_COMPILED
        base = ExperimentSampleFacts(sample_id=sample.id, pair_ordinal=sample.pair_ordinal, arm=arm)
        if sample.run_id is None:
            return base
        run = (
            await self._session.execute(
                select(BenchmarkRun).where(
                    BenchmarkRun.id == sample.run_id,
                    BenchmarkRun.merchant_id == merchant_id,
                )
            )
        ).scalar_one_or_none()
        if run is None:
            return base
        results = await self._mission_rows(sample.run_id, merchant_id=merchant_id)
        events = await self._trace_events(sample.run_id, merchant_id=merchant_id)
        usages = await self._provider_usage(sample.run_id, merchant_id=merchant_id)
        flat_events = [event for items in events.values() for event in items]
        flat_usages = [usage for items in usages.values() for usage in items]
        reported_flags = [
            usage.input_tokens is not None or usage.total_tokens is not None
            for usage in flat_usages
            if usage.measurement_kind is AgentUsageKind.PROVIDER_REPORTED
        ]
        requested_models = sorted(
            {model for model in (u.requested_model for u in flat_usages) if model}
        )
        resolved_models = sorted(
            {model for model in (u.actual_model for u in flat_usages) if model}
        )
        metrics = compute_metrics(outcomes_of(results)) if results else None
        outcome_records = tuple(
            MissionOutcomeFacts(
                mission_key=result.mission.mission_key,
                status=result.status.value,
                primary_failure_reason=None
                if result.primary_failure_reason is None
                else result.primary_failure_reason.value,
            )
            for result in results
            if result.is_terminal and result.status is not MissionRunStatus.ERRORED
        )
        return ExperimentSampleFacts(
            sample_id=sample.id,
            pair_ordinal=sample.pair_ordinal,
            arm=arm,
            run_id=run.id,
            run_status=run.status.value,
            metrics=metrics,
            mission_outcomes=outcome_records,
            provider_failure_missions=sum(
                1
                for mission_events in events.values()
                if any(e.event_type.value == "PROVIDER_ERROR" for e in mission_events)
            ),
            model_invocations=sum(
                1 for event in flat_events if event.event_type.value == "MODEL_REQUEST"
            ),
            tool_calls=sum(1 for event in flat_events if event.event_type.value == "TOOL_CALL"),
            token_usage_reported=all(reported_flags) if reported_flags else None,
            requested_model=requested_models[0] if len(requested_models) == 1 else None,
            resolved_models=tuple(resolved_models),
            agent_implementation_version=_implementation_version(run),
        )

    async def _mission_rows(
        self, run_id: uuid.UUID, *, merchant_id: uuid.UUID
    ) -> list[BenchmarkMissionRun]:
        rows = await self._session.execute(
            select(BenchmarkMissionRun)
            .options(joinedload(BenchmarkMissionRun.mission))
            .where(
                BenchmarkMissionRun.run_id == run_id,
                BenchmarkMissionRun.merchant_id == merchant_id,
            )
        )
        return list(rows.scalars().unique())

    async def _provider_failure_counts(
        self, run_ids: list[uuid.UUID], *, merchant_id: uuid.UUID
    ) -> dict[uuid.UUID, int]:
        rows = await self._session.execute(
            select(
                AgentTraceEvent.run_id,
                func.count(func.distinct(AgentTraceEvent.mission_run_id)),
            )
            .where(
                AgentTraceEvent.run_id.in_(run_ids),
                AgentTraceEvent.merchant_id == merchant_id,
                AgentTraceEvent.event_type == "PROVIDER_ERROR",
            )
            .group_by(AgentTraceEvent.run_id)
        )
        return {row[0]: int(row[1]) for row in rows}

    async def _latest_experiment(self, merchant_id: uuid.UUID) -> OverviewExperimentSummary | None:
        row = (
            await self._session.execute(
                select(CompilerImpactExperiment)
                .where(CompilerImpactExperiment.merchant_id == merchant_id)
                .order_by(CompilerImpactExperiment.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        result = await self.experiment_diagnosis(row.id, merchant_id=merchant_id)
        diagnosis = result.diagnosis
        return OverviewExperimentSummary(
            experiment_id=row.id,
            benchmark_designation=diagnosis.benchmark_designation,
            completed_sample_pairs=diagnosis.completed_sample_pairs,
            conclusion_kind=diagnosis.conclusion.kind,
            conclusion_statement=diagnosis.conclusion.statement,
        )

    async def _representation_state(self, merchant_id: uuid.UUID) -> RepresentationState:
        snapshot = (
            await self._session.execute(
                select(MerchantSourceSnapshot)
                .where(MerchantSourceSnapshot.merchant_id == merchant_id)
                .order_by(MerchantSourceSnapshot.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        compiled = (
            await self._session.execute(
                select(CommerceRepresentation)
                .where(
                    CommerceRepresentation.merchant_id == merchant_id,
                    CommerceRepresentation.producer == RepresentationProducer.COMPILER,
                )
                .order_by(CommerceRepresentation.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        review_required = 0
        if compiled is not None:
            for product in compiled.payload.get("products", []):
                for variant in product.get("variants", []):
                    for attribute in variant.get("attributes", []):
                        fact = attribute.get("fact", {})
                        confidence = fact.get("confidence")
                        review_state = fact.get("review_state")
                        if (
                            confidence == FactConfidence.REVIEW_REQUIRED.value
                            or review_state == "REVIEW_REQUIRED"
                        ):
                            review_required += 1
        return RepresentationState(
            source_snapshot_id=None if snapshot is None else snapshot.id,
            source_snapshot_label=None if snapshot is None else snapshot.label,
            compiled_representation_id=None if compiled is None else compiled.id,
            compiled_representation_label=None if compiled is None else compiled.label,
            review_required_facts=review_required,
        )


def _implementation_version(run: BenchmarkRun) -> int | None:
    configuration = run.agent_configuration
    if not isinstance(configuration, dict):
        return None
    version = configuration.get("agent_implementation_version")
    return version if isinstance(version, int) else None
