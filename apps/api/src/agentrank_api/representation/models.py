"""Immutable persistence for merchant source snapshots and Commerce IR."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from agentrank_api.benchmark.definitions import KEY_PATTERN, MAX_KEY_LENGTH
from agentrank_api.benchmark.identity import HASH_LENGTH, HASH_PATTERN
from agentrank_api.models import Base
from agentrank_api.representation.definitions import RepresentationProducer

PRODUCER = Enum(
    RepresentationProducer,
    native_enum=False,
    create_constraint=False,
    validate_strings=True,
    length=24,
    name="representation_producer",
)


class MerchantSourceSnapshot(Base):
    """One published raw merchant input, written once and never reinterpreted."""

    __tablename__ = "merchant_source_snapshot"
    __table_args__ = (
        UniqueConstraint("merchant_id", "source_key", "source_version", name="uq_source_version"),
        UniqueConstraint("id", "merchant_id", name="uq_source_snapshot_binding"),
        ForeignKeyConstraint(["merchant_id"], ["merchant.id"], ondelete="RESTRICT"),
        CheckConstraint(f"source_key ~ '{KEY_PATTERN}'", name="source_key_format"),
        CheckConstraint("source_version > 0", name="source_version_positive"),
        CheckConstraint(f"content_hash ~ '{HASH_PATTERN}'", name="content_hash_format"),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="payload_object"),
        Index(None, "merchant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid7)
    merchant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    source_key: Mapped[str] = mapped_column(String(MAX_KEY_LENGTH), nullable=False)
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(HASH_LENGTH), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    @property
    def label(self) -> str:
        return f"{self.source_key}@{self.source_version}"


class CommerceRepresentation(Base):
    """One published Commerce IR document derived from one source snapshot."""

    __tablename__ = "commerce_representation"
    __table_args__ = (
        UniqueConstraint("id", "merchant_id", name="uq_commerce_representation_binding"),
        UniqueConstraint(
            "source_snapshot_id", "producer", "producer_version", name="uq_representation_producer"
        ),
        ForeignKeyConstraint(
            ["source_snapshot_id", "merchant_id"],
            ["merchant_source_snapshot.id", "merchant_source_snapshot.merchant_id"],
            name="fk_commerce_representation_source",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["compiler_run_id", "merchant_id"],
            ["compiler_run.id", "compiler_run.merchant_id"],
            name="fk_commerce_representation_compiler_run",
            ondelete="RESTRICT",
        ),
        CheckConstraint(f"content_hash ~ '{HASH_PATTERN}'", name="content_hash_format"),
        CheckConstraint("producer IN ('MANUAL_FIXTURE', 'COMPILER')", name="producer_known"),
        CheckConstraint("length(btrim(producer_version)) > 0", name="producer_version_not_blank"),
        CheckConstraint(
            "(producer = 'COMPILER') = (compiler_run_id IS NOT NULL)",
            name="compiler_representation_run_binding",
        ),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="payload_object"),
        Index(None, "merchant_id"),
        Index(None, "source_snapshot_id", "merchant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid7)
    merchant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    source_snapshot_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    compiler_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    producer: Mapped[RepresentationProducer] = mapped_column(PRODUCER, nullable=False)
    producer_version: Mapped[str] = mapped_column(String(128), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(HASH_LENGTH), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    @property
    def label(self) -> str:
        return f"{self.producer.value.lower()}:{self.producer_version}:{self.content_hash}"
