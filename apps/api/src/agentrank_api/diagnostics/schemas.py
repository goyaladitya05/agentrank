"""Product-facing read models for diagnostics.

These are the API contract for everything a merchant dashboard will consume, and they are
deliberately shaped for that reader rather than mirrored off the domain: simulated demand
fields always carry the word simulated, ownership and actionability travel beside every
finding, evidence references name the row they point at, and nothing here exposes compiler
provenance, provider payloads beyond their redacted trace form, or any internal ORM object.

Mapping from the diagnostics layer's frozen dataclasses is written out field by field, per
this repository's rule that adding a field to a domain type must never silently change an
API response.
"""

import uuid
from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel

from agentrank_api.benchmark.metrics import BenchmarkMetrics
from agentrank_api.diagnostics.codes import Actionability, DiagnosticOwner, EvidenceLevel, Severity
from agentrank_api.diagnostics.experiment import ExperimentDiagnosis
from agentrank_api.diagnostics.findings import MerchantFinding
from agentrank_api.diagnostics.mission import (
    MissionDiagnosis,
    MissionFinding,
    SimulatedDemandEffect,
)
from agentrank_api.diagnostics.service import (
    MerchantOverview,
    OverviewRunSummary,
    RunDiagnostics,
    TraceProjection,
)


class SimulatedDemandEffectView(BaseModel):
    """Simulated benchmark demand one mission or finding carried, in one bucket."""

    currency: str
    bucket: str
    amount_minor: int

    @classmethod
    def from_domain(cls, effect: SimulatedDemandEffect) -> Self:
        return cls(currency=effect.currency, bucket=effect.bucket, amount_minor=effect.amount_minor)


class SimulatedDemandBucketView(BaseModel):
    """Simulated demand totals for one currency, labelled simulated at every field."""

    currency: str
    simulated_potential_demand_amount_minor: int
    simulated_captured_demand_amount_minor: int
    simulated_lost_demand_amount_minor: int
    simulated_not_measured_demand_amount_minor: int


class EvidenceReferenceView(BaseModel):
    kind: str
    identifier: str
    establishes: str


class MissionFindingView(BaseModel):
    code: str
    owner: DiagnosticOwner
    actionability: Actionability
    severity: Severity
    evidence_level: EvidenceLevel
    summary: str
    recommendation: str | None
    attribute_keys: tuple[str, ...]
    product_ids: tuple[uuid.UUID, ...]
    variant_ids: tuple[uuid.UUID, ...]
    evidence: list[EvidenceReferenceView]

    @classmethod
    def from_domain(cls, finding: MissionFinding) -> Self:
        return cls(
            code=finding.code.value,
            owner=finding.owner,
            actionability=finding.actionability,
            severity=finding.severity,
            evidence_level=finding.evidence_level,
            summary=finding.summary,
            recommendation=finding.recommendation,
            attribute_keys=finding.attribute_keys,
            product_ids=finding.product_ids,
            variant_ids=finding.variant_ids,
            evidence=[
                EvidenceReferenceView(
                    kind=reference.kind,
                    identifier=reference.identifier,
                    establishes=reference.establishes,
                )
                for reference in finding.evidence
            ],
        )


class MissionDiagnosisView(BaseModel):
    engine_identity: str
    run_id: uuid.UUID
    mission_run_id: uuid.UUID
    mission_key: str
    status: str
    outcome: str
    primary_code: str | None
    findings: list[MissionFindingView]
    simulated_demand: list[SimulatedDemandEffectView]
    model_invocations: int | None
    tool_calls: int | None
    tool_errors: int | None

    @classmethod
    def from_domain(cls, diagnosis: MissionDiagnosis) -> Self:
        interactions = diagnosis.interactions
        return cls(
            engine_identity=diagnosis.engine_identity,
            run_id=diagnosis.run_id,
            mission_run_id=diagnosis.mission_run_id,
            mission_key=diagnosis.mission_key,
            status=diagnosis.status.value,
            outcome=diagnosis.outcome,
            primary_code=None if diagnosis.primary is None else diagnosis.primary.code.value,
            findings=[MissionFindingView.from_domain(finding) for finding in diagnosis.findings],
            simulated_demand=[
                SimulatedDemandEffectView.from_domain(effect)
                for effect in diagnosis.simulated_demand
            ],
            model_invocations=None if interactions is None else interactions.model_invocations,
            tool_calls=None if interactions is None else interactions.tool_calls,
            tool_errors=None if interactions is None else interactions.tool_errors,
        )


