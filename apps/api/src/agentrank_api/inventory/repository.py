"""Persistence access for inventory reservations.

The repository owns SQLAlchemy and does not commit: the caller sets the transaction
boundary, which is what lets a reservation, its lines and its audit event be one unit of
work.

There is deliberately no update method and no way to add a line to an existing
reservation. A reservation is written once. The only transition it has is release.
"""

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from agentrank_api.inventory.models import (
    InventoryReservation,
    InventoryReservationLine,
    ReservationStatus,
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

    async def get_active_for_checkout(self, checkout_id: uuid.UUID) -> InventoryReservation | None:
        """Fetch the one active reservation for a checkout, if there is one.

        At most one exists: a partial unique index says so. Expiry is not filtered here on
        purpose. Whether an active reservation is still effective depends on the instant
        being asked about, and that instant belongs to the caller rather than to a query.
        """
        statement = (
            select(InventoryReservation)
            .options(selectinload(InventoryReservation.lines))
            .where(
                InventoryReservation.checkout_id == checkout_id,
                InventoryReservation.status == ReservationStatus.ACTIVE,
            )
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

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

    async def release(self, reservation: InventoryReservation) -> bool:
        """Release a reservation, and report whether this call is what changed it.

        Idempotent: releasing an already released reservation is not an error and does not
        move `released_at`. The return value exists so that the caller can append exactly
        one audit event for exactly one real transition.

        The timestamp comes from the database clock. Inside one transaction `now()` is the
        transaction time, so a release and the event recording it carry the same instant
        rather than two clock readings that merely look simultaneous.
        """
        if reservation.status is ReservationStatus.RELEASED:
            return False

        reservation.status = ReservationStatus.RELEASED
        reservation.released_at = func.now()
        await self._session.flush()
        # Explicitly reloaded rather than left expired. A SQL expression assigned to an
        # attribute is not readable until it is fetched back, and an implicit fetch inside
        # an async session raises MissingGreenlet.
        await self._session.refresh(reservation, ["released_at"])
        return True
