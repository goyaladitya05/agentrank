"""Deterministic diagnosis of a controlled raw versus compiled experiment.

An experiment's numbers already exist as benchmark metrics. This module adds the reading a
merchant actually needs: were the two arms comparable, did anything change, how much
simulated demand moved and in which direction, and which methodological caveats must travel
with every one of those statements.

The rules here exist because AgentRank has already met every confound they warn about, in
real experiments: provider throttling that landed on whichever arm ran second, before
retrying existed; experiments run before the discovery boundary made the treatment honest;
single pair studies whose result looked decisive. A warning this module can emit corresponds
to something that actually happened in this repository's history.

Two absolutes shape everything below. No causality beyond the design: a paired comparison
with two samples supports "no difference was observed", never "compilation does nothing".
And no invented statistics: there are no confidence intervals, no significance language and
no weighting anywhere in this module. Counts, sums within one currency, and stated caveats.
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass

from agentrank_api.benchmark.failures import FAILURE_PRECEDENCE, FailureReason
from agentrank_api.benchmark.metrics import (
    BenchmarkMetrics,
    SimulatedDemand,
    SimulatedDemandReport,
)

ARM_RAW = "RAW"
ARM_COMPILED = "COMPILED"

DESIGNATION_DEVELOPMENT = "DEVELOPMENT"
DESIGNATION_EVALUATION = "EVALUATION"

PAIR_ORDER_LEGACY = "raw_then_compiled"
PAIR_ORDER_COUNTERBALANCED = "counterbalanced"

# Buyer implementation versions with known methodology boundaries. These are facts about
# what a version could and could not do, established when those changes shipped; they let a
# historical experiment be explained rather than invalidated.
IMPLEMENTATION_THROTTLE_RETRY = 3
IMPLEMENTATION_DISCOVERY_BOUNDARY = 4


@dataclass(frozen=True, slots=True)
class MissionOutcomeFacts:
    """One mission's terminal position inside one sample."""

    mission_key: str
    status: str
    primary_failure_reason: str | None


@dataclass(frozen=True, slots=True)
class ExperimentSampleFacts:
    """One predeclared sample and what its bound run measured.

    `run_id` None or `run_status` None means this slot never produced a measurement, which
    is itself a fact the comparison must carry rather than hide.
    """

    sample_id: uuid.UUID
    pair_ordinal: int
    arm: str
    run_id: uuid.UUID | None = None
    run_status: str | None = None
    metrics: BenchmarkMetrics | None = None
    mission_outcomes: tuple[MissionOutcomeFacts, ...] = ()
    provider_failure_missions: int = 0
    model_invocations: int = 0
    tool_calls: int = 0
    # Token reporting is three valued in effect: reported everywhere, absent everywhere, or
    # partially present. Partial presence is recorded rather than averaged away.
    token_usage_reported: bool | None = None
    requested_model: str | None = None
    resolved_models: tuple[str, ...] = ()
    agent_implementation_version: int | None = None

    @property
    def completed(self) -> bool:
        return self.run_status == "COMPLETED"


@dataclass(frozen=True, slots=True)
class ExperimentFacts:
    """The frozen plan and the measured samples of one controlled experiment."""

    experiment_id: uuid.UUID
    benchmark_designation: str
    pair_order: str
    declared_sample_pairs: int
    buyer_configuration_digest: str
    samples: tuple[ExperimentSampleFacts, ...] = ()

    @property
    def completed_pairs(self) -> int:
        return sum(
            1 for _, pair in self._pairs() if all(sample.completed for sample in pair.values())
        )

    def _pairs(self) -> list[tuple[int, dict[str, ExperimentSampleFacts]]]:
        grouped: dict[int, dict[str, ExperimentSampleFacts]] = defaultdict(dict)
        for sample in self.samples:
            grouped[sample.pair_ordinal][sample.arm] = sample
        return sorted(grouped.items())


@dataclass(frozen=True, slots=True)
class MethodologyWarning:
    """One stated caveat, with a stable code an API consumer can filter on."""

    code: str
    message: str


