"""Product-facing read and write models for the merchant re-evaluation command.

Written out field by field from the launch service's frozen dataclasses, per this repository's
rule that adding a field to a domain type must never silently change an API response.

Two things are deliberately absent. There is no cost estimate, because AgentRank has no
trustworthy provider pricing data and a currency figure derived from none would be the most
confident number on the page; what is published instead is exactly what will be executed and the
bounds it runs under. And there is no progress percentage, because the only progress facts this
system has are how many missions a suite holds and how many of them have finished.
"""

import uuid
from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field

from agentrank_api.benchmark.launch import ReevaluationDetail, ReevaluationPlan
from agentrank_api.benchmark.reevaluation import (
    REQUEST_KEY_PATTERN,
    BuyerProfile,
    ReevaluationStatus,
)
from agentrank_api.diagnostics.comparison import RunComparison


class LaunchBlockerView(BaseModel):
    """One reason a launch is refused right now, with a code and a merchant sentence."""

    code: str
    message: str


class ReevaluationPreflightView(BaseModel):
    """What a re-evaluation would evaluate, before the merchant commits to spending one."""

    launchable: bool
    representation_id: uuid.UUID | None
    representation_label: str | None
    compiler_run_id: uuid.UUID | None
    source_snapshot_id: uuid.UUID | None
    suite_id: uuid.UUID | None
    suite_label: str | None
    suite_definition_hash: str | None
    mission_count: int | None
    environment_id: uuid.UUID | None
    environment_label: str | None
    buyer_profile: BuyerProfile
    executor_kind: str
    provider: str | None
    requested_model: str | None
    max_model_turns: int | None
    max_tool_calls: int | None
    mission_deadline_seconds: float | None
    baseline_run_id: uuid.UUID | None
    baseline_run_completed_at: datetime | None
    pending_reevaluation_id: uuid.UUID | None
    blockers: list[LaunchBlockerView]

    @classmethod
    def from_domain(cls, plan: ReevaluationPlan) -> Self:
        return cls(
            launchable=plan.launchable,
            representation_id=plan.representation_id,
            representation_label=plan.representation_label,
            compiler_run_id=plan.compiler_run_id,
            source_snapshot_id=plan.source_snapshot_id,
            suite_id=plan.suite_id,
            suite_label=plan.suite_label,
            suite_definition_hash=plan.suite_definition_hash,
            mission_count=plan.mission_count,
            environment_id=plan.environment_id,
            environment_label=plan.environment_label,
            buyer_profile=plan.buyer_profile,
            executor_kind=plan.executor_kind,
            provider=plan.provider,
            requested_model=plan.requested_model,
            max_model_turns=plan.max_model_turns,
            max_tool_calls=plan.max_tool_calls,
            mission_deadline_seconds=plan.mission_deadline_seconds,
            baseline_run_id=plan.baseline_run_id,
            baseline_run_completed_at=plan.baseline_run_completed_at,
            pending_reevaluation_id=plan.pending_reevaluation_id,
            blockers=[
                LaunchBlockerView(code=blocker.code, message=blocker.message)
                for blocker in plan.blockers
            ],
        )


class ReevaluationRequest(BaseModel):
    """The whole of what a browser may say about a launch.

    A representation identifier, so a page rendered against an artifact that has since been
    superseded is refused rather than quietly running something else, and a request key, so a
    double submit and a retry after a lost response are the same launch. Which merchant this is
    comes from the credential and is not a field here or anywhere else.

    `extra="forbid"` so a body carrying `merchant_id`, a suite, a buyer configuration or an
    execution limit is refused rather than ignored. A field this schema does not have is a field
    a caller believed would take effect, and silently dropping one is how a request comes to mean
    something the caller did not intend.
    """

    model_config = ConfigDict(extra="forbid")

    representation_id: uuid.UUID
    request_key: str = Field(pattern=REQUEST_KEY_PATTERN)


class ReevaluationView(BaseModel):
    """One launch: what it froze, where it has got to, and what it will be compared with."""

    reevaluation_id: uuid.UUID
    status: ReevaluationStatus
    failure_code: str | None
    requested_at: datetime
    started_at: datetime | None
    settled_at: datetime | None
    representation_id: uuid.UUID
    representation_label: str
    compiler_run_id: uuid.UUID
    suite_id: uuid.UUID
    suite_label: str
    mission_count: int
    environment_label: str
    buyer_profile: BuyerProfile
    executor_kind: str
    provider: str | None
    requested_model: str | None
    buyer_configuration_digest: str | None
    run_id: uuid.UUID | None
    run_status: str | None
    missions_completed: int | None
    baseline_run_id: uuid.UUID | None

    @classmethod
    def from_domain(cls, detail: ReevaluationDetail) -> Self:
        return cls(
            reevaluation_id=detail.reevaluation_id,
            status=detail.status,
            failure_code=detail.failure_code,
            requested_at=detail.requested_at,
            started_at=detail.started_at,
            settled_at=detail.settled_at,
            representation_id=detail.representation_id,
            representation_label=detail.representation_label,
            compiler_run_id=detail.compiler_run_id,
            suite_id=detail.suite_id,
            suite_label=detail.suite_label,
            mission_count=detail.mission_count,
            environment_label=detail.environment_label,
            buyer_profile=detail.buyer_profile,
            executor_kind=detail.executor_kind,
            provider=detail.provider,
            requested_model=detail.requested_model,
            buyer_configuration_digest=detail.buyer_configuration_digest,
            run_id=detail.run_id,
            run_status=detail.run_status,
            missions_completed=detail.missions_completed,
            baseline_run_id=detail.baseline_run_id,
        )


