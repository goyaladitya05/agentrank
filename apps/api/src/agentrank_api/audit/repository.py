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
        credential_id: uuid.UUID | None = None,
    ) -> AuditEvent:
        """Record one event.

        Does not commit. The point of that is atomicity: the caller puts the state change
        and this event in one transaction, so a mandate cannot exist without the event
        that says it was created.

        `occurred_at` comes from the database clock rather than the process clock, so events
        written by several application instances are stamped by one clock rather than by
        several that disagree.

        It is append time, and specifically it is transaction time: `now()` in PostgreSQL is
        `transaction_timestamp()`, so every event written in one transaction carries the
        instant that transaction began. That is deliberate and it is what lets the trail show
        that a state change and the event recording it were one unit of work.

        It is not commit time, and this column is not commit order. A transaction that starts
        first and commits last stamps its event earlier than one that started later and
        committed first. Nothing here is commit order: version 7 identifiers and any counter
        added later are allocated when a row is written, not when its transaction commits.

        The rule that follows, and it matters most for the phase that adds payments: never
        infer that one thing happened before another from the relative order of these rows.
        Authoritative state and its locked transitions decide that. See docs/architecture.md.

        `credential_id` defaults to absent, and absent means nobody knows rather than nobody
        did. Two kinds of caller legitimately leave it unset: everything written before Phase
        1H, and the operator command line, which has no authenticated identity of any kind. A
        default was chosen over a required parameter because the alternative is every internal
        and operator path passing None explicitly, which reads as a decision being made where
        none is.

        The safety of that default is that it cannot grant anything. Attribution being absent
        weakens the evidence an event carries; it does not weaken the authorization that
        produced it, which was decided by merchant scoped SQL before this was ever called.
        """
        event = AuditEvent(
            merchant_id=merchant_id,
            actor_type=actor_type,
            credential_id=credential_id,
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
