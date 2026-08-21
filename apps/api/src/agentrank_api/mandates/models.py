"""Spending mandate persistence.

A spending mandate is the authoritative financial authorization boundary. It is the row
that future execution code reads to answer "is this checkout permitted", using nothing
but the mandate, the checkout and the current time. No model, no prose, no intent.

Three properties are enforced by the database rather than by application code, because
the database is the only layer that cannot be bypassed:

- an amount is a non negative integer count of minor units and its currency is never
  absent
- a validity window is ordered, so a mandate that expires before it begins cannot exist
- the authorization fields are immutable once written, and revocation is terminal

The last one is a trigger rather than a constraint, because it is a rule about a
transition rather than about a row. See the migration for the statement itself.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from agentrank_api.models import Base
from agentrank_api.money import CURRENCY_PATTERN


class MandateStatus(StrEnum):
    """The states a mandate can be in.

    Deliberately two. Expiry is derived from `valid_until` and the current time rather
    than written into this column, so there is no background job whose failure would
    leave an expired mandate looking usable.
    """

    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


# Stored as text rather than as a native PostgreSQL enum. The set is small but not
# settled, and adding a value to a native enum is an ALTER TYPE rather than an ordinary
# constraint change.
#
# The type does not create its own check constraint. One is declared on the table below
# instead, so the constraint has a name this repository chose, appears in the migration
# verbatim, and is visible where every other constraint on this table is.
MANDATE_STATUS = Enum(
    MandateStatus,
    native_enum=False,
    create_constraint=False,
    validate_strings=True,
    length=16,
    name="mandate_status",
)

_STATUS_VALUES = ", ".join(f"'{status.value}'" for status in MandateStatus)


class SpendingMandate(Base):
    """What a buyer is permitted to spend with one merchant, and until when.

    There is no `updated_at`. Every field except `status` and `revoked_at` is immutable,
    and the one transition that exists stamps its own timestamp, so a general purpose
    modification time would only be a second name for `revoked_at`.

    `max_quantity` is nullable, and null means this mandate places no limit on quantity.
    It does not mean zero and it does not mean one.

    Changing an authorization means creating a new mandate. That is deliberate: a new row
    leaves the old one intact and auditable, while an edit would rewrite history.
    """

    __tablename__ = "spending_mandate"
    __table_args__ = (
        # Redundant against the primary key, and present only as a composite foreign key
        # target: a checkout is bound through (mandate_id, merchant_id), so it cannot name
        # a mandate granted to a different merchant.
        UniqueConstraint("id", "merchant_id"),
        # Merchant scoped reads and the RESTRICT check when a merchant is deleted. The
        # unique constraint above has id leftmost, so it does not serve either.
        Index(None, "merchant_id"),
        CheckConstraint(f"status IN ({_STATUS_VALUES})", name="status_known"),
        CheckConstraint("max_total_amount_minor >= 0", name="amount_not_negative"),
        CheckConstraint(f"currency ~ '{CURRENCY_PATTERN}'", name="currency_format"),
        CheckConstraint("max_quantity IS NULL OR max_quantity > 0", name="quantity_positive"),
        CheckConstraint("valid_until > valid_from", name="validity_window_ordered"),
        # Status and revoked_at are two views of one fact, so they cannot disagree.
        CheckConstraint(
            "(status = 'REVOKED') = (revoked_at IS NOT NULL)",
            name="revoked_at_matches_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid7)
    # RESTRICT, not CASCADE. A financial authorization is not catalog data: it must not
    # disappear as a side effect of removing the merchant it was granted against.
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("merchant.id", ondelete="RESTRICT"),
        nullable=False,
    )
    max_total_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    max_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # No server default. An insert that does not state a status is a bug, and failing is
    # better than defaulting a row that authorizes spending into existence as active.
    status: Mapped[MandateStatus] = mapped_column(MANDATE_STATUS, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
