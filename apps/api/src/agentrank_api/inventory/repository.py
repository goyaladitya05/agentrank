"""Persistence access for inventory reservations.

The repository owns SQLAlchemy and does not commit: the caller sets the transaction
boundary, which is what lets a reservation, its lines and its audit event be one unit of
work.

There is deliberately no update method and no way to add a line to an existing
reservation. A reservation is written once, and the only things that ever change on it are
the three lifecycle transitions written out below: commitment to a payment, release, and
consumption by a successful purchase. Each is its own method, each reports whether it is
what changed the row, and none of them takes a status from a caller.
"""

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from agentrank_api.commerce.models import Variant
from agentrank_api.inventory.models import (
    HOLDING_STATUSES,
    InventoryReservation,
    InventoryReservationLine,
    ReservationStatus,
)


def reservation_lock_statement(reservation_id: uuid.UUID) -> Select[tuple[InventoryReservation]]:
    """The statement that locks one reservation, written where a test can read it.

    For an operation about to change what a hold means: binding it to a payment, giving it
    back, or consuming it. `FOR UPDATE` for the same reason every other lock here is: it is
    the one mode that conflicts with all the others, including the `FOR KEY SHARE` a payment
    attempt's foreign key takes when it names this row.

    The lines are loaded by a second statement rather than an outer join, so the lock applies
    to the reservation row alone. The lines are immutable at the database anyway.

    `populate_existing` is load bearing. Without it a reservation already in the session's
    identity map is returned with the attributes it was loaded with, so the row would be
    locked and then read stale, which is the exact failure the lock exists to prevent.

    Fourth in the lock order, after the variant rows and before the payment attempt. See
    agentrank_api.locking.
    """
    return (
        select(InventoryReservation)
        .options(selectinload(InventoryReservation.lines))
        .where(InventoryReservation.id == reservation_id)
        .with_for_update(of=InventoryReservation)
        .execution_options(populate_existing=True)
    )


def variant_lock_statement(
    *, merchant_id: uuid.UUID, variant_ids: Sequence[uuid.UUID]
) -> Select[tuple[uuid.UUID, int]]:
    """The statement that takes the locks, written where a test can read it.

    The ordering is in the SQL rather than only in the parameter list, because locks are
    taken in the order the plan returns rows and the planner decides that order. A Python
    side sort alone would not survive it choosing another one.
    """
    return (
        select(Variant.id, Variant.inventory_quantity)
        .where(Variant.merchant_id == merchant_id, Variant.id.in_(variant_ids))
        .order_by(Variant.id)
        .with_for_update()
    )


class InventoryReservationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        merchant_id: uuid.UUID,
        checkout_id: uuid.UUID,
        expires_at: datetime,
        quantities: Mapping[uuid.UUID, int],
    ) -> InventoryReservation:
        """Write a reservation and its lines, and flush so that generated columns are set.

        A reservation is always created active. There is no parameter for status, so a
        released reservation cannot be brought into existence, only arrived at.

        `expires_at` is passed in rather than derived here because deriving it needs the
        checkout and the mandate, which is the service's business. This writes what it is
        given, and the database refuses an expiry that has already passed.
        """
        if not quantities:
            raise ValueError("a reservation must hold at least one line")

        reservation = InventoryReservation(
            merchant_id=merchant_id,
            checkout_id=checkout_id,
            status=ReservationStatus.ACTIVE,
            expires_at=expires_at,
        )
        reservation.lines = [
            InventoryReservationLine(
                merchant_id=merchant_id,
                variant_id=variant_id,
                quantity=quantity,
            )
            # Sorted so that two reservations written in one transaction insert their lines
            # in one order, which is the same reason variant locks are taken in order.
            for variant_id, quantity in sorted(quantities.items(), key=lambda item: str(item[0]))
        ]
        self._session.add(reservation)
        await self._session.flush()
        return reservation

    async def get(self, reservation_id: uuid.UUID) -> InventoryReservation | None:
        """Fetch one reservation with every line loaded."""
        statement = (
            select(InventoryReservation)
            .options(selectinload(InventoryReservation.lines))
            .where(InventoryReservation.id == reservation_id)
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get_for_update(self, reservation_id: uuid.UUID) -> InventoryReservation | None:
        """Fetch one reservation and hold it against every other transaction until commit."""
        statement = reservation_lock_statement(reservation_id)
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get_holding_for_checkout(self, checkout_id: uuid.UUID) -> InventoryReservation | None:
        """Fetch the one reservation still holding stock for a checkout, if there is one.

        Holding means ACTIVE or COMMITTED. At most one exists: a partial unique index says
        so. Expiry is not filtered here on purpose. Whether an ACTIVE reservation is still
        effective depends on the instant being asked about, and that instant belongs to the
        caller rather than to a query. A COMMITTED one is effective whatever the instant.
        """
        statement = (
            select(InventoryReservation)
            .options(selectinload(InventoryReservation.lines))
            .where(
                InventoryReservation.checkout_id == checkout_id,
                InventoryReservation.status.in_(HOLDING_STATUSES),
            )
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get_holding_for_checkout_for_update(
        self, checkout_id: uuid.UUID
    ) -> InventoryReservation | None:
        """The same reservation, held against every other transaction until commit.

        For an operation that is about to change what this hold means. Two statements rather
        than one locking read: the identifier is found first, then the row is locked by
        primary key, so the lock statement is the same one every other caller uses and the
        `FOR UPDATE` never has to be reasoned about beside a status predicate that another
        transaction may be changing.
        """
        found = await self.get_holding_for_checkout(checkout_id)
        if found is None:
            return None
        return await self.get_for_update(found.id)

    async def list_for_checkout(self, checkout_id: uuid.UUID) -> Sequence[InventoryReservation]:
        """Every reservation ever written for a checkout, oldest first.

        Version 7 identifiers are time ordered, so this is the order they were created in.
        """
        statement = (
            select(InventoryReservation)
            .options(selectinload(InventoryReservation.lines))
            .where(InventoryReservation.checkout_id == checkout_id)
            .order_by(InventoryReservation.id)
        )
        return (await self._session.execute(statement)).scalars().all()

    async def list_holding_for_merchant(
        self, merchant_id: uuid.UUID
    ) -> Sequence[InventoryReservation]:
        """Every reservation still holding one merchant's stock, oldest first.

        Holding means ACTIVE or COMMITTED, the same predicate `get_holding_for_checkout` uses,
        and expiry is deliberately not filtered here for the same reason: whether an ACTIVE
        reservation is still effective depends on the instant being asked about, and that
        instant belongs to the caller.

        Merchant scoped rather than global. The one caller is benchmark world preparation,
        which gives back everything an earlier mission left held so that the next mission sees
        the stock the fixture describes, and a preparation that could reach across merchants
        would be a preparation that could free somebody else's inventory.
        """
        statement = (
            select(InventoryReservation)
            .options(selectinload(InventoryReservation.lines))
            .where(
                InventoryReservation.merchant_id == merchant_id,
                InventoryReservation.status.in_(HOLDING_STATUSES),
            )
            .order_by(InventoryReservation.id)
        )
        return (await self._session.execute(statement)).scalars().all()

    async def lock_variants(
        self, *, merchant_id: uuid.UUID, variant_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        """Take a row lock on each variant and report the stock each one holds.

        This is the whole of the concurrency answer. Two preparations that need the same
        variant cannot both pass the availability check, because the second one waits here
        until the first has committed and then reads the row again. Nothing in Python
        arbitrates it: a process local lock would not survive a second worker, and a check
        outside a transaction would be a read that something else can invalidate before the
        write lands.

        The lock order is `variant.id` ascending, stated in SQL rather than implied by the
        parameter order, because the planner decides what order rows come back in and the
        locks are taken in that output order. Two preparations sharing two variants
        therefore queue behind each other in the same sequence rather than each holding
        what the other wants next.

        `FOR UPDATE` rather than a weaker mode: the point is that no other transaction may
        hold or take a conflicting lock on these rows while availability is being decided.

        It lives here rather than on `CatalogRepository` because it is not a catalog read.
        It is one step of the reservation operation, and separating the lock from the
        accounting it protects is how a lock ends up being forgotten.
        """
        if not variant_ids:
            return {}

        statement = variant_lock_statement(merchant_id=merchant_id, variant_ids=variant_ids)
        rows = (await self._session.execute(statement)).all()
        return {row.id: row.inventory_quantity for row in rows}

    async def effective_reserved_quantities(
        self, *, variant_ids: Sequence[uuid.UUID], at: datetime
    ) -> dict[uuid.UUID, int]:
        """How many units of each variant are held by reservations effective at `at`.

        Effective means ACTIVE and not yet expired, or COMMITTED at any time. Expiry is a
        comparison against `at` rather than a status anyone had to write, which is what lets
        an expired reservation stop consuming capacity with no sweeper job whose failure
        would hold stock forever.

        A CONSUMED reservation contributes nothing, and that is the whole of the answer to
        double subtraction: the units it held have already been taken out of the variant
        total, so counting them here as well would remove one purchase twice.

        A variant nobody has reserved is absent from the result rather than present as
        zero, so the caller reads it with a default. Meaning is the same and the query does
        not have to invent rows.
        """
        if not variant_ids:
            return {}
        if at.tzinfo is None:
            raise ValueError("evaluation time must be timezone aware")

        statement = (
            select(
                InventoryReservationLine.variant_id,
                func.sum(InventoryReservationLine.quantity),
            )
            .join(
                InventoryReservation,
                InventoryReservation.id == InventoryReservationLine.reservation_id,
            )
            .where(
                InventoryReservationLine.variant_id.in_(variant_ids),
                # The two ways a reservation holds stock, and only one of them is expiry
                # governed. A COMMITTED reservation is bound to a payment that was admitted
                # while everything was valid, so it keeps holding its units until that
                # payment has a definitive answer, however long that takes.
                or_(
                    and_(
                        InventoryReservation.status == ReservationStatus.ACTIVE,
                        InventoryReservation.expires_at > at,
                    ),
                    InventoryReservation.status == ReservationStatus.COMMITTED,
                ),
            )
            .group_by(InventoryReservationLine.variant_id)
        )
        return {
            variant_id: int(quantity)
            for variant_id, quantity in (await self._session.execute(statement)).all()
        }

    async def commit_to_payment(self, reservation: InventoryReservation) -> bool:
        """Bind a hold to an admitted payment, and report whether this call changed it.

        ACTIVE becomes COMMITTED and nothing else does. A reservation that is already
        COMMITTED is left alone and reported as unchanged, which is what makes a repeated
        admission of the same payment identity write no second transition. A RELEASED or
        CONSUMED one is refused rather than quietly ignored: binding a payment to stock that
        has been given back or already sold is not an idempotent repeat, it is a mistake, and
        the database trigger refuses it too.

        No timestamp is stamped. The instant this hold became committed is the admission
        instant, which is `payment_attempt.created_at`, and writing it twice would be two
        answers to one question.

        Idempotent only if the row was read under a lock. The decision below is made from the
        status this object was loaded with.
        """
        if reservation.status is ReservationStatus.COMMITTED:
            return False
        if reservation.status is not ReservationStatus.ACTIVE:
            raise ValueError(
                f"a {reservation.status.value} reservation cannot be committed to a payment"
            )

        reservation.status = ReservationStatus.COMMITTED
        await self._session.flush()
        return True

    async def release(self, reservation: InventoryReservation) -> bool:
        """Release a reservation, and report whether this call is what changed it.

        Idempotent: releasing an already released reservation is not an error and does not
        move `released_at`. The return value exists so that the caller can append exactly
        one audit event for exactly one real transition.

        A COMMITTED reservation may be released, and that is the definitive decline path: the
        payment it was bound to failed, no money moved, and the stock goes back on the shelf.
        A CONSUMED one may not, because those units have been sold.

        The timestamp comes from the database clock. Inside one transaction `now()` is the
        transaction time, so a release and the event recording it carry the same instant
        rather than two clock readings that merely look simultaneous.
        """
        if reservation.status is ReservationStatus.RELEASED:
            return False
        if reservation.status is ReservationStatus.CONSUMED:
            raise ValueError("a consumed reservation cannot be released")

        reservation.status = ReservationStatus.RELEASED
        reservation.released_at = func.now()
        await self._session.flush()
        # Explicitly reloaded rather than left expired. A SQL expression assigned to an
        # attribute is not readable until it is fetched back, and an implicit fetch inside
        # an async session raises MissingGreenlet.
        await self._session.refresh(reservation, ["released_at"])
        return True

    async def consume(self, reservation: InventoryReservation) -> bool:
        """Mark a hold as permanently sold, and report whether this call changed it.

        Only a COMMITTED reservation can be consumed, because consumption is what a
        successful payment does and a payment is only ever admitted against a commitment.
        Consuming an already consumed reservation is not an error and does not move
        `consumed_at`, so a reconciliation that arrives twice records one sale.

        This marks the reservation. Taking the units out of `variant.inventory_quantity` is
        `consume_stock` below, and the two happen in one transaction. Splitting them here
        rather than fusing them keeps each statement readable next to the locks it needs.
        """
        if reservation.status is ReservationStatus.CONSUMED:
            return False
        if reservation.status is not ReservationStatus.COMMITTED:
            raise ValueError(
                f"a {reservation.status.value} reservation cannot be consumed by a payment"
            )

        reservation.status = ReservationStatus.CONSUMED
        reservation.consumed_at = func.now()
        await self._session.flush()
        await self._session.refresh(reservation, ["consumed_at"])
        return True

    async def consume_stock(
        self, *, merchant_id: uuid.UUID, quantities: Mapping[uuid.UUID, int]
    ) -> dict[uuid.UUID, int]:
        """Take units permanently out of the variant totals, and report the shortfall.

        The only place in this application that ever writes `variant.inventory_quantity`. It
        is a decrement rather than a set, so it cannot silently overwrite a merchant edit it
        never read, and it runs with the variant rows locked, which the caller has already
        taken in ascending identifier order.

        The rows are loaded and assigned through the ORM rather than updated in place by a
        Core statement, so `updated_at` is refreshed the same way every other catalog write
        refreshes it.

        Underflow is clamped at zero and reported rather than hidden. A total below the
        quantity held by committed reservations should be unreachable: reservation accounting
        subtracts every effective hold before allowing another, and nothing in this
        application writes stock. It becomes reachable the day a merchant inventory endpoint
        exists, and the honest answer then is that the money moved and the merchant is
        oversold, which is a fact worth surfacing rather than a negative number worth
        forbidding. The caller records the shortfall in the audit trail. See
        docs/architecture.md.
        """
        if not quantities:
            return {}

        statement = (
            select(Variant)
            .where(Variant.merchant_id == merchant_id, Variant.id.in_(list(quantities)))
            .order_by(Variant.id)
            .with_for_update()
            # Load bearing, for the same reason it is on every other locking read here. A
            # variant already in the session's identity map would otherwise be returned with
            # the stock level it was loaded with, and the decrement would be applied to a
            # number the lock was taken precisely to stop trusting.
            .execution_options(populate_existing=True)
        )
        variants = (await self._session.execute(statement)).scalars().all()
        found = {variant.id: variant for variant in variants}

        shortfalls: dict[uuid.UUID, int] = {}
        for variant_id, quantity in sorted(quantities.items(), key=lambda item: str(item[0])):
            variant = found.get(variant_id)
            if variant is None:
                # Not reachable: the reservation line's composite foreign key onto variant is
                # RESTRICT, so the row it names exists. Counted as a total shortfall rather
                # than skipped, because stock that cannot be found is not stock that was
                # sold without incident.
                shortfalls[variant_id] = quantity
                continue
            taken = min(quantity, variant.inventory_quantity)
            variant.inventory_quantity -= taken
            if taken < quantity:
                shortfalls[variant_id] = quantity - taken

        await self._session.flush()
        return shortfalls
