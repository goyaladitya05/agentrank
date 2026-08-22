"""The durable relationship between one AgentRank payment and one Razorpay Order.

Standard Checkout runs in a browser, and a browser is not a place state lives. The customer can
reload the page, close the tab, pay from a different device, or come back an hour later, and the
question "which Razorpay order belongs to this payment attempt" has to be answerable by this
application without asking any of them. That is what this table is.

Five properties are enforced by the database rather than by application code, because the
database is the only layer that cannot be bypassed:

- one payment attempt has at most one logical Razorpay Order, through a unique constraint on
  `payment_attempt_id`. Not a convention and not a check in a service that could be reached by
  two requests at once
- the merchant, the amount and the currency are the attempt's own, structurally, through a
  composite foreign key onto `(id, merchant_id, amount_minor, currency)`. It is impossible for a
  binding to name one merchant's attempt while claiming another merchant, and impossible for it
  to carry an amount the attempt does not. What is sent to Razorpay is what was admitted,
  because they are the same columns
- a provider receipt is unique locally, so two bindings cannot claim one identity even before
  Razorpay is asked
- a provider order identifier is unique when present, so one Razorpay order cannot be bound to
  two attempts
- ownership, money, identity and the order identifier are immutable, and the lifecycle is a
  whitelist of transitions

The last one is a trigger rather than a constraint, because it is a rule about a transition
rather than about a row. See the migration for the statement itself.

The receipt is written before Razorpay is called, and that ordering is the whole recovery story.
A row committed first means a create whose response never arrived can be recovered by asking
Razorpay for the order carrying that receipt, rather than by creating a second one and hoping.
The receipt is also derivable from scratch, so even a lost row is recoverable, and the column
exists so that the uniqueness is structural rather than arithmetic.

There is no `updated_at`. Every column except the two lifecycle pairs is immutable, and each
transition stamps its own timestamp.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
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
from sqlalchemy.orm import Mapped, mapped_column

from agentrank_api.models import Base
from agentrank_api.money import CURRENCY_PATTERN
from agentrank_api.payments.references import MAX_OPERATION_REFERENCE_LENGTH

# Razorpay identifiers are short prefixed strings such as `order_abc123` and `pay_abc123`. Sized
# generously rather than exactly, because the length is the vendor's to change and a column that
# truncated one would corrupt the only handle this application has on a payment.
MAX_PROVIDER_IDENTIFIER_LENGTH = 64


class RazorpayCheckoutStatus(StrEnum):
    """Where one interactive checkout has got to.

    This lifecycle exists so that `PaymentAttemptStatus` does not have to grow one. The payment
    attempt states describe an autonomous provider execution: ADMITTED means no provider request
    has been dispatched, IN_FLIGHT means one may have been. Creating a Razorpay Order is neither.
    No money can move from an order, and the customer may never open the checkout at all, so
    calling that state IN_FLIGHT would make the strongest claim in the payment kernel untrue for
    an entire class of payments.

    So the waiting happens here, on the binding, and the attempt stays ADMITTED until a signature
    verified callback proves that a provider payment actually exists.

    PREPARING
        A receipt has been reserved and committed and no Razorpay order is known yet. Either the
        create has not happened, or it happened and the answer did not come back. Both are
        resolved the same way, by asking Razorpay what exists under this receipt.

    AWAITING_PAYMENT
        A Razorpay order exists, its amount, currency and receipt have been checked against the
        attempt, and it is safe to open Standard Checkout against it. Nothing has been paid.

    CONFIRMED
        A callback was verified, the provider payment was confirmed with Razorpay, and the
        AgentRank outcome was applied through the ordinary payment machinery. Terminal.
    """

    PREPARING = "PREPARING"
    AWAITING_PAYMENT = "AWAITING_PAYMENT"
    CONFIRMED = "CONFIRMED"


# Stored as text with a check constraint rather than as a native PostgreSQL enum, for the same
# reason as every other enumeration in this schema: adding a value should be an ordinary
# constraint change rather than ALTER TYPE.
RAZORPAY_CHECKOUT_STATUS = Enum(
    RazorpayCheckoutStatus,
    native_enum=False,
    create_constraint=False,
    validate_strings=True,
    length=20,
    name="razorpay_checkout_status",
)

_STATUS_VALUES = ", ".join(f"'{status.value}'" for status in RazorpayCheckoutStatus)


class RazorpayCheckout(Base):
    """One AgentRank payment attempt, bound to one Razorpay Order."""

    __tablename__ = "razorpay_checkout"
    __table_args__ = (
        # One foreign key, three invariants. The attempt is reached through
        # (payment_attempt_id, merchant_id, amount_minor, currency), so a binding cannot name
        # another merchant's payment, cannot carry an amount that differs from what was
        # admitted, and cannot carry a different currency. Every one of those columns is
        # immutable on `payment_attempt`, so the freezing is structural rather than a copy that
        # could later drift.
        #
        # This is what makes "the provider amount comes from authoritative admitted state" a
        # property of the schema instead of a property of a service remembering to read the
        # right row.
        #
        # Named explicitly, because the metadata convention would produce a name longer than the
        # 63 bytes PostgreSQL keeps.
        #
        # RESTRICT. A provider order is financial history.
        ForeignKeyConstraint(
            ["payment_attempt_id", "merchant_id", "amount_minor", "currency"],
            [
                "payment_attempt.id",
                "payment_attempt.merchant_id",
                "payment_attempt.amount_minor",
                "payment_attempt.currency",
            ],
            name="fk_razorpay_checkout_payment_attempt",
            ondelete="RESTRICT",
        ),
        # One attempt maps to at most one logical Razorpay Order. Two requests preparing the
        # same checkout at the same instant resolve to one row rather than to two orders.
        UniqueConstraint("payment_attempt_id", name="uq_razorpay_checkout_attempt"),
        # A receipt identifies one operation at Razorpay, so it identifies one here too. This is
        # the local half of the guarantee: the remote half is that Razorpay treats a receipt as
        # unique on the account and refuses a second order under one.
        UniqueConstraint("provider_receipt", name="uq_razorpay_checkout_receipt"),
        # Unique when present. PostgreSQL treats NULLs as distinct in a unique constraint, so
        # every PREPARING row coexists happily and no two bindings can name one order.
        UniqueConstraint("provider_order_id", name="uq_razorpay_checkout_order"),
        CheckConstraint(f"status IN ({_STATUS_VALUES})", name="status_known"),
        # Positive rather than merely non negative. Razorpay refuses a zero amount order, so a
        # row that could hold one would be a row describing an order that cannot exist.
        CheckConstraint("amount_minor > 0", name="amount_positive"),
        CheckConstraint(f"currency ~ '{CURRENCY_PATTERN}'", name="currency_format"),
        CheckConstraint("length(btrim(provider_receipt)) > 0", name="provider_receipt_not_blank"),
        CheckConstraint(
            "provider_order_id IS NULL OR length(btrim(provider_order_id)) > 0",
            name="provider_order_id_not_blank",
        ),
        CheckConstraint(
            "provider_payment_id IS NULL OR length(btrim(provider_payment_id)) > 0",
            name="provider_payment_id_not_blank",
        ),
        # PREPARING is the one state that promises no order is known, so it is the one state
        # with no order identifier. The two cannot disagree.
        CheckConstraint(
            "(status = 'PREPARING') = (provider_order_id IS NULL)",
            name="order_id_matches_status",
        ),
        CheckConstraint(
            "(provider_order_id IS NULL) = (order_created_at IS NULL)",
            name="order_created_at_matches_order_id",
        ),
        # A confirmed checkout knows which payment confirmed it and when. A checkout that is
        # not confirmed knows neither. Half a record in either direction is refused.
        CheckConstraint(
            "(status = 'CONFIRMED') = (provider_payment_id IS NOT NULL)",
            name="payment_id_matches_status",
        ),
        CheckConstraint(
            "(status = 'CONFIRMED') = (confirmed_at IS NOT NULL)",
            name="confirmed_at_matches_status",
        ),
        # The merchant scoped read every endpoint performs, and the RESTRICT check when an
        # attempt is deleted. The unique constraints above cover the other access paths.
        Index(None, "merchant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid7)
    merchant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    payment_attempt_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    # The derived provider operation reference. Sized to what a Razorpay receipt allows, which
    # is the smaller of the two bounds and therefore the one that matters.
    provider_receipt: Mapped[str] = mapped_column(
        String(MAX_OPERATION_REFERENCE_LENGTH), nullable=False
    )
    provider_order_id: Mapped[str | None] = mapped_column(
        String(MAX_PROVIDER_IDENTIFIER_LENGTH), nullable=True
    )
    # Which Razorpay payment confirmed this checkout. Recorded for the trail and for an operator
    # looking a payment up in the dashboard. The authoritative outcome lives on the
    # `PaymentAttempt`, and this column never decides anything.
    provider_payment_id: Mapped[str | None] = mapped_column(
        String(MAX_PROVIDER_IDENTIFIER_LENGTH), nullable=True
    )
    # Equal to the attempt's amount and currency by composite foreign key. Razorpay is asked to
    # collect these two values and nothing reads a live checkout to decide what to charge.
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    # No server default, for the same reason as everywhere else in this schema: an insert that
    # does not state a status is a bug, and failing beats defaulting a payment into existence.
    status: Mapped[RazorpayCheckoutStatus] = mapped_column(RAZORPAY_CHECKOUT_STATUS, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    order_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def is_confirmed(self) -> bool:
        return self.status is RazorpayCheckoutStatus.CONFIRMED