@dataclass(frozen=True, slots=True)
class ArmAggregate:
    """What one arm measured, summed only where summation means something."""

    planned_samples: int
    completed_samples: int
    completion_rate_mean: float | None
    metrics_totals: BenchmarkMetrics | None
    provider_failure_missions: int
    model_invocations: int
    tool_calls: int
    resolved_models: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CurrencyDelta:
    """Compiled minus raw, for one currency. Never folded into another currency."""

    currency: str
    potential_amount_minor: int
    captured_amount_minor: int
    lost_amount_minor: int
    not_measured_amount_minor: int


@dataclass(frozen=True, slots=True)
class MissionTransition:
    """One mission whose terminal position differed between paired arms."""

    pair_ordinal: int
    mission_key: str
    raw_status: str
    raw_primary_failure_reason: str | None
    compiled_status: str
    compiled_primary_failure_reason: str | None
    direction: str


TRANSITION_COMPILED_GAIN = "COMPILED_GAIN"
TRANSITION_COMPILED_LOSS = "COMPILED_LOSS"
TRANSITION_CHANGED = "CHANGED"


@dataclass(frozen=True, slots=True)
class ComparisonConclusion:
    """The strongest statement this evidence deterministically supports.

    PARITY means every completed pair agreed on every mission, on safety and on captured
    demand. It does not mean compilation cannot help anywhere; it means it did not help
    here, at this sample size, for this model and this catalog.
    """

    kind: str
    statement: str


CONCLUSION_PARITY = "PARITY"
CONCLUSION_OUTCOME_DIFFERENCES = "OUTCOME_DIFFERENCES"
CONCLUSION_INCOMPLETE = "INCOMPLETE"


@dataclass(frozen=True, slots=True)
class ExperimentDiagnosis:
    """The complete product-facing reading of one controlled experiment."""

    engine_identity: str
    experiment_id: uuid.UUID
    benchmark_designation: str
    pair_order: str
    declared_sample_pairs: int
    completed_sample_pairs: int
    arms: dict[str, ArmAggregate]
    demand_delta_by_currency: tuple[CurrencyDelta, ...]
    mission_transitions: tuple[MissionTransition, ...]
    warnings: tuple[MethodologyWarning, ...]
    conclusion: ComparisonConclusion


def diagnose_experiment(
    facts: ExperimentFacts,
    *,
    engine_identity: str,
) -> ExperimentDiagnosis:
    """Read one experiment, deterministically and without overclaiming."""
    arms = {arm: _aggregate_arm(facts, arm) for arm in (ARM_RAW, ARM_COMPILED)}
    transitions = _mission_transitions(facts)
    warnings = tuple(_warnings(facts, arms))
    conclusion = _conclusion(facts, transitions)
    return ExperimentDiagnosis(
        engine_identity=engine_identity,
        experiment_id=facts.experiment_id,
        benchmark_designation=facts.benchmark_designation,
        pair_order=facts.pair_order,
        declared_sample_pairs=facts.declared_sample_pairs,
        completed_sample_pairs=facts.completed_pairs,
        arms=arms,
        demand_delta_by_currency=_demand_delta(arms),
        mission_transitions=transitions,
        warnings=warnings,
        conclusion=conclusion,
    )


def _aggregate_arm(facts: ExperimentFacts, arm: str) -> ArmAggregate:
    group = [sample for sample in facts.samples if sample.arm == arm]
    completed = [sample for sample in group if sample.completed]
    rates = [
        rate
        for rate in (
            sample.metrics.task_completion_rate if sample.metrics else None for sample in completed
        )
        if rate is not None
    ]
    totals = _sum_metrics([sample.metrics for sample in completed]) if completed else None
    return ArmAggregate(
        planned_samples=len(group),
        completed_samples=len(completed),
        completion_rate_mean=None if not rates else sum(rates) / len(rates),
        metrics_totals=totals,
        provider_failure_missions=sum(sample.provider_failure_missions for sample in group),
        model_invocations=sum(sample.model_invocations for sample in group),
        tool_calls=sum(sample.tool_calls for sample in group),
        resolved_models=tuple(
            sorted({model for sample in group for model in sample.resolved_models})
        ),
    )


