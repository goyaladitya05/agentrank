"""Persistence access for audit events.

Append and read. There is deliberately no `update_audit_event` and no
`delete_audit_event`: an event that can be edited is not an audit trail. The database
refuses both as well, so this is a contract rather than a convention.

Reads are bounded. An audit log is the one table that only grows, and an endpoint that
could return all of it would be a way to dump every action a merchant ever took.
"""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.audit.models import ActorType, AuditEvent

DEFAULT_AUDIT_LIMIT = 50
MAX_AUDIT_LIMIT = 200


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(
        self,
        *,
        merchant_id: uuid.UUID,
        actor_type: ActorType,
        event_type: str,
        resource_type: str,
        resource_id: uuid.UUID,
        payload: dict[str, Any],
    ) -> AuditEvent:
        """Record one event.

        Does not commit. The point of that is atomicity: the caller puts the state change
        and this event in one transaction, so a mandate cannot exist without the event
        that says it was created.

        `occurred_at` comes from the database clock, so events from several application
        instances order against each other rather than against their own clocks.
        """
        event = AuditEvent(
            merchant_id=merchant_id,
            actor_type=actor_type,
            event_type=event_type,
            resource_type=resource_type,
            resource_id=resource_id,
            payload=payload,
        )
        self._session.add(event)
        await self._session.flush()
        return event

    async def list_for_resource(
        self,
        *,
        resource_type: str,
        resource_id: uuid.UUID,
        limit: int = DEFAULT_AUDIT_LIMIT,
    ) -> Sequence[AuditEvent]:
        """Everything recorded about one resource, oldest first."""
        statement = (
            select(AuditEvent)
            .where(
                AuditEvent.resource_type == resource_type,
                AuditEvent.resource_id == resource_id,
            )
            .order_by(AuditEvent.occurred_at, AuditEvent.id)
            .limit(_bounded(limit))
        )
        return (await self._session.execute(statement)).scalars().all()

    async def list_for_merchant(
        self, merchant_id: uuid.UUID, *, limit: int = DEFAULT_AUDIT_LIMIT
    ) -> Sequence[AuditEvent]:
        """Everything recorded for one merchant, oldest first."""
        statement = (
            select(AuditEvent)
            .where(AuditEvent.merchant_id == merchant_id)
            .order_by(AuditEvent.occurred_at, AuditEvent.id)
            .limit(_bounded(limit))
        )
        return (await self._session.execute(statement)).scalars().all()


def _bounded(limit: int) -> int:
    """Order is total, so a bounded read is a stable prefix rather than an arbitrary one.

    Events share an `occurred_at` when they are written in one transaction, which is why
    the identifier is the tiebreak: version 7 identifiers are time ordered, so the
    sequence matches the order the events were appended in.
    """
    return min(max(limit, 1), MAX_AUDIT_LIMIT)
