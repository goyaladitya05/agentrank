"""Checkout persistence.

A checkout session is a merchant quote prepared for a possible purchase. It is not a
payment and it holds no payment fields. What it does hold is enough immutable detail for
a later decision to be reproducible: the prices that were quoted, what they add up to,
which mandate the quote was prepared against, and when the quote stops being good.

Five properties are enforced by the database rather than by application code, because the
database is the only layer that cannot be bypassed:

- money is a non negative integer count of minor units, its currency is never absent, and
  `total = subtotal + shipping - discount` holds on every row
- the semantic snapshot on a line is a JSON object, so the evaluator that reads it can rely
  on a shape rather than testing what arrived
- a checkout, its mandate, its lines and their variants all belong to one merchant, and
  every line is priced in the checkout's own currency, all through composite foreign keys
- a quote expires, and it cannot be created already expired
- the quote fields are immutable once written, and cancellation is terminal

The last one is a trigger rather than a constraint, because it is a rule about a
transition rather than about a row. See the migration for the statement itself.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from agentrank_api.models import Base
from agentrank_api.money import CURRENCY_PATTERN


class CheckoutStatus(StrEnum):
    """The states a checkout can be in.

    Deliberately two. Expiry is derived by comparing `expires_at` with the current time
    rather than written into this column, exactly as a mandate's expiry is, so no
    background job exists whose failure would leave an expired quote looking usable.

    `PAID`, `FAILED` and `COMPLETED` are absent because payment does not exist. A status
    value naming a state the system cannot reach would be a promise, not a record.
    """

    OPEN = "OPEN"
    CANCELLED = "CANCELLED"


# Stored as text with a check constraint rather than as a native PostgreSQL enum, for the
# same reason as the mandate status: this set will grow when payment execution arrives,
# and adding a value should be an ordinary constraint change rather than ALTER TYPE.
CHECKOUT_STATUS = Enum(
    CheckoutStatus,
    native_enum=False,
    create_constraint=False,
    validate_strings=True,
    length=16,
    name="checkout_status",
)

_STATUS_VALUES = ", ".join(f"'{status.value}'" for status in CheckoutStatus)


class CheckoutSession(Base):
    """One merchant quote, fixed at the moment it was made.

    There is no `updated_at`. Every field except `status` and `cancelled_at` is immutable,
    and the one transition that exists stamps its own timestamp, so a general purpose
    modification time would only be a second name for `cancelled_at`.

    There is no relationship to the mandate either. The composite foreign key ties the two
    together structurally, and authorization loads the mandate deliberately rather than
    getting it for free as a side effect of reading a checkout.
    """

    __tablename__ = "checkout_session"
    __table_args__ = (
        # The mandate is reached through (id, merchant_id), so a checkout cannot be bound
        # to a mandate granted to a different merchant. Merchant integrity is transitive
        # through it: spending_mandate.merchant_id already references merchant.
        #
        # RESTRICT, not CASCADE. A quote is financial history, not catalog data.
        ForeignKeyConstraint(
            ["mandate_id", "merchant_id"],
            ["spending_mandate.id", "spending_mandate.merchant_id"],
            ondelete="RESTRICT",
        ),
        # Redundant against the primary key, and present only as a composite foreign key
        # target. Carrying the currency into it is what lets a line be tied to both this
        # checkout's merchant and this checkout's currency in one constraint.
        UniqueConstraint("id", "merchant_id", "currency"),
        CheckConstraint(f"status IN ({_STATUS_VALUES})", name="status_known"),
        CheckConstraint(f"currency ~ '{CURRENCY_PATTERN}'", name="currency_format"),
        CheckConstraint("subtotal_amount_minor >= 0", name="subtotal_not_negative"),
        CheckConstraint("shipping_amount_minor >= 0", name="shipping_not_negative"),
        CheckConstraint("discount_amount_minor >= 0", name="discount_not_negative"),
        CheckConstraint("total_amount_minor >= 0", name="total_not_negative"),
        CheckConstraint(
            "total_amount_minor = subtotal_amount_minor + shipping_amount_minor"
            " - discount_amount_minor",
            name="total_matches_parts",
        ),
        # A quote that has already expired is not a quote. The comparison is against the
        # row's own creation time, which the database supplies, so it cannot be fooled by
        # a caller's clock.
        CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
        # Status and cancelled_at are two views of one fact, so they cannot disagree.
        CheckConstraint(
            "(status = 'CANCELLED') = (cancelled_at IS NOT NULL)",
            name="cancelled_at_matches_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid7)
    merchant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    mandate_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    subtotal_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    shipping_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    discount_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    total_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # No server default, for the same reason a mandate has none: an insert that does not
    # state a status is a bug, and failing beats defaulting a quote into existence.
    status: Mapped[CheckoutStatus] = mapped_column(CHECKOUT_STATUS, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    lines: Mapped[list[CheckoutLine]] = relationship(
        back_populates="checkout",
        lazy="raise_on_sql",
        cascade="all, delete-orphan",
        # Version 7 identifiers are time ordered, so this is the order the lines were
        # quoted in rather than an arbitrary but stable one.
        order_by="CheckoutLine.id",
    )


class CheckoutLine(Base):
    """One variant on a quote, as the catalog described it when the quote was made.

    `unit_price_amount_minor` is a snapshot, not a pointer. Recomputing a total from the
    live variant would mean a catalog edit silently rewrote what a buyer was quoted, which
    is exactly what a quote exists to prevent.

    `product_category` and `variant_attributes` are snapshots for the same reason, and
    they are what semantic authorization is evaluated against. Reading them from the live
    catalog instead would mean a merchant changing `black` to `blue` after the fact could
    change whether a historical quote satisfied what the buyer asked for. A checkout has to
    stay explainable as the exact offer the buyer considered.

    Only structured commerce values are copied here. Title, description and anything else a
    merchant wrote in prose are absent: the evaluator compares machine readable fields, and
    prose in a financial row is duplication that answers nothing.

    The line carries its own currency rather than inheriting one by convention, because
    the project rule is that an amount and its currency travel together. It cannot
    disagree with the checkout: the composite foreign key includes it.
    """

    __tablename__ = "checkout_line"
    __table_args__ = (
        # Named explicitly. The metadata convention would generate a 66 character name,
        # and PostgreSQL truncates identifiers at 63 bytes, so the name in the migration
        # and the name in the database would silently disagree.
        ForeignKeyConstraint(
            ["checkout_id", "merchant_id", "currency"],
            ["checkout_session.id", "checkout_session.merchant_id", "checkout_session.currency"],
            name="fk_checkout_line_checkout_session",
            ondelete="CASCADE",
        ),
        # The variant is reached through (id, merchant_id), so a line cannot put another
        # merchant's variant on this merchant's quote. RESTRICT because a quote that
        # references a variant which no longer exists is a hole in the financial record.
        ForeignKeyConstraint(
            ["variant_id", "merchant_id"],
            ["variant.id", "variant.merchant_id"],
            ondelete="RESTRICT",
        ),
        # One line per variant. Two lines for one variant are one line with a larger
        # quantity, and allowing both shapes would make a quantity ceiling depend on how a
        # caller chose to split the request. Also the index for lookup by checkout and for
        # the cascade delete, since checkout_id is leftmost.
        UniqueConstraint("checkout_id", "variant_id"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("unit_price_amount_minor >= 0", name="unit_price_not_negative"),
        CheckConstraint(f"currency ~ '{CURRENCY_PATTERN}'", name="currency_format"),
        # The same shape rule the catalog holds on variant.attributes, so a consumer of
        # either can rely on a JSON object rather than testing what arrived.
        CheckConstraint(
            "jsonb_typeof(variant_attributes) = 'object'",
            name="variant_attributes_are_an_object",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid7)
    checkout_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    merchant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    variant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    # Nullable because product.category is. A catalog that does not say what something is
    # cannot satisfy an allowed category constraint, and that failure is the honest answer
    # rather than a reason to guess.
    product_category: Mapped[str | None] = mapped_column(String(120), nullable=True)
    variant_attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    checkout: Mapped[CheckoutSession] = relationship(back_populates="lines", lazy="raise_on_sql")

    @property
    def line_amount_minor(self) -> int:
        """What this line contributes to the subtotal."""
        return self.quantity * self.unit_price_amount_minor
