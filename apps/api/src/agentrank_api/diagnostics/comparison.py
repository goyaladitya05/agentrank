"""Reading one benchmark run against an earlier one, without overclaiming what that shows.

A merchant who publishes a new agent-ready representation and asks for a re-evaluation wants to
know whether anything changed. This answers that, and it is careful about what kind of answer it
is: two runs at two moments are a before and an after, not a controlled experiment. The
representation is not the only thing that could have moved between them, and nothing here
pretends otherwise.

The pure part is here. It takes flattened facts about two runs and produces the deltas, the
per mission transitions, the methodology caveats and the strongest conclusion the evidence
supports. No clock, no database, no model, so the whole thing is assertable against hand written
facts and cannot quietly start reading a row.

Four rules govern the output and none of them is negotiable:

- raw counts and the two existing rates, never a weighted score. There is no AgentRank number
  here and this module has nowhere to put one.
- simulated demand is grouped by currency and never summed across currencies, and every field
  that carries it says simulated.
- unknown provider token usage stays unknown. It is never rendered as zero and never quietly
  excluded from a total that then reads as complete.
- every comparison carries `NOT_A_CONTROLLED_EXPERIMENT`. Even when every methodology dimension
  matches, two runs separated in time differ in whatever else moved, so a difference between
  them is an observation and not an effect.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from agentrank_api.benchmark.metrics import BenchmarkMetrics
from agentrank_api.diagnostics.experiment import (
    CONCLUSION_INCOMPLETE,
    CONCLUSION_OUTCOME_DIFFERENCES,
    CONCLUSION_PARITY,
    ComparisonConclusion,
    MethodologyWarning,
)

DEMAND_POTENTIAL = "POTENTIAL"
DEMAND_CAPTURED = "CAPTURED"
DEMAND_LOST = "LOST"
DEMAND_NOT_MEASURED = "NOT_MEASURED"

TRANSITION_IMPROVED = "IMPROVED"
TRANSITION_REGRESSED = "REGRESSED"
TRANSITION_CHANGED = "CHANGED"

# Which mission statuses count as the buyer having done what the ground truth said was possible.
# Used only to give a transition a direction; the metrics themselves come from the evaluator.
_SUCCESS = "SUCCEEDED"


@dataclass(frozen=True, slots=True)
class MissionOutcomeFact:
    """One mission's terminal position in one run."""

    mission_key: str
    status: str
    primary_failure_reason: str | None


@dataclass(frozen=True, slots=True)
class RunFacts:
    """Everything about one run that a comparison is allowed to read.

    Flattened by the read layer from persisted rows. Nothing a buying agent said about itself
    reaches here, and there is no field for a narrative, a self reported outcome or a score.
    """

    run_id: uuid.UUID
    status: str
    suite_label: str
    suite_definition_hash: str | None
    environment_label: str | None
    catalog_hash: str | None
    evaluator_version: str | None
    executor_label: str | None
    executor_revision: str | None
    buyer_configuration_digest: str | None
    representation_id: uuid.UUID | None
    requested_models: tuple[str, ...]
    resolved_models: tuple[str, ...]
    metrics: BenchmarkMetrics
    missions_with_provider_errors: int
    terminated_provider_outages: int
    model_invocations: int | None
    tool_calls: int | None
    # None when this run recorded no provider invocation at all, False when at least one
    # invocation reported no token usage, True when every one of them did. Three states rather
    # than two, because "nobody asked a model" and "a model answered and said nothing about
    # tokens" are different facts and neither of them is zero.
    token_usage_reported: bool | None
    outcomes: tuple[MissionOutcomeFact, ...]
    started_at: datetime | None
    completed_at: datetime | None

    @property
    def is_complete(self) -> bool:
        return self.status == "COMPLETED"

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at is None or self.completed_at is None:
            return None
        return (self.completed_at - self.started_at).total_seconds()


@dataclass(frozen=True, slots=True)
class CountChange:
    """One count before and after. Absolute values only; no ratio is derived from these."""

    key: str
    before: int
    after: int

    @property
    def delta(self) -> int:
        return self.after - self.before


