"""Persistence access for spending mandates.

Concrete, not generic. The repository owns SQLAlchemy and does not commit: the caller
sets the transaction boundary, which is what lets a mandate and its audit event be one
unit of work.

There is deliberately no update method. Authorization fields are immutable, and the only
transition a mandate has is revocation.
"""

import uuid
from datetime import datetime

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.mandates.models import MandateStatus, SpendingMandate


def mandate_lock_statement(
    mandate_id: uuid.UUID, merchant_id: uuid.UUID
) -> Select[tuple[SpendingMandate]]:
    """The statement that locks one mandate, written where a test can read it.

    Merchant scoped, like every other read of this table. An authorization granted to somebody
    else is not locked and not returned, and the caller sees exactly what it would see for a
    mandate that does not exist.

    `FOR UPDATE` rather than a weaker mode. It is the only row lock that conflicts with
    every other one, including the `FOR KEY SHARE` a foreign key reference takes, so the
    guarantee does not depend on anybody rereading PostgreSQL's conflict matrix correctly
    later. A mandate is locked at most once per operation and released at commit, so the
    strictness costs a short wait and buys a rule with no exceptions.

    `populate_existing` is load bearing. Without it a mandate already in the session's
    identity map is returned with the attributes it was loaded with, so the row would be
    locked and then read stale, which is the exact failure the lock exists to prevent.
    """
    return (
        select(SpendingMandate)
        .where(SpendingMandate.id == mandate_id, SpendingMandate.merchant_id == merchant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


class MandateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        merchant_id: uuid.UUID,
        max_total_amount_minor: int,
        currency: str,
        valid_from: datetime,
        valid_until: datetime,
        max_quantity: int | None = None,
    ) -> SpendingMandate:
        """Write a new mandate and flush so that its server generated columns are set.

        A mandate is always created active. There is no parameter for status, so a
        revoked mandate cannot be brought into existence, only arrived at.
        """
        mandate = SpendingMandate(
            merchant_id=merchant_id,
            max_total_amount_minor=max_total_amount_minor,
            currency=currency,
            max_quantity=max_quantity,
            valid_from=valid_from,
            valid_until=valid_until,
            status=MandateStatus.ACTIVE,
        )
        self._session.add(mandate)
        await self._session.flush()
        return mandate

    async def get(self, mandate_id: uuid.UUID, *, merchant_id: uuid.UUID) -> SpendingMandate | None:
        """Fetch one merchant's mandate.

        There is no unscoped counterpart. Every way of reading an authorization out of this
        repository names the merchant it must have been granted to, so a caller cannot
        authenticate as one merchant and then read another merchant's mandate by identifier:
        the method that would allow it does not exist.

        The merchant is a condition in the SQL rather than a comparison afterwards, so a
        mandate granted to somebody else is absent rather than refused, and a caller learns
        nothing about whether it exists.
        """
        statement = select(SpendingMandate).where(
            SpendingMandate.id == mandate_id, SpendingMandate.merchant_id == merchant_id
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get_for_update(
        self, mandate_id: uuid.UUID, *, merchant_id: uuid.UUID
    ) -> SpendingMandate | None:
        """Fetch one merchant's mandate and hold it against every other transaction until
        commit.

        For an operation that is about to treat this mandate's status and validity window
        as authoritative. An unlocked read answers what was true when it was issued, and
        between that answer and the decision made from it a revocation can commit, which
        is how an authorization gets acted on after it was withdrawn.

        Every caller of this is either revoking the mandate or deciding something on the
        strength of it, so the two serialize: whichever arrives first finishes, and the
        other reads what that one left behind rather than what it found on the way in.

        First in the lock order. A caller that also needs the checkout or the variant rows
        takes this one before either. See agentrank_api.locking.

        Merchant scoped, exactly as the unlocked read is. Locking a row and then discovering it
        belongs to somebody else would mean a foreign caller could make a merchant's own
        request wait.
        """
        statement = mandate_lock_statement(mandate_id, merchant_id)
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def revoke(self, mandate: SpendingMandate) -> bool:
        """Revoke a mandate, and report whether this call is what changed it.

        Idempotent: revoking an already revoked mandate is not an error and does not move
        `revoked_at`. The return value exists so that the caller can append exactly one
        audit event for exactly one real transition.

        Idempotent only if the row was read under `get_for_update`. The decision below is
        made from the status this object was loaded with, so two revocations that both read
        an active mandate would both take the transition, and the second would move
        `revoked_at` and append a second event. The lock is what makes the second one read
        the first one's result instead.

        The timestamp comes from the database clock. Inside one transaction `now()` is
        the transaction time, so a revocation and the audit event recording it carry the
        same instant rather than two clock readings that merely look simultaneous.
        """
        if mandate.status is MandateStatus.REVOKED:
            return False

        mandate.status = MandateStatus.REVOKED
        mandate.revoked_at = func.now()
        await self._session.flush()
        # Explicitly reloaded rather than left expired. A SQL expression assigned to an
        # attribute is not readable until it is fetched back, and an implicit fetch
        # inside an async session raises MissingGreenlet.
        await self._session.refresh(mandate, ["revoked_at"])
        return True
