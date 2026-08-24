"""Product-shaped merchant compiler review contracts."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class CompilerEvidenceView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    excerpt: str | None


class CompilerReviewHistoryView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_id: uuid.UUID
    decision: Literal["ACCEPT", "CORRECT", "REJECT"]
    correction: dict[str, Any] | None
    reviewer: str
    created_at: datetime


class CompilerCandidateView(BaseModel):
    """One proposed fact, already read for a merchant rather than left as compiler internals.

    `proposed_value`, `authority` and `confidence` are lifted out of `proposal` here so that the
    console never parses a compiler payload to decide what to show. `proposal` stays, whole and
    unedited, because the merchant is entitled to see the exact document the compiler wrote.
    """

    model_config = ConfigDict(extra="forbid")

    candidate_id: uuid.UUID
    target: str
    product_or_variant: str
    attribute: str
    proposal: dict[str, Any]
    proposed_value: Any
    authority: Literal["AUTHORITATIVE", "DERIVED"]
    confidence: Literal["AUTHORITATIVE", "HIGH", "REVIEW_REQUIRED"]
    attribute_kind: str | None
    unit: str | None
    state: str
    requires_correction: bool
    evidence: list[CompilerEvidenceView]
    review: CompilerReviewHistoryView | None


class PublishReadinessView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    publishable: bool
    blockers: list[str]
    published_representation_id: uuid.UUID | None


class CompilerRunReviewView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: uuid.UUID
    source_snapshot_id: uuid.UUID
    source_label: str
    configuration_digest: str
    status: str
    created_at: datetime
    completed_at: datetime | None
    candidates: list[CompilerCandidateView]
    readiness: PublishReadinessView


class CompilerRunSummaryView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: uuid.UUID
    source_snapshot_id: uuid.UUID
    source_label: str
    status: str
    created_at: datetime
    review_required_count: int
    reviewed_count: int
    published_representation_id: uuid.UUID | None


class CompilerOverviewView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_representation_id: uuid.UUID | None
    review_required_count: int
    runs: list[CompilerRunSummaryView]


class CorrectCandidateRequest(BaseModel):
    """Only a typed value and an existing source citation may be corrected by a browser."""

    model_config = ConfigDict(extra="forbid")

    value: str | int | bool
    provenance_field: str = Field(min_length=1, max_length=256)
    provenance_excerpt: str | None = Field(default=None, max_length=500)


class StartCompilerRunRequest(BaseModel):
    """The whole of what a browser may say about starting a compiler run.

    One snapshot identifier, checked against the merchant its credential authenticated. There is
    no configuration field, because this build has exactly one compiler configuration and a
    browser that could name one could ask for a reading nobody can reproduce. There is no request
    key either: a run is unique on its source snapshot and its configuration digest already, so a
    repeat resolves to the run that exists rather than to a second reading of one document.
    """

    model_config = ConfigDict(extra="forbid")

    source_snapshot_id: uuid.UUID