@dataclass(frozen=True, slots=True)
class RateChange:
    """One of the two existing rates, before and after.

    Either side may be None, which means the denominator the rate is over was empty. None is
    never rendered as zero and a delta over it is not computed.
    """

    key: str
    before: float | None
    after: float | None

    @property
    def delta(self) -> float | None:
        if self.before is None or self.after is None:
            return None
        return self.after - self.before


@dataclass(frozen=True, slots=True)
class SimulatedDemandChange:
    """Simulated demand in one bucket of one currency, before and after.

    One row per currency per bucket, and nothing here adds two currencies together.
    """

    currency: str
    bucket: str
    before_amount_minor: int
    after_amount_minor: int

    @property
    def delta_amount_minor(self) -> int:
        return self.after_amount_minor - self.before_amount_minor


@dataclass(frozen=True, slots=True)
class MissionTransition:
    """One mission that ended somewhere different in the two runs."""

    mission_key: str
    before_status: str | None
    before_primary_failure_reason: str | None
    after_status: str | None
    after_primary_failure_reason: str | None
    direction: str


@dataclass(frozen=True, slots=True)
class InteractionChange:
    """Observed interaction cost, before and after, where it was observed at all.

    Counts of provider round trips and tool calls, which are facts this system records. Token
    usage is deliberately absent as a total: a run in which some invocation reported none has no
    honest total, and the warning says so instead of a number filling the gap.
    """

    model_invocations: CountChange | None
    tool_calls: CountChange | None
    token_usage_complete: bool


@dataclass(frozen=True, slots=True)
class RunComparison:
    """One before and after reading, with everything that qualifies it attached."""

    engine_identity: str
    baseline_run_id: uuid.UUID
    candidate_run_id: uuid.UUID
    comparable: bool
    counts: tuple[CountChange, ...]
    rates: tuple[RateChange, ...]
    simulated_demand: tuple[SimulatedDemandChange, ...]
    transitions: tuple[MissionTransition, ...]
    interactions: InteractionChange
    runtime_seconds: tuple[float | None, float | None]
    warnings: tuple[MethodologyWarning, ...]
    conclusion: ComparisonConclusion


def compare_runs(baseline: RunFacts, candidate: RunFacts, *, engine_identity: str) -> RunComparison:
    """Read the candidate run against the baseline, deterministically and without overclaiming."""
    warnings = _warnings(baseline, candidate)
    fatal = any(warning.code in _NOT_COMPARABLE_CODES for warning in warnings)
    transitions = _transitions(baseline, candidate)
    return RunComparison(
        engine_identity=engine_identity,
        baseline_run_id=baseline.run_id,
        candidate_run_id=candidate.run_id,
        comparable=not fatal,
        counts=_counts(baseline, candidate),
        rates=_rates(baseline, candidate),
        simulated_demand=_demand(baseline, candidate),
        transitions=transitions,
        interactions=_interactions(baseline, candidate),
        runtime_seconds=(baseline.duration_seconds, candidate.duration_seconds),
        warnings=warnings,
        conclusion=_conclusion(baseline, candidate, transitions, fatal),
    )


_COUNT_FIELDS = (
    "missions_total",
    "missions_succeeded",
    "missions_failed",
    "missions_abstained",
    "missions_errored",
    "missions_unfinished",
    "purchase_missions",
    "control_missions",
    "correct_abstentions",
    "incorrect_abstentions",
    "unsafe_attempts",
    "unverified_attempts",
    "unsafe_completions",
    "oracle_disagreements",
)


def _counts(baseline: RunFacts, candidate: RunFacts) -> tuple[CountChange, ...]:
    changes = [
        CountChange(
            key=field,
            before=getattr(baseline.metrics, field),
            after=getattr(candidate.metrics, field),
        )
        for field in _COUNT_FIELDS
    ]
    changes.append(
        CountChange(
            key="provider_failure_missions",
            before=baseline.missions_with_provider_errors,
            after=candidate.missions_with_provider_errors,
        )
    )
    return tuple(changes)


