"""Immutable persistence for merchant source snapshots and Commerce IR."""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
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


class SourceOrigin(StrEnum):
    """Which mechanism supplied one piece of merchant source evidence.

    Two members, and the second one earns its place by being a genuinely different answer to
    "where did this come from" rather than a second name for the same act. A console submission
    is a merchant writing their catalog down. An import is a merchant naming their own public
    pages, AgentRank retrieving them and extracting a draft deterministically, and the merchant
    confirming what came out. The evidence behind an imported snapshot is a set of URLs and
    digests that `merchant_source_import` holds; the evidence behind a console one is the
    merchant.

    Both produce ordinary snapshots through one intake. This column says which mechanism, and
    nothing downstream reads it to behave differently.
    """

    MERCHANT_CONSOLE = "MERCHANT_CONSOLE"
    MERCHANT_IMPORT = "MERCHANT_IMPORT"


ORIGIN = Enum(
    SourceOrigin,
    native_enum=False,
    create_constraint=False,
    validate_strings=True,
    length=32,
    name="merchant_source_origin",
)

# One key per rendered console form. Bounded and character restricted because it arrives from a
# browser and is stored; the same shape a benchmark launch key has, spelled out here rather than
# imported so that merchant source evidence keeps no dependency on the benchmark package.
SUBMISSION_KEY_PATTERN = r"^[0-9a-zA-Z_-]{8,64}$"
MAX_SUBMISSION_KEY_LENGTH = 64

_ORIGIN_VALUES = ", ".join(f"'{origin.value}'" for origin in SourceOrigin)


def write_order_column() -> Mapped[int]:
    """The order PostgreSQL wrote this row in, and the only authority on which one is newest.

    `GENERATED ALWAYS AS IDENTITY` rather than a default, because "always" is the half that
    matters: no INSERT anywhere can supply a value, so nothing in this application can decide
    that one row was written before another. The database decides, at the moment the INSERT
    reaches it, for every writer at once.

    That is what the primary key used to be doing and could not. A version 7 UUID is generated
    in Python and is monotonic only within one process, so two API processes inserting in the
    same millisecond order themselves by a random draw. `created_at` cannot do it either: it is
    `transaction_timestamp()`, so a transaction that began first and committed second carries
    the earlier timestamp, which is the exact shape two submissions serializing on the
    per-merchant advisory lock have.

    Unique rather than merely indexed. A tie here would be a tie in the answer to which snapshot
    a merchant is publishing, and there is no sensible way to break one.
    """
    return mapped_column(BigInteger, Identity(always=True), nullable=False, unique=True)


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
    write_order: Mapped[int] = write_order_column()
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
    write_order: Mapped[int] = write_order_column()
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


class MerchantSourceSubmission(Base):
    """One merchant command that supplied source evidence, and the snapshot it resolved to.

    A snapshot is what the merchant said. This is the command that said it, and the two are
    separate rows because they are not the same count. Submitting evidence identical to the
    merchant's current snapshot resolves to that snapshot rather than writing a second copy of
    it, so many submissions can name one snapshot, and `created_snapshot` records which single
    command actually produced it.

    The row exists for three things a snapshot cannot carry:

    ```text
    request identity   one key per merchant, so a double submit is one submission
    origin             which mechanism supplied the evidence
    outcome            whether this command created a snapshot or matched the current one
    ```

    `request_key` is what makes a lost response answerable. The console generates one per
    rendered form, so pressing submit twice and retrying after a response nobody saw are the
    same command, and opening the form again is a deliberate second one.

    A snapshot with no submission at all was published by the operator command line, which is
    how every snapshot before this table came to exist. That absence is read as its origin
    rather than backfilled with a guess.
    """

    __tablename__ = "merchant_source_submission"
    __table_args__ = (
        # The idempotency key. A repeated or concurrent submit with the same key is the same
        # submission and resolves to the same snapshot.
        UniqueConstraint(
            "merchant_id", "request_key", name="uq_merchant_source_submission_request"
        ),
        ForeignKeyConstraint(["merchant_id"], ["merchant.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["source_snapshot_id", "merchant_id"],
            ["merchant_source_snapshot.id", "merchant_source_snapshot.merchant_id"],
            name="fk_merchant_source_submission_snapshot",
            ondelete="RESTRICT",
        ),
        CheckConstraint(f"request_key ~ '{SUBMISSION_KEY_PATTERN}'", name="request_key_format"),
        CheckConstraint(f"origin IN ({_ORIGIN_VALUES})", name="origin_known"),
        # One snapshot is created by one command. Many submissions may name a snapshot, and
        # exactly one of them may claim to have written it.
        Index(
            "uq_merchant_source_submission_creator",
            "source_snapshot_id",
            unique=True,
            postgresql_where=text("created_snapshot"),
        ),
        Index(None, "merchant_id"),
        Index(None, "source_snapshot_id", "merchant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid7)
    merchant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    request_key: Mapped[str] = mapped_column(String(MAX_SUBMISSION_KEY_LENGTH), nullable=False)
    source_snapshot_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    origin: Mapped[SourceOrigin] = mapped_column(ORIGIN, nullable=False)
    created_snapshot: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