def _sum_metrics(metrics: list[BenchmarkMetrics | None]) -> BenchmarkMetrics:
    """Add counts across runs of one suite, keeping demand partitioned per currency.

    Rates are recomputed from the summed counts against the suite fixed denominators, which
    is why the denominators are stored on the metrics rather than derived from what ran.
    """
    demand_buckets: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for entry in metrics:
        assert entry is not None
        for item in entry.simulated_demand.by_currency:
            buckets = demand_buckets[item.currency]
            buckets["potential"] += item.potential_amount_minor
            buckets["captured"] += item.captured_amount_minor
            buckets["not_measured"] += item.not_measured_amount_minor
            buckets["lost"] += item.lost_amount_minor
    return BenchmarkMetrics(
        missions_total=sum(m.missions_total for m in metrics if m),
        missions_succeeded=sum(m.missions_succeeded for m in metrics if m),
        missions_failed=sum(m.missions_failed for m in metrics if m),
        missions_abstained=sum(m.missions_abstained for m in metrics if m),
        missions_errored=sum(m.missions_errored for m in metrics if m),
        missions_unfinished=sum(m.missions_unfinished for m in metrics if m),
        purchase_missions=sum(m.purchase_missions for m in metrics if m),
        control_missions=sum(m.control_missions for m in metrics if m),
        correct_abstentions=sum(m.correct_abstentions for m in metrics if m),
        incorrect_abstentions=sum(m.incorrect_abstentions for m in metrics if m),
        unsafe_attempts=sum(m.unsafe_attempts for m in metrics if m),
        unverified_attempts=sum(m.unverified_attempts for m in metrics if m),
        unsafe_completions=sum(m.unsafe_completions for m in metrics if m),
        mandate_denials_protecting=sum(m.mandate_denials_protecting for m in metrics if m),
        mandate_denials_on_compliant_attempt=sum(
            m.mandate_denials_on_compliant_attempt for m in metrics if m
        ),
        oracle_disagreements=sum(m.oracle_disagreements for m in metrics if m),
        oracle_unchecked=sum(m.oracle_unchecked for m in metrics if m),
        primary_failure_counts=_sum_reason_counts(metrics),
        contributing_failure_counts=_sum_contributing_counts(metrics),
        simulated_demand=SimulatedDemandReport(
            by_currency=tuple(
                SimulatedDemand(
                    currency=currency,
                    potential_amount_minor=buckets["potential"],
                    captured_amount_minor=buckets["captured"],
                    not_measured_amount_minor=buckets["not_measured"],
                )
                for currency, buckets in sorted(demand_buckets.items())
            )
        ),
    )


def _sum_reason_counts(
    metrics: list[BenchmarkMetrics | None],
) -> dict[FailureReason, int]:
    totals: dict[FailureReason, int] = defaultdict(int)
    for entry in metrics:
        assert entry is not None
        for reason, count in entry.primary_failure_counts.items():
            totals[reason] += count
    return {reason: totals[reason] for reason in FAILURE_PRECEDENCE if reason in totals}


def _sum_contributing_counts(
    metrics: list[BenchmarkMetrics | None],
) -> dict[FailureReason, int]:
    totals: dict[FailureReason, int] = defaultdict(int)
    for entry in metrics:
        assert entry is not None
        for reason, count in entry.contributing_failure_counts.items():
            totals[reason] += count
    return {reason: totals[reason] for reason in FAILURE_PRECEDENCE if reason in totals}


def _mission_transitions(facts: ExperimentFacts) -> tuple[MissionTransition, ...]:
    transitions: list[MissionTransition] = []
    for pair_ordinal, pair in facts._pairs():
        raw, compiled = pair.get(ARM_RAW), pair.get(ARM_COMPILED)
        if raw is None or compiled is None or not (raw.completed and compiled.completed):
            continue
        raw_outcomes = {outcome.mission_key: outcome for outcome in raw.mission_outcomes}
        compiled_outcomes = {outcome.mission_key: outcome for outcome in compiled.mission_outcomes}
        for key in sorted(raw_outcomes.keys() & compiled_outcomes.keys()):
            raw_outcome, compiled_outcome = raw_outcomes[key], compiled_outcomes[key]
            if (raw_outcome.status, raw_outcome.primary_failure_reason) == (
                compiled_outcome.status,
                compiled_outcome.primary_failure_reason,
            ):
                continue
            transitions.append(
                MissionTransition(
                    pair_ordinal=pair_ordinal,
                    mission_key=key,
                    raw_status=raw_outcome.status,
                    raw_primary_failure_reason=raw_outcome.primary_failure_reason,
                    compiled_status=compiled_outcome.status,
                    compiled_primary_failure_reason=compiled_outcome.primary_failure_reason,
                    direction=_direction(raw_outcome, compiled_outcome),
                )
            )
    return tuple(transitions)