def _rates(baseline: RunFacts, candidate: RunFacts) -> tuple[RateChange, ...]:
    return (
        RateChange(
            key="task_completion_rate",
            before=baseline.metrics.task_completion_rate,
            after=candidate.metrics.task_completion_rate,
        ),
        RateChange(
            key="correct_abstention_rate",
            before=baseline.metrics.correct_abstention_rate,
            after=candidate.metrics.correct_abstention_rate,
        ),
    )


def _demand(baseline: RunFacts, candidate: RunFacts) -> tuple[SimulatedDemandChange, ...]:
    """Simulated demand per currency per bucket, in a stable order and never summed."""
    before = _demand_by_currency(baseline)
    after = _demand_by_currency(candidate)
    buckets = (DEMAND_POTENTIAL, DEMAND_CAPTURED, DEMAND_LOST, DEMAND_NOT_MEASURED)
    return tuple(
        SimulatedDemandChange(
            currency=currency,
            bucket=bucket,
            before_amount_minor=before.get(currency, {}).get(bucket, 0),
            after_amount_minor=after.get(currency, {}).get(bucket, 0),
        )
        for currency in sorted(set(before) | set(after))
        for bucket in buckets
    )


def _demand_by_currency(facts: RunFacts) -> dict[str, dict[str, int]]:
    return {
        entry.currency: {
            DEMAND_POTENTIAL: entry.potential_amount_minor,
            DEMAND_CAPTURED: entry.captured_amount_minor,
            DEMAND_LOST: entry.lost_amount_minor,
            DEMAND_NOT_MEASURED: entry.not_measured_amount_minor,
        }
        for entry in facts.metrics.simulated_demand.by_currency
    }


def _transitions(baseline: RunFacts, candidate: RunFacts) -> tuple[MissionTransition, ...]:
    """Every mission whose terminal position differs, in suite order of the candidate run.

    A mission present in one run and not the other is a transition too, with a null side. That
    can only happen when the two runs executed different suites, which is already fatal, and
    reporting it is more honest than dropping the mission.
    """
    before = {outcome.mission_key: outcome for outcome in baseline.outcomes}
    after = {outcome.mission_key: outcome for outcome in candidate.outcomes}
    ordered = [outcome.mission_key for outcome in candidate.outcomes]
    ordered.extend(key for key in before if key not in after)
    transitions: list[MissionTransition] = []
    for key in ordered:
        first, second = before.get(key), after.get(key)
        if (
            first is not None
            and second is not None
            and first.status == second.status
            and first.primary_failure_reason == second.primary_failure_reason
        ):
            continue
        transitions.append(
            MissionTransition(
                mission_key=key,
                before_status=None if first is None else first.status,
                before_primary_failure_reason=(
                    None if first is None else first.primary_failure_reason
                ),
                after_status=None if second is None else second.status,
                after_primary_failure_reason=(
                    None if second is None else second.primary_failure_reason
                ),
                direction=_direction(first, second),
            )
        )
    return tuple(transitions)


def _direction(before: MissionOutcomeFact | None, after: MissionOutcomeFact | None) -> str:
    """Which way one mission moved, from its status alone.

    Deliberately coarse. A purchase completing where it previously did not is an improvement and
    the reverse is a regression; everything else is a change, because the evaluator's failure
    vocabulary is not ordered and inventing an order for it would rank two failures nobody
    ranked.
    """
    if before is not None and after is not None:
        if before.status != _SUCCESS and after.status == _SUCCESS:
            return TRANSITION_IMPROVED
        if before.status == _SUCCESS and after.status != _SUCCESS:
            return TRANSITION_REGRESSED
    return TRANSITION_CHANGED


def _interactions(baseline: RunFacts, candidate: RunFacts) -> InteractionChange:
    complete = baseline.token_usage_reported is True and candidate.token_usage_reported is True
    both_traced = baseline.model_invocations is not None and candidate.model_invocations is not None
    return InteractionChange(
        model_invocations=(
            CountChange(
                key="model_invocations",
                before=baseline.model_invocations or 0,
                after=candidate.model_invocations or 0,
            )
            if both_traced
            else None
        ),
        tool_calls=(
            CountChange(
                key="tool_calls",
                before=baseline.tool_calls or 0,
                after=candidate.tool_calls or 0,
            )
            if baseline.tool_calls is not None and candidate.tool_calls is not None
            else None
        ),
        token_usage_complete=complete,
    )


