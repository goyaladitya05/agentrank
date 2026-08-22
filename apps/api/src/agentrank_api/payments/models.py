"""Payment attempt persistence.

A `PaymentAttempt` is the durable proof that one payment operation was authorized. It is
written and committed before any payment provider is called, and it is what the provider
call is made from: the amount, the currency and the identity a provider is given are read
off this row and never off a live `CheckoutSession`. That is the whole point of the table.
What was authorized and what was sent to the provider are the same numbers because they are
the same columns.

Seven properties are enforced by the database rather than by application code, because the
database is the only layer that cannot be bypassed:

- an attempt, its checkout, its mandate, its reservation and its merchant are one consistent
  set, through composite foreign keys rather than through application care
- the amount and the currency equal the checkout's total and currency, structurally, through
  the same foreign key that binds the checkout. Freezing here is not a copy that could drift
- one logical payment operation exists once, through a unique constraint on the checkout and
  the idempotency key
- an interactive provider binding cannot disagree with the attempt about the merchant, the
  amount or the currency, because a composite foreign key points at those four columns here
- at most one attempt under a mandate is SUCCEEDED, through a partial unique index. This is
  the single purchase mandate rule, structural at last
- at most one attempt for a checkout is SUCCEEDED, through the same kind of index
- at most one attempt under a mandate is non terminal, through a third one. That is what
  stops two candidate checkouts under one mandate from both reaching a provider
- ownership, money and identity are immutable, and the lifecycle is a whitelist of
  transitions rather than a blacklist of terminal values

The last one is a trigger rather than a constraint, because it is a rule about a transition
rather than about a row. See the migration for the statement itself.
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
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from agentrank_api.models import Base
from agentrank_api.money import CURRENCY_PATTERN

# An idempotency key is a caller chosen or server generated identity for one logical payment
# operation, and it is the same string the provider is given. Bounded and restricted to
# characters that survive a URL, a header and a provider API without escaping, because a key
# that has to be encoded differently in two places is two keys.
IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,63}$"
MAX_IDEMPOTENCY_KEY_LENGTH = 64
MIN_IDEMPOTENCY_KEY_LENGTH = 8


class PaymentAttemptStatus(StrEnum):
    """The states one payment operation can be in.

    Five, and each names an externally meaningful degree of certainty rather than an internal
    step. There is no PENDING and no PROCESSING, because neither would tell a caller anything
    it could act on.

    ADMITTED
        The operation was authorized while every lock was held, and durably recorded. No
        provider request has been dispatched, and this is the strong direction of that
        claim: IN_FLIGHT is committed before any network call begins, so an attempt found in
        ADMITTED after a crash was definitely never sent. It is safe to dispatch.

    IN_FLIGHT
        A provider request may have been dispatched. The uncertainty is one sided on purpose.
        An attempt found here after a crash must never be blindly dispatched again; it has to
        be reconciled against the provider first.

    SUCCEEDED
        The provider definitively reported success. Terminal.

    FAILED
        The provider definitively reported a decline or a failure in which no money moved.
        Terminal.

    UNKNOWN
        The result was ambiguous: a timeout, a reset connection, a response that never
        arrived. It is not FAILED, and nothing may retry it automatically. It is resolved by
        querying the provider. Not terminal, and deliberately not stamped as resolved.
    """

    ADMITTED = "ADMITTED"
    IN_FLIGHT = "IN_FLIGHT"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class OutcomeSource(StrEnum):
    """Which path learned the outcome this row currently records.

    Not called `resolved_by`, because UNKNOWN carries a source and is explicitly not
    resolved. The question this answers is where the information came from, which stays
    answerable in every state that has any information at all.

    EXECUTION
        A dispatch received the answer from the provider.

    RECONCILIATION
        A query received it from the provider afterwards.

    OPERATOR
        Nobody received it. An operator decided an unresolved payment would never be
        resolved and terminalized it deliberately, accepting the residual risk that the
        provider may yet reveal that money moved. It is its own value rather than borrowed
        from RECONCILIATION because the authoritative row must not claim a provider said
        something no provider said. See `agentrank_api.payments.recovery`.

    INTERACTIVE
        A customer completed a provider hosted checkout, the callback was signature verified,
        and the provider was then asked what the payment actually did. Its own value for the
        same reason OPERATOR is: this application never dispatched anything, so EXECUTION would
        claim a dispatch that never happened, and RECONCILIATION would describe a payment
        settled in seconds by a browser as one recovered afterwards from an ambiguous result.
        The two are different operational facts and an operator reading this column needs to
        tell them apart. See `agentrank_api.razorpay.verification`.
    """

    EXECUTION = "EXECUTION"
    RECONCILIATION = "RECONCILIATION"
    OPERATOR = "OPERATOR"
    INTERACTIVE = "INTERACTIVE"


# Stored as text with a check constraint rather than as a native PostgreSQL enum, for the
# same reason as every other enumeration here: adding a value should be an ordinary
# constraint change rather than ALTER TYPE.
PAYMENT_ATTEMPT_STATUS = Enum(
    PaymentAttemptStatus,
    native_enum=False,
    create_constraint=False,
    validate_strings=True,
    length=16,
    name="payment_attempt_status",
)

OUTCOME_SOURCE = Enum(
    OutcomeSource,
    native_enum=False,
    create_constraint=False,
    validate_strings=True,
    length=16,
    name="outcome_source",
)

TERMINAL_STATUSES: frozenset[PaymentAttemptStatus] = frozenset(
    {PaymentAttemptStatus.SUCCEEDED, PaymentAttemptStatus.FAILED}
)

# Non terminal means this identity may still reach a provider, or may already have reached
# one without the answer coming back. A mandate may have at most one attempt in this set, and
# that is what stops two candidate checkouts under one mandate from both being dispatched.
OPEN_STATUSES: tuple[PaymentAttemptStatus, ...] = (
    PaymentAttemptStatus.ADMITTED,
    PaymentAttemptStatus.IN_FLIGHT,
    PaymentAttemptStatus.UNKNOWN,
)

_STATUS_VALUES = ", ".join(f"'{status.value}'" for status in PaymentAttemptStatus)
_SOURCE_VALUES = ", ".join(f"'{source.value}'" for source in OutcomeSource)

# The predicates the partial unique indexes are built on. Named here because the model and
# the migration have to state the same thing, and because the repository filters on them.
SUCCEEDED_PREDICATE = f"status = '{PaymentAttemptStatus.SUCCEEDED.value}'"
OPEN_PREDICATE = "status IN (" + ", ".join(f"'{status.value}'" for status in OPEN_STATUSES) + ")"


class PaymentAttempt(Base):
    """One payment operation, frozen at the instant it was authorized.

    There is no `updated_at`. Every column except the lifecycle ones is immutable, and each
    transition stamps its own timestamp, so a general purpose modification time would only be
    an ambiguous fourth name for one of them.

    Three timestamps, and they answer three different questions. `created_at` is the
    admission instant, which is the instant the authorization was decided at. `dispatched_at`
    is when this attempt stopped being certainly unsent. `resolved_at` is when a definitive
    answer arrived, and it is null for UNKNOWN, because an ambiguous result is not a
    resolution and recording one would be inventing certainty.

    There are no relationships to the checkout, the mandate or the reservation. The composite
    foreign keys tie all of them together structurally, and a provider call that could
    navigate to a live `CheckoutSession` is a provider call that could read a number this row
    was supposed to have frozen.
    """

    __tablename__ = "payment_attempt"
    __table_args__ = (
        # One foreign key, four invariants. The checkout is reached through
        # (id, merchant_id, mandate_id, currency, total_amount_minor), so an attempt cannot
        # name another merchant's checkout, cannot claim a mandate the checkout was not
        # quoted against, and cannot carry an amount or a currency that differ from what was
        # quoted and authorized. Freezing is therefore structural rather than a copy that
        # something could later diverge from.
        #
        # Named explicitly, because the metadata convention would produce a name longer than
        # the 63 bytes PostgreSQL keeps, and the name written in the migration and the name
        # in the database would silently disagree.
        #
        # RESTRICT. A payment attempt is financial history.
        ForeignKeyConstraint(
            ["checkout_id", "merchant_id", "mandate_id", "currency", "amount_minor"],
            [
                "checkout_session.id",
                "checkout_session.merchant_id",
                "checkout_session.mandate_id",
                "checkout_session.currency",
                "checkout_session.total_amount_minor",
            ],
            name="fk_payment_attempt_checkout",
            ondelete="RESTRICT",
        ),
        # The reservation is reached through (id, merchant_id, checkout_id), so an attempt
        # cannot be bound to another merchant's hold, and cannot be bound to a hold taken for
        # a different checkout. There is no separate foreign key to merchant or to
        # spending_mandate: both are transitive through the checkout, which already reaches
        # the mandate, which already reaches the merchant.
        ForeignKeyConstraint(
            ["reservation_id", "merchant_id", "checkout_id"],
            [
                "inventory_reservation.id",
                "inventory_reservation.merchant_id",
                "inventory_reservation.checkout_id",
            ],
            name="fk_payment_attempt_reservation",
            ondelete="RESTRICT",
        ),
        # One logical payment operation exists once. Two requests carrying the same key
        # against the same checkout are the same operation and must resolve to this one row,
        # never to two rows and two provider calls.
        UniqueConstraint("checkout_id", "idempotency_key", name="uq_payment_attempt_identity"),
        # Not a rule, a target. PostgreSQL needs a unique constraint on exactly these columns
        # for a composite foreign key to point at them, and `razorpay_checkout` points at them
        # so that a provider binding structurally carries this attempt's merchant, amount and
        # currency rather than a copy of them. It changes no row: all four are already unique
        # because `id` alone is.
        UniqueConstraint(
            "id", "merchant_id", "amount_minor", "currency", name="uq_payment_attempt_binding"
        ),
        # Another target rather than another rule, added for the same reason as the one above
        # and pointing at two of its four columns. A benchmark mission run records which payment
        # a mission produced, bound through (payment_attempt_id, merchant_id), so a benchmark
        # result cannot name a payment belonging to a different merchant. The wider constraint
        # above cannot serve it: PostgreSQL needs a unique constraint on exactly the referenced
        # columns. It changes no row, because both are already unique through `id` alone.
        UniqueConstraint("id", "merchant_id", name="uq_payment_attempt_ownership"),
        CheckConstraint(f"status IN ({_STATUS_VALUES})", name="status_known"),
        CheckConstraint(
            f"outcome_source IS NULL OR outcome_source IN ({_SOURCE_VALUES})",
            name="outcome_source_known",
        ),
        CheckConstraint("amount_minor >= 0", name="amount_not_negative"),
        CheckConstraint(f"currency ~ '{CURRENCY_PATTERN}'", name="currency_format"),
        CheckConstraint(
            f"idempotency_key ~ '{IDEMPOTENCY_KEY_PATTERN}'", name="idempotency_key_format"
        ),
        # ADMITTED is the one state that promises no provider request has begun, so it is the
        # one state with no dispatch instant. The two cannot disagree.
        CheckConstraint(
            "(status = 'ADMITTED') = (dispatched_at IS NULL)", name="dispatched_at_matches_status"
        ),
        # Resolved means definitive. UNKNOWN carries no resolution instant on purpose.
        CheckConstraint(
            "(status IN ('SUCCEEDED', 'FAILED')) = (resolved_at IS NOT NULL)",
            name="resolved_at_matches_status",
        ),
        # An outcome source without an outcome, or an outcome without a source, would each be
        # half a record.
        CheckConstraint(
            "(status IN ('ADMITTED', 'IN_FLIGHT')) = (outcome_source IS NULL)",
            name="outcome_source_matches_status",
        ),
        # A failure code is what a decline was, so it belongs to a decline and to nothing
        # else. A success carries a provider reference instead.
        CheckConstraint(
            "failure_code IS NULL OR status = 'FAILED'", name="failure_code_only_when_failed"
        ),
        CheckConstraint(
            "provider_reference IS NULL OR length(btrim(provider_reference)) > 0",
            name="provider_reference_not_blank",
        ),
        # The single purchase mandate rule, structural. At most one attempt under a mandate
        # may be SUCCEEDED, whatever the application believes it checked. The predicate is
        # static, which is what makes it indexable.
        Index(
            "uq_payment_attempt_mandate_succeeded",
            "mandate_id",
            unique=True,
            postgresql_where=text(SUCCEEDED_PREDICATE),
        ),
        # A checkout is paid at most once, for the same reason and by the same mechanism.
        Index(
            "uq_payment_attempt_checkout_succeeded",
            "checkout_id",
            unique=True,
            postgresql_where=text(SUCCEEDED_PREDICATE),
        ),
        # At most one non terminal attempt per mandate. This is what makes a second provider
        # operation unreachable rather than merely unlikely: a mandate authorizes one
        # purchase, so it may have one payment in flight, and a second candidate checkout
        # under the same mandate is refused at admission rather than racing the first one to
        # a provider. It also gives the per checkout property, since a checkout names exactly
        # one mandate.
        Index(
            "uq_payment_attempt_mandate_open",
            "mandate_id",
            unique=True,
            postgresql_where=text(OPEN_PREDICATE),
        ),
        # The partial indexes above cover a subset of rows each, so none of them serves a
        # read of everything ever attempted for one checkout, or the RESTRICT check when a
        # checkout or a reservation is deleted.
        Index(None, "checkout_id"),
        Index(None, "reservation_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid7)
    merchant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    checkout_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    mandate_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    reservation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(MAX_IDEMPOTENCY_KEY_LENGTH), nullable=False)
    # Frozen at admission, and equal to the checkout total by composite foreign key. A
    # provider is charged from these two columns and from nothing else.
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    # No server default, for the same reason a mandate, a checkout and a reservation have
    # none: an insert that does not state a status is a bug, and failing beats defaulting a
    # payment into existence.
    status: Mapped[PaymentAttemptStatus] = mapped_column(PAYMENT_ATTEMPT_STATUS, nullable=False)
    provider_reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    outcome_source: Mapped[OutcomeSource | None] = mapped_column(OUTCOME_SOURCE, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def is_terminal(self) -> bool:
        """Whether this attempt has a definitive answer that nothing will change."""
        return self.status in TERMINAL_STATUSES

    @property
    def is_open(self) -> bool:
        """Whether this identity may still reach a provider or is waiting on one.

        The complement of terminal. It is what the mandate scoped uniqueness is built on, and
        what a caller asking whether a payment is still going on is really asking.
        """
        return not self.is_terminal
