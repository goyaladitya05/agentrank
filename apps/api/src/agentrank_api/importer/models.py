"""One record per merchant import attempt, and deliberately nothing else.

This table is not a second source truth. The immutable `merchant_source_snapshot` remains the only
place a merchant's catalog is stated, and the only way a row here reaches it is the merchant
confirming, which goes through the ordinary source intake and writes an ordinary snapshot. What
this holds is the acquisition: which URLs were fetched, what each one answered, what was extracted
from them, and what was left out and why.

It exists because the alternative does not work. An import fetches several pages, takes seconds,
and must be inspected before it becomes history, so the extracted draft has to survive between two
requests. Handing it to the browser and taking it back would make the merchant's own browser the
custodian of the provenance, and provenance a client can edit proves nothing about where a fact
came from.

What it deliberately does not hold is the pages. A bounded record of each retrieval is kept, which
is its URL, its status, its size and the digest of what arrived, and the merchant's own extracted
text is kept because that is the evidence. The markup is not: storing whole storefront pages would
be this application accumulating a copy of somebody else's website, indefinitely, for a provenance
question that a digest already answers.

Immutable in the sense that matters. Nothing rewrites the pages or the draft after the import
finishes. The one field that changes is the link to the snapshot the merchant confirmed it into,
which goes from absent to set exactly once and is what `confirmed_at` records.
"""

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
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from agentrank_api.models import Base
from agentrank_api.representation.models import MAX_SUBMISSION_KEY_LENGTH, SUBMISSION_KEY_PATTERN

# `scheme://host:port`, which is the whole of what makes two URLs one storefront.
MAX_ORIGIN_LENGTH = 260


class ImportState(StrEnum):
    """How one import attempt ended.

    Two members, because an import runs inside one request and either finishes or does not. There
    is no queued or running state to represent: nothing writes a row until the fetching is over,
    so a row that exists is a row whose work is done.

    `FAILED` is for an import that produced no draft at all, such as one that reached its overall
    deadline. An import where every page answered and nothing could be extracted from any of them
    is `COMPLETED` with an empty draft and a list of reasons, because that is a different fact and
    the merchant can act on it.
    """

    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


_STATE_VALUES = ", ".join(f"'{state.value}'" for state in ImportState)

STATE = Enum(
    ImportState,
    native_enum=False,
    create_constraint=False,
    validate_strings=True,
    length=16,
    name="merchant_source_import_state",
)


class MerchantSourceImport(Base):
    """One attempt to turn a merchant's own public pages into a source draft."""

    __tablename__ = "merchant_source_import"
    __table_args__ = (
        # The idempotency key. A double submit of the import form, or a retry after a response
        # nobody saw, is one import and fetches the merchant's pages once.
        UniqueConstraint("merchant_id", "request_key", name="uq_merchant_source_import_request"),
        UniqueConstraint("id", "merchant_id", name="uq_merchant_source_import_binding"),
        ForeignKeyConstraint(["merchant_id"], ["merchant.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["source_snapshot_id", "merchant_id"],
            ["merchant_source_snapshot.id", "merchant_source_snapshot.merchant_id"],
            name="fk_merchant_source_import_snapshot",
            ondelete="RESTRICT",
        ),
        CheckConstraint(f"request_key ~ '{SUBMISSION_KEY_PATTERN}'", name="request_key_format"),
        CheckConstraint(f"state IN ({_STATE_VALUES})", name="state_known"),
        CheckConstraint("jsonb_typeof(pages) = 'array'", name="pages_array"),
        CheckConstraint("jsonb_typeof(draft) = 'object'", name="draft_object"),
        # Confirmation is one fact with three halves, and a row carrying some of them would be a
        # row nobody could read. A snapshot with no time and a time with no snapshot are both
        # states this workflow has no meaning for.
        CheckConstraint(
            "(source_snapshot_id IS NULL) = (confirmed_at IS NULL)",
            name="confirmation_complete",
        ),
        CheckConstraint(
            "confirmed_at IS NOT NULL OR stock_level IS NULL",
            name="stock_level_needs_confirmation",
        ),
        CheckConstraint("stock_level IS NULL OR stock_level >= 0", name="stock_level_not_negative"),
        # A failed import produced no draft and cannot have been confirmed into anything.
        CheckConstraint(
            "state <> 'FAILED' OR source_snapshot_id IS NULL",
            name="failed_import_is_not_confirmed",
        ),
        Index(None, "merchant_id"),
        Index(None, "source_snapshot_id", "merchant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid7)
    merchant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    request_key: Mapped[str] = mapped_column(String(MAX_SUBMISSION_KEY_LENGTH), nullable=False)
    origin: Mapped[str] = mapped_column(String(MAX_ORIGIN_LENGTH), nullable=False)
    state: Mapped[ImportState] = mapped_column(STATE, nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pages: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    draft: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    source_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    stock_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