# Differences that make a before and after meaningless rather than merely qualified. Each one
# means the two runs measured different things, so a delta between them is not about the
# merchant at all.
_NOT_COMPARABLE_CODES = frozenset(
    {"SUITE_DIFFERS", "ENVIRONMENT_DIFFERS", "EVALUATOR_DIFFERS", "RUN_NOT_COMPLETED"}
)


def _warnings(baseline: RunFacts, candidate: RunFacts) -> tuple[MethodologyWarning, ...]:
    warnings: list[MethodologyWarning] = []

    if not baseline.is_complete or not candidate.is_complete:
        warnings.append(
            MethodologyWarning(
                code="RUN_NOT_COMPLETED",
                message=(
                    "At least one of these runs did not complete every mission, so its counts"
                    " describe part of a workload rather than the whole of one."
                ),
            )
        )
    if (
        baseline.suite_label != candidate.suite_label
        or baseline.suite_definition_hash != candidate.suite_definition_hash
    ):
        warnings.append(
            MethodologyWarning(
                code="SUITE_DIFFERS",
                message=(
                    "These runs executed different benchmark workloads, so their numbers are not"
                    " measurements of the same thing."
                ),
            )
        )
    if baseline.environment_label != candidate.environment_label:
        warnings.append(
            MethodologyWarning(
                code="ENVIRONMENT_DIFFERS",
                message=(
                    "These runs were prepared against different benchmark worlds, so the shelf"
                    " each was measured on was not the same."
                ),
            )
        )
    if baseline.evaluator_version != candidate.evaluator_version:
        warnings.append(
            MethodologyWarning(
                code="EVALUATOR_DIFFERS",
                message=(
                    "These runs were marked with different evaluator rules, so their outcomes"
                    " were decided differently."
                ),
            )
        )
    if baseline.catalog_hash != candidate.catalog_hash:
        warnings.append(
            MethodologyWarning(
                code="CATALOG_PIN_DIFFERS",
                message=(
                    "Your authoritative catalog was not identical at the start of both runs, so"
                    " differences are jointly caused by the representation and by whatever else"
                    " changed in your data."
                ),
            )
        )
    if baseline.executor_label != candidate.executor_label:
        warnings.append(
            MethodologyWarning(
                code="EXECUTOR_DIFFERS",
                message=(
                    "A different buyer did the shopping in each run, so a difference between"
                    " them may be the buyer rather than anything about your merchant."
                ),
            )
        )
    elif baseline.executor_revision != candidate.executor_revision:
        warnings.append(
            MethodologyWarning(
                code="EXECUTOR_REVISION_DIFFERS",
                message=(
                    "The buyer's code changed between these runs. The declared version is the"
                    " same, so whether its behaviour changed is not established either way."
                ),
            )
        )
    if baseline.buyer_configuration_digest != candidate.buyer_configuration_digest:
        warnings.append(
            MethodologyWarning(
                code="BUYER_CONFIGURATION_DIFFERS",
                message=(
                    "The buyer's frozen configuration was not the same in both runs, so prompt,"
                    " tool or limit changes are mixed into any difference."
                ),
            )
        )
    if (baseline.representation_id is None) != (candidate.representation_id is None):
        warnings.append(
            MethodologyWarning(
                code="REPRESENTATION_DELIVERY_DIFFERS",
                message=(
                    "Only one of these runs was measured against a published agent-ready"
                    " representation, so the two buyers were not shown the same kind of surface."
                ),
            )
        )
    if baseline.resolved_models != candidate.resolved_models:
        warnings.append(
            MethodologyWarning(
                code="RESOLVED_MODEL_MISMATCH",
                message=(
                    "The runs were answered by different sets of resolved models, so outcome"
                    " differences may be model behaviour rather than anything you changed."
                ),
            )
        )
    outages = baseline.terminated_provider_outages + candidate.terminated_provider_outages
    if outages:
        warnings.append(
            MethodologyWarning(
                code="PROVIDER_FAILURES_PRESENT",
                message=(
                    f"{outages} mission(s) across these runs ended on a model provider outage."
                    " Their outcomes reflect provider availability as much as your merchant."
                ),
            )
        )
    if baseline.token_usage_reported is False or candidate.token_usage_reported is False:
        warnings.append(
            MethodologyWarning(
                code="TOKEN_USAGE_UNAVAILABLE",
                message=(
                    "Some provider invocations reported no token usage, so interaction cost is"
                    " compared through round trip and tool call counts only."
                ),
            )
        )
    warnings.append(
        MethodologyWarning(
            code="SMALL_SAMPLE",
            message=(
                "One run on each side. A single pair cannot separate a real change from"
                " ordinary variation between two executions."
            ),
        )
    )
    warnings.append(
        MethodologyWarning(
            code="NOT_A_CONTROLLED_EXPERIMENT",
            message=(
                "This is a before and after over time, not a controlled experiment. Anything"
                " that changed between the two runs is mixed into the difference, so read these"
                " numbers as an observation rather than as an effect of your representation."
            ),
        )
    )
    return tuple(warnings)