class CountChangeView(BaseModel):
    """One count before and after, with the difference stated rather than left to arithmetic."""

    key: str
    before: int
    after: int
    delta: int


class RateChangeView(BaseModel):
    """One rate before and after. Null is an empty denominator and is never rendered as zero."""

    key: str
    before: float | None
    after: float | None
    delta: float | None


class SimulatedDemandChangeView(BaseModel):
    """Simulated demand in one bucket of one currency. Currencies are never summed."""

    currency: str
    bucket: str
    simulated_before_amount_minor: int
    simulated_after_amount_minor: int
    simulated_delta_amount_minor: int


class MissionTransitionView(BaseModel):
    """One mission that ended somewhere different in the two runs."""

    mission_key: str
    before_status: str | None
    before_primary_failure_reason: str | None
    after_status: str | None
    after_primary_failure_reason: str | None
    direction: str


class InteractionChangeView(BaseModel):
    """Observed interaction cost, where it was observed at all.

    Counts are published only when both runs recorded a trace, and which side did is carried
    separately, because "neither run asked a model" and "only the later run did" are different
    facts and reporting both as no data would state something that did not happen.

    `token_usage_complete` has three states. True when every provider invocation on both sides
    reported its usage, false when at least one did not so no honest token total exists, and
    null when at least one run recorded no provider invocation at all.
    """

    model_invocations: CountChangeView | None
    tool_calls: CountChangeView | None
    baseline_traced: bool
    candidate_traced: bool
    token_usage_complete: bool | None


class MethodologyWarningView(BaseModel):
    code: str
    message: str


class ComparisonConclusionView(BaseModel):
    kind: str
    statement: str


class RunComparisonView(BaseModel):
    """One before and after reading, with everything that qualifies it attached.

    `comparable` false means the two runs did not measure the same thing. The deltas are still
    on the wire, and the console deliberately does not render them: what a reader needs then is
    which pin differed, and a table of numbers beside that reads as a comparison anyway.
    """

    engine_identity: str
    baseline_run_id: uuid.UUID
    candidate_run_id: uuid.UUID
    comparable: bool
    counts: list[CountChangeView]
    rates: list[RateChangeView]
    simulated_demand: list[SimulatedDemandChangeView]
    transitions: list[MissionTransitionView]
    interactions: InteractionChangeView
    baseline_runtime_seconds: float | None
    candidate_runtime_seconds: float | None
    warnings: list[MethodologyWarningView]
    conclusion: ComparisonConclusionView

    @classmethod
    def from_domain(cls, comparison: RunComparison) -> Self:
        before, after = comparison.runtime_seconds
        interactions = comparison.interactions
        return cls(
            engine_identity=comparison.engine_identity,
            baseline_run_id=comparison.baseline_run_id,
            candidate_run_id=comparison.candidate_run_id,
            comparable=comparison.comparable,
            counts=[
                CountChangeView(
                    key=change.key, before=change.before, after=change.after, delta=change.delta
                )
                for change in comparison.counts
            ],
            rates=[
                RateChangeView(
                    key=change.key, before=change.before, after=change.after, delta=change.delta
                )
                for change in comparison.rates
            ],
            simulated_demand=[
                SimulatedDemandChangeView(
                    currency=change.currency,
                    bucket=change.bucket,
                    simulated_before_amount_minor=change.before_amount_minor,
                    simulated_after_amount_minor=change.after_amount_minor,
                    simulated_delta_amount_minor=change.delta_amount_minor,
                )
                for change in comparison.simulated_demand
            ],
            transitions=[
                MissionTransitionView(
                    mission_key=transition.mission_key,
                    before_status=transition.before_status,
                    before_primary_failure_reason=transition.before_primary_failure_reason,
                    after_status=transition.after_status,
                    after_primary_failure_reason=transition.after_primary_failure_reason,
                    direction=transition.direction,
                )
                for transition in comparison.transitions
            ],
            interactions=InteractionChangeView(
                model_invocations=(
                    None
                    if interactions.model_invocations is None
                    else CountChangeView(
                        key=interactions.model_invocations.key,
                        before=interactions.model_invocations.before,
                        after=interactions.model_invocations.after,
                        delta=interactions.model_invocations.delta,
                    )
                ),
                tool_calls=(
                    None
                    if interactions.tool_calls is None
                    else CountChangeView(
                        key=interactions.tool_calls.key,
                        before=interactions.tool_calls.before,
                        after=interactions.tool_calls.after,
                        delta=interactions.tool_calls.delta,
                    )
                ),
                baseline_traced=interactions.baseline_traced,
                candidate_traced=interactions.candidate_traced,
                token_usage_complete=interactions.token_usage_complete,
            ),
            baseline_runtime_seconds=before,
            candidate_runtime_seconds=after,
            warnings=[
                MethodologyWarningView(code=warning.code, message=warning.message)
                for warning in comparison.warnings
            ],
            conclusion=ComparisonConclusionView(
                kind=comparison.conclusion.kind, statement=comparison.conclusion.statement
            ),
        )


class ReevaluationDetailView(ReevaluationView):
    """One launch with its comparison, when there is one to give.

    `comparison` is null while nothing has been measured against a prior run yet: no run, no
    prior run of the same suite, or a run that has not finished. Null is not an empty comparison
    and must never be rendered as "nothing changed".
    """

    comparison: RunComparisonView | None

    @classmethod
    def with_comparison(
        cls, detail: ReevaluationDetail, comparison: RunComparison | None
    ) -> ReevaluationDetailView:
        return cls(
            **ReevaluationView.from_domain(detail).model_dump(),
            comparison=None if comparison is None else RunComparisonView.from_domain(comparison),
        )
