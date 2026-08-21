"""Persistence access for checkouts.

The repository owns SQLAlchemy and does not commit: the caller sets the transaction
boundary, which is what lets a checkout, its lines and its audit event be one unit of
work.

There is deliberately no update method and no way to add a line to an existing checkout.
A quote is written once. The only transition it has is cancellation.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from agentrank_api.checkout.models import CheckoutLine, CheckoutSession, CheckoutStatus
from agentrank_api.checkout.quote import QuotedLine, checkout_totals


class CheckoutRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        merchant_id: uuid.UUID,
        mandate_id: uuid.UUID,
        currency: str,
        lines: Sequence[QuotedLine],
        expires_at: datetime,
        shipping_amount_minor: int = 0,
        discount_amount_minor: int = 0,
    ) -> CheckoutSession:
        """Write a quote and its lines, and flush so that generated columns are set.

        The totals are derived here rather than accepted from the caller, so a checkout
        whose total disagrees with its own lines is not reachable through this repository.
        The database enforces the row local half of the same identity; the cross row sum
        is the half it cannot see.

        A checkout is always created open. There is no parameter for status, so a
        cancelled checkout cannot be brought into existence, only arrived at.

        The semantic snapshot on each line is copied out of the quoted line rather than
        read from the catalog here. This repository writes what it is given; deciding what
        the catalog said is the service's job, and it does it once.
        """
        totals = checkout_totals(
            lines,
            shipping_amount_minor=shipping_amount_minor,
            discount_amount_minor=discount_amount_minor,
        )
        checkout = CheckoutSession(
            merchant_id=merchant_id,
            mandate_id=mandate_id,
            currency=currency,
            subtotal_amount_minor=totals.subtotal_amount_minor,
            shipping_amount_minor=totals.shipping_amount_minor,
            discount_amount_minor=totals.discount_amount_minor,
            total_amount_minor=totals.total_amount_minor,
            status=CheckoutStatus.OPEN,
            expires_at=expires_at,
        )
        checkout.lines = [
            CheckoutLine(
                merchant_id=merchant_id,
                variant_id=line.variant_id,
                quantity=line.quantity,
                unit_price_amount_minor=line.unit_price_amount_minor,
                currency=currency,
                product_category=line.product_category,
                # Copied rather than referenced. The source is a live `Variant.attributes`
                # dictionary, and sharing the object would let a later catalog edit reach
                # into a written quote through the ORM identity map.
                variant_attributes=dict(line.variant_attributes),
            )
            for line in lines
        ]
        self._session.add(checkout)
        await self._session.flush()
        return checkout

    async def get(self, checkout_id: uuid.UUID) -> CheckoutSession | None:
        """Fetch one checkout with every line loaded, in the order they were quoted.

        The lines are loaded eagerly and completely. Authorization sums their quantities,
        and a collection that is loaded lazily or partially would make that sum depend on
        how the object happened to be fetched.
        """
        statement = (
            select(CheckoutSession)
            .options(selectinload(CheckoutSession.lines))
            .where(CheckoutSession.id == checkout_id)
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def cancel(self, checkout: CheckoutSession) -> bool:
        """Cancel a checkout, and report whether this call is what changed it.

        Idempotent: cancelling an already cancelled checkout is not an error and does not
        move `cancelled_at`. The return value exists so that the caller can append exactly
        one audit event for exactly one real transition.

        The timestamp comes from the database clock. Inside one transaction `now()` is the
        transaction time, so a cancellation and the event recording it carry the same
        instant rather than two clock readings that merely look simultaneous.
        """
        if checkout.status is CheckoutStatus.CANCELLED:
            return False

        checkout.status = CheckoutStatus.CANCELLED
        checkout.cancelled_at = func.now()
        await self._session.flush()
        # Explicitly reloaded rather than left expired. A SQL expression assigned to an
        # attribute is not readable until it is fetched back, and an implicit fetch inside
        # an async session raises MissingGreenlet.
        await self._session.refresh(checkout, ["cancelled_at"])
        return True