def _direction(raw: MissionOutcomeFacts, compiled: MissionOutcomeFacts) -> str:
    if compiled.status == "SUCCEEDED" and raw.status != "SUCCEEDED":
        return TRANSITION_COMPILED_GAIN
    if raw.status == "SUCCEEDED" and compiled.status != "SUCCEEDED":
        return TRANSITION_COMPILED_LOSS
    return TRANSITION_CHANGED


def _demand_delta(arms: dict[str, ArmAggregate]) -> tuple[CurrencyDelta, ...]:
    raw_totals, compiled_totals = _demand_totals(arms[ARM_RAW]), _demand_totals(arms[ARM_COMPILED])
    currencies = sorted(raw_totals.keys() | compiled_totals.keys())
    deltas = []
    for currency in currencies:
        raw_entry, compiled_entry = (
            raw_totals.get(currency, {}),
            compiled_totals.get(currency, {}),
        )
        deltas.append(
            CurrencyDelta(
                currency=currency,
                potential_amount_minor=(
                    compiled_entry.get("potential", 0) - raw_entry.get("potential", 0)
                ),
                captured_amount_minor=(
                    compiled_entry.get("captured", 0) - raw_entry.get("captured", 0)
                ),
                lost_amount_minor=(compiled_entry.get("lost", 0) - raw_entry.get("lost", 0)),
                not_measured_amount_minor=(
                    compiled_entry.get("not_measured", 0) - raw_entry.get("not_measured", 0)
                ),
            )
        )
    return tuple(deltas)


def _demand_totals(arm: ArmAggregate) -> dict[str, dict[str, int]]:
    totals: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    totals_by_currency = getattr(arm.metrics_totals, "simulated_demand", None)
    if totals_by_currency is None:
        return totals
    for entry in totals_by_currency.by_currency:
        buckets = totals[entry.currency]
        buckets["potential"] += entry.potential_amount_minor
        buckets["captured"] += entry.captured_amount_minor
        buckets["lost"] += entry.lost_amount_minor
        buckets["not_measured"] += entry.not_measured_amount_minor
    return totals


def _conclusion(
    facts: ExperimentFacts, transitions: tuple[MissionTransition, ...]
) -> ComparisonConclusion:
    complete = [
        (ordinal, pair)
        for ordinal, pair in facts._pairs()
        if all(sample.completed for sample in pair.values())
        and set(pair) == {ARM_RAW, ARM_COMPILED}
    ]
    if not complete:
        return ComparisonConclusion(
            kind=CONCLUSION_INCOMPLETE,
            statement=(
                "No complete raw and compiled pair has been measured yet, so this"
                " experiment supports no comparison."
            ),
        )
    if not transitions:
        return ComparisonConclusion(
            kind=CONCLUSION_PARITY,
            statement=(
                f"Across {len(complete)} completed paired sample(s), raw and agent-ready"
                " representations produced identical mission outcomes, safety results and"
                " captured simulated demand. No measurable compiler benefit was observed at"
                " this sample size."
            ),
        )
    gains = sum(1 for t in transitions if t.direction is TRANSITION_COMPILED_GAIN)
    losses = sum(1 for t in transitions if t.direction is TRANSITION_COMPILED_LOSS)
    return ComparisonConclusion(
        kind=CONCLUSION_OUTCOME_DIFFERENCES,
        statement=(
            f"Across {len(complete)} completed paired sample(s), {len(transitions)} mission(s)"
            f" differed between arms: {gains} favoured the agent-ready representation,"
            f" {losses} favoured the raw storefront. Differences at this sample size are"
            " observations, not estimates of effect."
        ),
    )


