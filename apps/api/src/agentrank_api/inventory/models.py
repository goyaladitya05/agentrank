"""Inventory reservation persistence.

A reservation is a claim on stock held for one checkout while that checkout is being
prepared for execution. It is not a sale, it is not an order, and it does not consume
inventory: `variant.inventory_quantity` stays the authoritative total and is never
decremented here. What a reservation does is make the difference between total stock and
stock already promised to someone else visible, so two checkouts cannot both be prepared
against the last unit.

Five properties are enforced by the database rather than by application code, because the
database is the only layer that cannot be bypassed:

- a reservation, its checkout, its lines and their variants all belong to one merchant,
  through composite foreign keys
- a reservation quantity is positive, and one variant appears at most once on a reservation
- a reservation cannot be created already expired
- at most one reservation per checkout is holding stock, through a partial unique index
- ownership, expiry and line quantities are immutable, and every status change is a
  transition on a whitelist rather than an update that merely avoids a terminal value

The last one is a pair of triggers rather than constraints, because they are rules about a
transition rather than about a row. See the migration for the statements themselves.

Expiry is derived by comparing `expires_at` with the current time, exactly as a mandate's
and a checkout's are. An ACTIVE reservation whose expiry has passed stops consuming
capacity on its own, so no sweeper job exists whose failure would leave stock held forever.

Commitment is where that stops. Once a reservation is bound to an admitted payment attempt
it is COMMITTED, and expiry no longer governs it: the authorization instant was admission,
and a provider operation that completes after the original `expires_at` is still the
operation that was authorized. A hold that lapsed under a payment nobody can recall would
be stock sold twice.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKeyConstraint,
    Index,
    Integer,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from agentrank_api.models import Base


class ReservationStatus(StrEnum):
    """The states a reservation can be in.

    ACTIVE
        Stock held before any payment was admitted. Expiry governs it: at `expires_at` it
        stops holding anything, with no database mutation and no sweeper job.

    COMMITTED
        Stock bound to a payment attempt that has been admitted and has not reached a
        definitive outcome. It holds stock exactly as ACTIVE does, and expiry no longer
        governs it. The reservation was effective at the instant the payment was authorized,
        and that instant is what the provider operation rests on.

    RELEASED
        The stock was given back, on purpose, with a recorded reason. Terminal.

    CONSUMED
        A payment succeeded and the units were permanently taken out of
        `variant.inventory_quantity`. Terminal, and deliberately distinct from RELEASED: one
        says the merchant got the stock back and the other says a buyer took it away. A
        CONSUMED reservation holds nothing, because the total it would have been subtracted
        from has already been reduced. That is what stops the same units being counted twice.
    """

    ACTIVE = "ACTIVE"
    COMMITTED = "COMMITTED"
    RELEASED = "RELEASED"
    CONSUMED = "CONSUMED"


# Stored as text with a check constraint rather than as a native PostgreSQL enum, for the
# same reason as every other enumeration here: adding COMMITTED and CONSUMED was an ordinary
# constraint change in an ordinary migration rather than an ALTER TYPE.
RESERVATION_STATUS = Enum(
    ReservationStatus,
    native_enum=False,
    create_constraint=False,
    validate_strings=True,
    length=16,
    name="reservation_status",
)

_STATUS_VALUES = ", ".join(f"'{status.value}'" for status in ReservationStatus)

# The two statuses under which a reservation is still a claim on stock. Everything else is
# terminal and holds nothing. Named once here because the model, the migration, the
# accounting query and the service all have to mean the same set.
HOLDING_STATUSES: tuple[ReservationStatus, ...] = (
    ReservationStatus.ACTIVE,
    ReservationStatus.COMMITTED,
)

# The partial unique index guaranteeing one holding reservation per checkout. Named here
# because both the model and the migration have to state the same predicate, and because
# the accounting query in the repository has to filter on the same status values.
HOLDING_PREDICATE = (
    "status IN (" + ", ".join(f"'{status.value}'" for status in HOLDING_STATUSES) + ")"
)


class InventoryReservation(Base):
    """Stock held for one checkout, for a bounded time.

    There is no `updated_at`. Everything except `status`, `released_at` and `consumed_at` is
    immutable, and each terminal transition stamps its own timestamp, so a general purpose
    modification time would only be an ambiguous third name for one of them. Commitment
    stamps nothing here: the instant a reservation was bound to a payment is the payment
    attempt's `created_at`, and recording it twice would create two answers to one question.

    `expires_at` is derived by the server from the checkout and the mandate and is never
    supplied by a caller. A reservation that outlived either would hold stock for a quote
    nobody may act on any more.

    A checkout may accumulate several reservations over its life, at most one of which is
    holding stock. That keeps a released reservation as history rather than rewriting it, which is
    the same choice this project makes everywhere else authorization adjacent data is
    written. See docs/architecture.md.
    """

    __tablename__ = "inventory_reservation"
    __table_args__ = (
        # Named explicitly. The metadata convention would produce a 65 character name and
        # PostgreSQL truncates identifiers at 63 bytes, so the name written in the
        # migration and the name in the database would silently disagree.
        #
        # The checkout is reached through (id, merchant_id), so a reservation cannot hold
        # stock against a checkout belonging to a different merchant. Merchant integrity is
        # transitive through it, since checkout_session.merchant_id already reaches
        # merchant, which is why there is no second foreign key straight to merchant.
        #
        # RESTRICT, not CASCADE. A reservation is a record of stock having been held.
        ForeignKeyConstraint(
            ["checkout_id", "merchant_id"],
            ["checkout_session.id", "checkout_session.merchant_id"],
            name="fk_inventory_reservation_checkout",
            ondelete="RESTRICT",
        ),
        # Redundant against the primary key, and present only as a composite foreign key
        # target, so a reservation line carries its merchant structurally.
        UniqueConstraint("id", "merchant_id"),
        # The target a payment attempt is bound through. Carrying the checkout into it is
        # what makes "this payment is for the stock held for this quote" structural rather
        # than something the admission code has to remember to compare.
        UniqueConstraint("id", "merchant_id", "checkout_id"),
        CheckConstraint(f"status IN ({_STATUS_VALUES})", name="status_known"),
        # A reservation that has already expired holds nothing. The comparison is against
        # the row's own creation time, which the database supplies, so it cannot be fooled
        # by a caller's clock.
        CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
        # Status and released_at are two views of one fact, so they cannot disagree. The
        # same for status and consumed_at.
        CheckConstraint(
            "(status = 'RELEASED') = (released_at IS NOT NULL)",
            name="released_at_matches_status",
        ),
        CheckConstraint(
            "(status = 'CONSUMED') = (consumed_at IS NOT NULL)",
            name="consumed_at_matches_status",
        ),
        # One holding reservation per checkout, structurally. The predicate is static: it
        # says nothing about expiry, because a predicate mentioning now() would not be
        # immutable and PostgreSQL will not index on one. Expiry is handled by the
        # accounting query instead, and the two together give the property that matters,
        # which is that reserved stock can never be counted twice for one checkout.
        Index(
            "uq_inventory_reservation_active_checkout",
            "checkout_id",
            unique=True,
            postgresql_where=text(HOLDING_PREDICATE),
        ),
        # The partial index above covers active rows only, so it serves neither a read of
        # a checkout's reservation history nor the RESTRICT check when a checkout is
        # deleted.
        Index(None, "checkout_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid7)
    merchant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    checkout_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    # No server default, for the same reason a mandate and a checkout have none: an insert
    # that does not state a status is a bug, and failing beats defaulting a claim on stock
    # into existence.
    status: Mapped[ReservationStatus] = mapped_column(RESERVATION_STATUS, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    lines: Mapped[list[InventoryReservationLine]] = relationship(
        back_populates="reservation",
        lazy="raise_on_sql",
        cascade="all, delete-orphan",
        # Version 7 identifiers are time ordered, so this is the order the lines were
        # written in rather than an arbitrary but stable one.
        order_by="InventoryReservationLine.id",
    )

    @property
    def total_quantity(self) -> int:
        """How many units this reservation holds.

        The sum of the line quantities, never the number of lines. Reading `lines` raises
        unless they were loaded, which is deliberate: a total computed from a collection
        that was never fetched would be a confident zero.
        """
        return sum(line.quantity for line in self.lines)


class InventoryReservationLine(Base):
    """One variant and how many units of it this reservation holds.

    There is no price here and no currency. A reservation is a claim on stock, not on
    money: what the buyer pays is the checkout's business and is already snapshotted there.

    The quantity is copied from the checkout line rather than supplied by a caller. A
    caller who could say "reserve one" for a quote of two would be choosing how much stock
    an authorization actually protects.
    """

    __tablename__ = "inventory_reservation_line"
    __table_args__ = (
        # Named explicitly, for the same length reason as the reservation's foreign key.
        ForeignKeyConstraint(
            ["reservation_id", "merchant_id"],
            ["inventory_reservation.id", "inventory_reservation.merchant_id"],
            name="fk_inventory_reservation_line_reservation",
            ondelete="CASCADE",
        ),
        # The variant is reached through (id, merchant_id), so a line cannot hold another
        # merchant's stock. RESTRICT because a reservation pointing at a variant that no
        # longer exists is a hole in the record of what was held.
        ForeignKeyConstraint(
            ["variant_id", "merchant_id"],
            ["variant.id", "variant.merchant_id"],
            ondelete="RESTRICT",
        ),
        # One line per variant, matching the checkout line rule it is copied from. Two
        # lines for one variant are one line with a larger quantity, and allowing both
        # shapes would make the reserved total depend on how the rows were written.
        UniqueConstraint("reservation_id", "variant_id"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        # The composite foreign keys create no index on the referencing side. The first is
        # needed to load a reservation's lines and for the cascade delete, the second for
        # the accounting query, which sums reserved quantity per variant.
        Index(None, "reservation_id"),
        Index(None, "variant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid7)
    reservation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    merchant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    variant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    reservation: Mapped[InventoryReservation] = relationship(
        back_populates="lines", lazy="raise_on_sql"
    )