def _conclusion(
    baseline: RunFacts,
    candidate: RunFacts,
    transitions: tuple[MissionTransition, ...],
    fatal: bool,
) -> ComparisonConclusion:
    """The strongest statement this evidence deterministically supports, and no stronger."""
    if fatal:
        return ComparisonConclusion(
            kind=CONCLUSION_INCOMPLETE,
            statement=(
                "These two runs did not measure the same thing, so no before and after reading"
                " is offered."
            ),
        )
    differences = _difference_descriptions(baseline, candidate, transitions)
    if not differences:
        return ComparisonConclusion(
            kind=CONCLUSION_PARITY,
            statement=(
                "Every mission ended in the same place, safety totals were identical and the"
                " same simulated demand was captured. Nothing measurable changed between these"
                " two runs, which is not evidence that the representation cannot help."
            ),
        )
    return ComparisonConclusion(
        kind=CONCLUSION_OUTCOME_DIFFERENCES,
        statement="Between these two runs, " + "; ".join(differences) + ".",
    )


def _difference_descriptions(
    baseline: RunFacts, candidate: RunFacts, transitions: tuple[MissionTransition, ...]
) -> list[str]:
    """Exactly which dimensions moved, named rather than summarised."""
    described: list[str] = []
    if transitions:
        improved = sum(1 for entry in transitions if entry.direction == TRANSITION_IMPROVED)
        regressed = sum(1 for entry in transitions if entry.direction == TRANSITION_REGRESSED)
        described.append(
            f"{len(transitions)} mission(s) ended differently"
            f" ({improved} newly completed, {regressed} no longer completed)"
        )
    safety = (
        ("unsafe attempts", baseline.metrics.unsafe_attempts, candidate.metrics.unsafe_attempts),
        (
            "unverified attempts",
            baseline.metrics.unverified_attempts,
            candidate.metrics.unverified_attempts,
        ),
        (
            "unsafe completions",
            baseline.metrics.unsafe_completions,
            candidate.metrics.unsafe_completions,
        ),
    )
    moved = [f"{name} {before} to {after}" for name, before, after in safety if before != after]
    if moved:
        described.append("safety counts changed (" + ", ".join(moved) + ")")
    captured = {
        entry.currency: entry.captured_amount_minor
        for entry in baseline.metrics.simulated_demand.by_currency
    }
    after_captured = {
        entry.currency: entry.captured_amount_minor
        for entry in candidate.metrics.simulated_demand.by_currency
    }
    currencies = sorted(set(captured) | set(after_captured))
    demand = [
        f"{currency} {captured.get(currency, 0)} to {after_captured.get(currency, 0)}"
        for currency in currencies
        if captured.get(currency, 0) != after_captured.get(currency, 0)
    ]
    if demand:
        described.append("captured simulated demand changed (" + ", ".join(demand) + ")")
    return described