def _warnings(facts: ExperimentFacts, arms: dict[str, ArmAggregate]) -> list[MethodologyWarning]:
    warnings: list[MethodologyWarning] = []

    if facts.benchmark_designation.upper() == DESIGNATION_DEVELOPMENT:
        warnings.append(
            MethodologyWarning(
                code="DEVELOPMENT_BENCHMARK",
                message=(
                    "This experiment ran on a development benchmark; it is not independent"
                    " evaluation evidence."
                ),
            )
        )

    versions = {
        sample.agent_implementation_version
        for sample in facts.samples
        if sample.agent_implementation_version is not None
    }
    if any(version < IMPLEMENTATION_DISCOVERY_BOUNDARY for version in versions):
        warnings.append(
            MethodologyWarning(
                code="PRE_DISCOVERY_BOUNDARY",
                message=(
                    "This experiment predates the discovery treatment boundary: raw buyers"
                    " could see structured attribute dictionaries the current storefront"
                    " hides, so its arms differ from what the same experiment would deliver"
                    " today."
                ),
            )
        )
    if any(version < IMPLEMENTATION_THROTTLE_RETRY for version in versions):
        warnings.append(
            MethodologyWarning(
                code="PRE_THROTTLE_RETRY",
                message=(
                    "This experiment predates bounded provider retry pacing, so a single"
                    " throttled invocation could end a mission."
                ),
            )
        )

    if facts.pair_order == PAIR_ORDER_LEGACY:
        warnings.append(
            MethodologyWarning(
                code="NOT_COUNTERBALANCED",
                message=(
                    "Pair order was not counterbalanced: every raw sample ran first, so arm"
                    " differences may partly reflect accumulated provider quota pressure."
                ),
            )
        )

    total_provider_failures = sum(arm.provider_failure_missions for arm in arms.values())
    if total_provider_failures:
        warnings.append(
            MethodologyWarning(
                code="PROVIDER_FAILURES_PRESENT",
                message=(
                    f"{total_provider_failures} mission(s) recorded provider failures. Their"
                    " outcomes reflect provider availability as much as either"
                    " representation."
                ),
            )
        )

    raw_resolved, compiled_resolved = (
        arms[ARM_RAW].resolved_models,
        arms[ARM_COMPILED].resolved_models,
    )
    if raw_resolved != compiled_resolved:
        warnings.append(
            MethodologyWarning(
                code="RESOLVED_MODEL_MISMATCH",
                message=(
                    "The arms were answered by different sets of resolved models, so outcome"
                    " differences may be model behaviour rather than treatment effects."
                ),
            )
        )

    incomplete = facts.declared_sample_pairs - facts.completed_pairs
    if incomplete > 0:
        warnings.append(
            MethodologyWarning(
                code="INCOMPLETE_PAIRS",
                message=(
                    f"{incomplete} of {facts.declared_sample_pairs} declared pair(s) have at"
                    " least one arm without a completed run."
                ),
            )
        )

    if facts.completed_pairs < 2:
        warnings.append(
            MethodologyWarning(
                code="SMALL_SAMPLE",
                message=(
                    f"{facts.completed_pairs} completed pair(s). A single paired sample"
                    " cannot distinguish treatment effects from ordinary variation."
                ),
            )
        )

    if any(sample.token_usage_reported is False for sample in facts.samples):
        warnings.append(
            MethodologyWarning(
                code="TOKEN_USAGE_UNAVAILABLE",
                message=(
                    "Some provider invocations reported no token usage. Interaction cost is"
                    " compared through invocation and tool-call counts only."
                ),
            )
        )

    if (
        any(
            warning.code in {"NOT_COUNTERBALANCED", "PROVIDER_FAILURES_PRESENT"}
            for warning in warnings
        )
        or incomplete > 0
    ):
        warnings.append(
            MethodologyWarning(
                code="NOT_CAUSALLY_INTERPRETABLE",
                message=(
                    "Given the caveats above, differences between the arms must not be read"
                    " as caused by the compiler-produced representation."
                ),
            )
        )
    return warnings
