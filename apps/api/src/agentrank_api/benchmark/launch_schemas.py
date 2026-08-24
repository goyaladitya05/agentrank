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
