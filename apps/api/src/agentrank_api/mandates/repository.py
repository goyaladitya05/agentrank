"""Persistence access for spending mandates.

Concrete, not generic. The repository owns SQLAlchemy and does not commit: the caller
sets the transaction boundary, which is what lets a mandate and its audit event be one
unit of work.

There is deliberately no update method. Authorization fields are immutable, and the only
transition a mandate has is revocation.
"""

import uuid
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.mandates.models import MandateStatus, SpendingMandate


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

    async def get(self, mandate_id: uuid.UUID) -> SpendingMandate | None:
        return await self._session.get(SpendingMandate, mandate_id)

    async def revoke(self, mandate: SpendingMandate) -> bool:
        """Revoke a mandate, and report whether this call is what changed it.

        Idempotent: revoking an already revoked mandate is not an error and does not move
        `revoked_at`. The return value exists so that the caller can append exactly one
        audit event for exactly one real transition.

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
