"""Persistence access for checkouts.

The repository owns SQLAlchemy and does not commit: the caller sets the transaction
boundary, which is what lets a checkout, its lines and its audit event be one unit of
work.

There is deliberately no update method and no way to add a line to an existing checkout.
A quote is written once, and the only things that ever change on it are its two terminal
transitions: cancellation, which withdraws it, and payment, which completes it.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from agentrank_api.checkout.models import CheckoutLine, CheckoutSession, CheckoutStatus
from agentrank_api.checkout.quote import QuotedLine, checkout_totals


def checkout_lock_statement(
    checkout_id: uuid.UUID, merchant_id: uuid.UUID
) -> Select[tuple[CheckoutSession]]:
    """The statement that locks one checkout, written where a test can read it.

    Merchant scoped, like every other read of this table. A quote belonging to somebody else is
    not locked and not returned, and the caller sees exactly what it would see for a checkout
    that does not exist.

    `FOR UPDATE` rather than a weaker mode, and specifically rather than relying on the
    reservation's foreign key. Inserting a reservation takes `FOR KEY SHARE` on the
    checkout it names, and a cancellation's ordinary update takes `FOR NO KEY UPDATE`.
    Those two modes do not conflict, so the foreign key lets a reservation be written
    against a checkout another transaction is in the middle of cancelling. `FOR UPDATE` is
    the one mode that conflicts with all of them.

    The lines are loaded by a second statement rather than an outer join, so the lock
    applies to the checkout row alone. That is the row the status and the expiry live on,
    and the lines are immutable at the database anyway.

    `populate_existing` is load bearing. Without it a checkout already in the session's
    identity map is returned with the attributes it was loaded with, so the row would be
    locked and then read stale, which is the exact failure the lock exists to prevent.
    """
    return (
        select(CheckoutSession)
        .options(selectinload(CheckoutSession.lines))
        .where(CheckoutSession.id == checkout_id, CheckoutSession.merchant_id == merchant_id)
        .with_for_update(of=CheckoutSession)
        .execution_options(populate_existing=True)
    )


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

    async def get(
        self, checkout_id: uuid.UUID, *, merchant_id: uuid.UUID
    ) -> CheckoutSession | None:
        """Fetch one merchant's checkout, with every line loaded in the order they were quoted.

        There is no unscoped counterpart, and that is the point rather than an inconvenience.
        Every way of reading a quote out of this repository names the merchant it must belong
        to, so authorizing at a route and then reading by identifier is not a mistake anybody
        can make here: the method that would allow it does not exist.

        The merchant is a condition in the SQL rather than a comparison afterwards. A quote
        belonging to somebody else is simply absent from the result, so the caller cannot
        distinguish it from one that was never written, and does not have to remember to.

        The lines are loaded eagerly and completely. Authorization sums their quantities,
        and a collection that is loaded lazily or partially would make that sum depend on
        how the object happened to be fetched.
        """
        statement = (
            select(CheckoutSession)
            .options(selectinload(CheckoutSession.lines))
            .where(CheckoutSession.id == checkout_id, CheckoutSession.merchant_id == merchant_id)
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get_for_update(
        self, checkout_id: uuid.UUID, *, merchant_id: uuid.UUID
    ) -> CheckoutSession | None:
        """Fetch one merchant's checkout and hold it against every other transaction until
        commit.

        For an operation that is about to treat this checkout's status and expiry as
        authoritative, or about to change them. An unlocked read answers what was true
        when it was issued, and between that answer and the decision made from it a
        cancellation can commit, which is how stock gets held for a withdrawn quote.

        Every caller of this is either cancelling the checkout or deciding something on
        the strength of it, so the two serialize: whichever arrives first finishes, and the
        other reads what that one left behind rather than what it found on the way in.

        Second in the lock order, after the mandate and before the variant rows. See
        agentrank_api.locking.

        Merchant scoped, exactly as the unlocked read is. Locking a row and then discovering it
        belongs to somebody else would mean a foreign caller could make a merchant's own
        request wait.
        """
        statement = checkout_lock_statement(checkout_id, merchant_id)
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def mark_paid(self, checkout: CheckoutSession) -> bool:
        """Record that this quote became a purchase, and report whether this call changed it.

        The only transition into PAID, called by the only operation allowed to make it: the
        transaction that records a definitive provider success. It is terminal and the database
        agrees, so there is no counterpart that reopens one and no path from PAID to CANCELLED.

        Idempotent, and only if the row was read under `get_for_update`. The decision below is
        made from the status this object was loaded with.

        Refuses anything that is not OPEN rather than silently ignoring it. A cancelled quote
        becoming paid would mean money moved for something that was withdrawn, and the trigger
        refuses it too.

        The timestamp comes from the database clock, so the transition and the event recording
        it carry the same instant.
        """
        if checkout.status is CheckoutStatus.PAID:
            return False
        if checkout.status is not CheckoutStatus.OPEN:
            raise ValueError(f"a {checkout.status.value.lower()} checkout cannot be paid")

        checkout.status = CheckoutStatus.PAID
        checkout.paid_at = func.now()
        await self._session.flush()
        # Explicitly reloaded rather than left expired. A SQL expression assigned to an
        # attribute is not readable until it is fetched back, and an implicit fetch inside an
        # async session raises MissingGreenlet.
        await self._session.refresh(checkout, ["paid_at"])
        return True

    async def cancel(self, checkout: CheckoutSession) -> bool:
        """Cancel a checkout, and report whether this call is what changed it.

        Idempotent: cancelling an already cancelled checkout is not an error and does not
        move `cancelled_at`. The return value exists so that the caller can append exactly
        one audit event for exactly one real transition.

        Idempotent only if the row was read under `get_for_update`. The decision below is
        made from the status this object was loaded with, so two cancellations that both
        read an open checkout would both take the transition, and the second would move
        `cancelled_at` and append a second event. The lock is what makes the second one
        read the first one's result instead.

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