class MetricsView(BaseModel):
    """One run's raw counts. There is deliberately no weighted score anywhere near this."""

    missions_total: int
    missions_succeeded: int
    missions_failed: int
    missions_abstained: int
    missions_errored: int
    missions_unfinished: int
    purchase_missions: int
    control_missions: int
    correct_abstentions: int
    incorrect_abstentions: int
    task_completion_rate: float | None
    correct_abstention_rate: float | None
    unsafe_attempts: int
    unverified_attempts: int
    unsafe_completions: int
    mandate_denials_protecting: int
    mandate_denials_on_compliant_attempt: int
    oracle_disagreements: int
    oracle_unchecked: int
    primary_failure_counts: dict[str, int]

    @classmethod
    def from_domain(cls, metrics: BenchmarkMetrics) -> Self:
        return cls(
            missions_total=metrics.missions_total,
            missions_succeeded=metrics.missions_succeeded,
            missions_failed=metrics.missions_failed,
            missions_abstained=metrics.missions_abstained,
            missions_errored=metrics.missions_errored,
            missions_unfinished=metrics.missions_unfinished,
            purchase_missions=metrics.purchase_missions,
            control_missions=metrics.control_missions,
            correct_abstentions=metrics.correct_abstentions,
            incorrect_abstentions=metrics.incorrect_abstentions,
            task_completion_rate=metrics.task_completion_rate,
            correct_abstention_rate=metrics.correct_abstention_rate,
            unsafe_attempts=metrics.unsafe_attempts,
            unverified_attempts=metrics.unverified_attempts,
            unsafe_completions=metrics.unsafe_completions,
            mandate_denials_protecting=metrics.mandate_denials_protecting,
            mandate_denials_on_compliant_attempt=metrics.mandate_denials_on_compliant_attempt,
            oracle_disagreements=metrics.oracle_disagreements,
            oracle_unchecked=metrics.oracle_unchecked,
            primary_failure_counts={
                reason.value: count for reason, count in metrics.primary_failure_counts.items()
            },
        )


class MerchantFindingView(BaseModel):
    key: str
    code: str
    owner: DiagnosticOwner
    actionability: Actionability
    severity: Severity
    evidence_level: EvidenceLevel
    title: str
    recommendation: str | None
    mission_run_ids: tuple[uuid.UUID, ...]
    mission_keys: tuple[str, ...]
    product_ids: tuple[uuid.UUID, ...]
    variant_ids: tuple[uuid.UUID, ...]
    attribute_keys: tuple[str, ...]
    simulated_demand: list[SimulatedDemandEffectView]

    @classmethod
    def from_domain(cls, finding: MerchantFinding) -> Self:
        return cls(
            key=finding.key,
            code=finding.code.value,
            owner=finding.owner,
            actionability=finding.actionability,
            severity=finding.severity,
            evidence_level=finding.evidence_level,
            title=finding.title,
            recommendation=finding.recommendation,
            mission_run_ids=finding.mission_run_ids,
            mission_keys=finding.mission_keys,
            product_ids=finding.product_ids,
            variant_ids=finding.variant_ids,
            attribute_keys=finding.attribute_keys,
            simulated_demand=[
                SimulatedDemandEffectView.from_domain(effect) for effect in finding.simulated_demand
            ],
        )


class RunProviderHealthView(BaseModel):
    missions_with_provider_errors: int
    terminated_outages: int
    recovered_throttles: int
    requested_model: str | None
    resolved_models: tuple[str, ...]


