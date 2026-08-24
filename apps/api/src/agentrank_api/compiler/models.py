"""Durable, append-only compiler evidence and review history."""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from agentrank_api.benchmark.identity import HASH_LENGTH, HASH_PATTERN
from agentrank_api.models import Base


class CompilerRunStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class CandidateState(StrEnum):
    ACCEPTED = "ACCEPTED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    REJECTED = "REJECTED"


class ReviewDecision(StrEnum):
    ACCEPT = "ACCEPT"
    CORRECT = "CORRECT"
    REJECT = "REJECT"


RUN_STATUS = Enum(CompilerRunStatus, native_enum=False, create_constraint=False, length=16)
CANDIDATE_STATE = Enum(CandidateState, native_enum=False, create_constraint=False, length=16)
REVIEW_DECISION = Enum(ReviewDecision, native_enum=False, create_constraint=False, length=16)


class CompilerRun(Base):
    __tablename__ = "compiler_run"
    __table_args__ = (
        UniqueConstraint("id", "merchant_id", name="uq_compiler_run_binding"),
        UniqueConstraint(
            "source_snapshot_id", "configuration_digest", name="uq_compiler_run_input"
        ),
        ForeignKeyConstraint(["merchant_id"], ["merchant.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["source_snapshot_id", "merchant_id"],
            ["merchant_source_snapshot.id", "merchant_source_snapshot.merchant_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"configuration_digest ~ '{HASH_PATTERN}'", name="configuration_digest_format"
        ),
        CheckConstraint("jsonb_typeof(configuration) = 'object'", name="configuration_object"),
        CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED')", name="status_known"
        ),
        Index(None, "merchant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid7)
    merchant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    source_snapshot_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    configuration_digest: Mapped[str] = mapped_column(String(HASH_LENGTH), nullable=False)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[CompilerRunStatus] = mapped_column(RUN_STATUS, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_representation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CompilerCandidate(Base):
    __tablename__ = "compiler_candidate"
    __table_args__ = (
        UniqueConstraint("id", "merchant_id", name="uq_compiler_candidate_binding"),
        UniqueConstraint("run_id", "target", name="uq_compiler_candidate_target"),
        ForeignKeyConstraint(
            ["run_id", "merchant_id"],
            ["compiler_run.id", "compiler_run.merchant_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("jsonb_typeof(proposal) = 'object'", name="proposal_object"),
        CheckConstraint("state IN ('ACCEPTED', 'REVIEW_REQUIRED', 'REJECTED')", name="state_known"),
        Index(None, "merchant_id"),
        Index(None, "run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid7)
    merchant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    target: Mapped[str] = mapped_column(String(256), nullable=False)
    proposal: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    state: Mapped[CandidateState] = mapped_column(CANDIDATE_STATE, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CompilerReview(Base):
    __tablename__ = "compiler_review"
    __table_args__ = (
        UniqueConstraint("candidate_id", name="uq_compiler_review_candidate"),
        ForeignKeyConstraint(
            ["candidate_id", "merchant_id"],
            ["compiler_candidate.id", "compiler_candidate.merchant_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["run_id", "merchant_id"],
            ["compiler_run.id", "compiler_run.merchant_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("decision IN ('ACCEPT', 'CORRECT', 'REJECT')", name="decision_known"),
        CheckConstraint(
            "correction IS NULL OR jsonb_typeof(correction) = 'object'", name="correction_object"
        ),
        CheckConstraint(
            "(decision = 'CORRECT') = (correction IS NOT NULL)", name="correction_matches_decision"
        ),
        Index(None, "merchant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid7)
    merchant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    decision: Mapped[ReviewDecision] = mapped_column(REVIEW_DECISION, nullable=False)
    # `none_as_null` because an accept or a reject carries no correction, and the check
    # constraint below reads SQL NULL. Without it SQLAlchemy writes a JSONB `null`, which is a
    # value rather than the absence of one, and every non-correcting review is refused.
    correction: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    reviewer: Mapped[str] = mapped_column(String(128), nullable=False, default="SYSTEM")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
