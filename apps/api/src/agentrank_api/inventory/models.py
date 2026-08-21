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
- at most one reservation per checkout is ACTIVE, through a partial unique index
- ownership, expiry and line quantities are immutable, and release is terminal

The last one is a pair of triggers rather than constraints, because they are rules about a
transition rather than about a row. See the migration for the statements themselves.

Expiry is derived by comparing `expires_at` with the current time, exactly as a mandate's
and a checkout's are. An ACTIVE reservation whose expiry has passed stops consuming
capacity on its own, so no sweeper job exists whose failure would leave stock held forever.
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

    Deliberately two. Expiry is derived from `expires_at` and the current time rather than
    written into this column, so an expired reservation needs no database mutation to stop
    holding stock.

    There is no CONSUMED value. Nothing in this system pays for anything yet, and a status
    naming a state the code cannot reach would be a promise rather than a record.
    """

    ACTIVE = "ACTIVE"
    RELEASED = "RELEASED"


# Stored as text with a check constraint rather than as a native PostgreSQL enum, for the
# same reason as every other enumeration here: this set grows when payment execution
# arrives, and adding a value should be an ordinary constraint change, not ALTER TYPE.
RESERVATION_STATUS = Enum(
    ReservationStatus,
    native_enum=False,
    create_constraint=False,
    validate_strings=True,
    length=16,
    name="reservation_status",
)

_STATUS_VALUES = ", ".join(f"'{status.value}'" for status in ReservationStatus)

# The partial unique index guaranteeing one active reservation per checkout. Named here
# because both the model and the migration have to state the same predicate, and because
# the accounting query in the repository has to filter on the same status value.
ACTIVE_PREDICATE = f"status = '{ReservationStatus.ACTIVE.value}'"


class InventoryReservation(Base):
    """Stock held for one checkout, for a bounded time.

    There is no `updated_at`. Everything except `status` and `released_at` is immutable,
    and the one transition that exists stamps its own timestamp, so a general purpose
    modification time would only be a second name for `released_at`.

    `expires_at` is derived by the server from the checkout and the mandate and is never
    supplied by a caller. A reservation that outlived either would hold stock for a quote
    nobody may act on any more.

    A checkout may accumulate several reservations over its life, at most one of which is
    ACTIVE. That keeps a released reservation as history rather than rewriting it, which is
    the same choice this project makes everywhere else authorization adjacent data is
    written. See docs/decisions.md.
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
        CheckConstraint(f"status IN ({_STATUS_VALUES})", name="status_known"),
        # A reservation that has already expired holds nothing. The comparison is against
        # the row's own creation time, which the database supplies, so it cannot be fooled
        # by a caller's clock.
        CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
        # Status and released_at are two views of one fact, so they cannot disagree.
        CheckConstraint(
            "(status = 'RELEASED') = (released_at IS NOT NULL)",
            name="released_at_matches_status",
        ),
        # One active reservation per checkout, structurally. The predicate is static: it
        # says nothing about expiry, because a predicate mentioning now() would not be
        # immutable and PostgreSQL will not index on one. Expiry is handled by the
        # accounting query instead, and the two together give the property that matters,
        # which is that reserved stock can never be counted twice for one checkout.
        Index(
            "uq_inventory_reservation_active_checkout",
            "checkout_id",
            unique=True,
            postgresql_where=text(ACTIVE_PREDICATE),
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