class RunDiagnosticsView(BaseModel):
    """Everything a merchant needs to read one benchmark run."""

    engine_identity: str
    run_id: uuid.UUID
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
    catalog_pin_verified: bool | None
    metrics: MetricsView
    findings: list[MerchantFindingView]
    missions: list[MissionDiagnosisView]
    provider_health: RunProviderHealthView
    simulated_demand: list[SimulatedDemandBucketView]

    @classmethod
    def from_domain(cls, diagnostics: RunDiagnostics) -> Self:
        demand = diagnostics.metrics.simulated_demand
        health = diagnostics.provider_health
        return cls(
            engine_identity=diagnostics.engine_identity,
            run_id=diagnostics.run_id,
            status=diagnostics.status,
            suite_label=diagnostics.suite_label,
            environment_label=diagnostics.environment_label,
            representation_id=diagnostics.representation_id,
            representation_label=diagnostics.representation_label,
            catalog_hash=diagnostics.catalog_hash,
            evaluator_version=diagnostics.evaluator_version,
            executor_label=diagnostics.executor_label,
            executor_revision=diagnostics.executor_revision,
            agent_implementation_version=diagnostics.agent_implementation_version,
            benchmark_designation=diagnostics.benchmark_designation,
            created_at=diagnostics.created_at,
            started_at=diagnostics.started_at,
            completed_at=diagnostics.completed_at,
            catalog_pin_verified=diagnostics.catalog_pin_verified,
            metrics=MetricsView.from_domain(diagnostics.metrics),
            findings=[MerchantFindingView.from_domain(finding) for finding in diagnostics.findings],
            missions=[
                MissionDiagnosisView.from_domain(mission) for mission in diagnostics.missions
            ],
            provider_health=RunProviderHealthView(
                missions_with_provider_errors=health.missions_with_provider_errors,
                terminated_outages=health.terminated_outages,
                recovered_throttles=health.recovered_throttles,
                requested_model=health.requested_model,
                resolved_models=health.resolved_models,
            ),
            simulated_demand=[
                SimulatedDemandBucketView(
                    currency=entry.currency,
                    simulated_potential_demand_amount_minor=entry.potential_amount_minor,
                    simulated_captured_demand_amount_minor=entry.captured_amount_minor,
                    simulated_lost_demand_amount_minor=entry.lost_amount_minor,
                    simulated_not_measured_demand_amount_minor=entry.not_measured_amount_minor,
                )
                for entry in demand.by_currency
            ],
        )


class TraceEventItemView(BaseModel):
    sequence: int
    event_type: str
    recorded_at: datetime
    payload: dict[str, Any]


class TraceProjectionView(BaseModel):
    total_events: int
    events: list[TraceEventItemView]

    @classmethod
    def from_domain(cls, projection: TraceProjection) -> Self:
        return cls(
            total_events=projection.total_events,
            events=[
                TraceEventItemView(
                    sequence=event.sequence,
                    event_type=event.event_type,
                    recorded_at=event.recorded_at,
                    payload=event.payload,
                )
                for event in projection.events
            ],
        )


class RunSummaryView(BaseModel):
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
    simulated_demand: list[SimulatedDemandBucketView]


class RepresentationStateView(BaseModel):
    source_snapshot_id: uuid.UUID | None
    source_snapshot_label: str | None
    compiled_representation_id: uuid.UUID | None
    compiled_representation_label: str | None
    review_required_facts: int


class LatestExperimentView(BaseModel):
    experiment_id: uuid.UUID
    benchmark_designation: str
    completed_sample_pairs: int
    conclusion_kind: str
    conclusion_statement: str


class MerchantOverviewView(BaseModel):
    engine_identity: str
    merchant_id: uuid.UUID
    runs: list[RunSummaryView]
    top_findings: list[MerchantFindingView]
    top_findings_run_id: uuid.UUID | None
    simulated_demand_totals_by_currency: list[SimulatedDemandBucketView]
    latest_experiment: LatestExperimentView | None
    representation_state: RepresentationStateView


class MethodologyWarningView(BaseModel):
    code: str
    message: str


class ArmAggregateView(BaseModel):
    arm: str
    planned_samples: int
    completed_samples: int
    completion_rate_mean: float | None
    provider_failure_missions: int
    model_invocations: int
    tool_calls: int
    resolved_models: tuple[str, ...]
    metrics_totals: MetricsView | None


class CurrencyDeltaView(BaseModel):
    currency: str
    simulated_potential_delta_amount_minor: int
    simulated_captured_delta_amount_minor: int
    simulated_lost_delta_amount_minor: int
    simulated_not_measured_delta_amount_minor: int


class MissionTransitionView(BaseModel):
    pair_ordinal: int
    mission_key: str
    raw_status: str
    raw_primary_failure_reason: str | None
    compiled_status: str
    compiled_primary_failure_reason: str | None
    direction: str


class ComparisonConclusionView(BaseModel):
    kind: str
    statement: str


class ExperimentComparisonView(BaseModel):
    engine_identity: str
    experiment_id: uuid.UUID
    buyer_configuration_digest: str
    benchmark_designation: str
    pair_order: str
    declared_sample_pairs: int
    completed_sample_pairs: int
    arms: list[ArmAggregateView]
    demand_delta_by_currency: list[CurrencyDeltaView]
    mission_transitions: list[MissionTransitionView]
    warnings: list[MethodologyWarningView]
    conclusion: ComparisonConclusionView


def overview_view(overview: MerchantOverview) -> MerchantOverviewView:
    return MerchantOverviewView(
        engine_identity=overview.engine_identity,
        merchant_id=overview.merchant_id,
        runs=[run_summary_view(summary) for summary in overview.runs],
        top_findings=[
            MerchantFindingView.from_domain(finding) for finding in overview.top_findings
        ],
        top_findings_run_id=overview.top_findings_run_id,
        simulated_demand_totals_by_currency=[
            SimulatedDemandBucketView(**entry)
            for entry in overview.simulated_demand_totals_by_currency
        ],
        latest_experiment=None
        if overview.latest_experiment is None
        else LatestExperimentView(
            experiment_id=overview.latest_experiment.experiment_id,
            benchmark_designation=overview.latest_experiment.benchmark_designation,
            completed_sample_pairs=overview.latest_experiment.completed_sample_pairs,
            conclusion_kind=overview.latest_experiment.conclusion_kind,
            conclusion_statement=overview.latest_experiment.conclusion_statement,
        ),
        representation_state=RepresentationStateView(
            source_snapshot_id=overview.representation_state.source_snapshot_id,
            source_snapshot_label=overview.representation_state.source_snapshot_label,
            compiled_representation_id=overview.representation_state.compiled_representation_id,
            compiled_representation_label=(
                overview.representation_state.compiled_representation_label
            ),
            review_required_facts=overview.representation_state.review_required_facts,
        ),
    )


def run_summary_view(summary: OverviewRunSummary) -> RunSummaryView:
    return RunSummaryView(
        run_id=summary.run_id,
        status=summary.status,
        suite_label=summary.suite_label,
        executor_label=summary.executor_label,
        started_at=summary.started_at,
        completed_at=summary.completed_at,
        missions_total=summary.missions_total,
        missions_succeeded=summary.missions_succeeded,
        missions_failed=summary.missions_failed,
        missions_abstained=summary.missions_abstained,
        missions_errored=summary.missions_errored,
        task_completion_rate=summary.task_completion_rate,
        correct_abstention_rate=summary.correct_abstention_rate,
        unsafe_attempts=summary.unsafe_attempts,
        unsafe_completions=summary.unsafe_completions,
        provider_failure_missions=summary.provider_failure_missions,
        simulated_demand=[
            SimulatedDemandBucketView(**entry) for entry in summary.simulated_demand_by_currency
        ],
    )


def experiment_view(result: Any) -> ExperimentComparisonView:
    """Map the service's experiment result onto the wire, without inventing causality."""
    diagnosis: ExperimentDiagnosis = result.diagnosis
    arms = []
    for arm_name in ("RAW", "COMPILED"):
        arm = diagnosis.arms.get(arm_name)
        if arm is None:
            continue
        arms.append(
            ArmAggregateView(
                arm=arm_name,
                planned_samples=arm.planned_samples,
                completed_samples=arm.completed_samples,
                completion_rate_mean=arm.completion_rate_mean,
                provider_failure_missions=arm.provider_failure_missions,
                model_invocations=arm.model_invocations,
                tool_calls=arm.tool_calls,
                resolved_models=arm.resolved_models,
                metrics_totals=None
                if arm.metrics_totals is None
                else MetricsView.from_domain(arm.metrics_totals),
            )
        )
    return ExperimentComparisonView(
        engine_identity=diagnosis.engine_identity,
        experiment_id=diagnosis.experiment_id,
        buyer_configuration_digest=result.buyer_configuration_digest,
        benchmark_designation=diagnosis.benchmark_designation,
        pair_order=diagnosis.pair_order,
        declared_sample_pairs=diagnosis.declared_sample_pairs,
        completed_sample_pairs=diagnosis.completed_sample_pairs,
        arms=arms,
        demand_delta_by_currency=[
            CurrencyDeltaView(
                currency=delta.currency,
                simulated_potential_delta_amount_minor=delta.potential_amount_minor,
                simulated_captured_delta_amount_minor=delta.captured_amount_minor,
                simulated_lost_delta_amount_minor=delta.lost_amount_minor,
                simulated_not_measured_delta_amount_minor=delta.not_measured_amount_minor,
            )
            for delta in diagnosis.demand_delta_by_currency
        ],
        mission_transitions=[
            MissionTransitionView(
                pair_ordinal=transition.pair_ordinal,
                mission_key=transition.mission_key,
                raw_status=transition.raw_status,
                raw_primary_failure_reason=transition.raw_primary_failure_reason,
                compiled_status=transition.compiled_status,
                compiled_primary_failure_reason=transition.compiled_primary_failure_reason,
                direction=transition.direction,
            )
            for transition in diagnosis.mission_transitions
        ],
        warnings=[
            MethodologyWarningView(code=warning.code, message=warning.message)
            for warning in diagnosis.warnings
        ],
        conclusion=ComparisonConclusionView(
            kind=diagnosis.conclusion.kind,
            statement=diagnosis.conclusion.statement,
        ),
    )
