"""Audit event persistence.

An audit event answers five questions and no others: what happened, when, who or what
caused it, which resource it affected, and what structured data went with it. It is not
an application log. Nothing here is a place to put a message a human wrote.

The table is append only. That is enforced at two levels: the application exposes no
update and no delete, and a trigger rejects both at the database. See docs/security.md.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, Index, String, Uuid, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from agentrank_api.models import Base


class ActorType(StrEnum):
    """Who or what caused an event.

    Three values, because three are real. `PAYMENT_PROVIDER` was added when a payment
    provider first existed and could actually appear here: a payment outcome is reported by
    the provider, not decided by this application, and attributing it to the buyer would
    claim the buyer chose whether their card was declined. `BUYER_AGENT`, `POLICY_ENGINE` and
    `MERCHANT` are still absent for the same reason they always were.
    """

    SYSTEM = "SYSTEM"
    BUYER = "BUYER"
    PAYMENT_PROVIDER = "PAYMENT_PROVIDER"


# Event types are lowercase dotted identifiers such as `mandate.created`. They are stable
# machine readable names, never display text, and they are code constants rather than
# anything a caller supplies.
EVENT_TYPE_PATTERN = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$"

ACTOR_TYPE = Enum(
    ActorType,
    native_enum=False,
    create_constraint=False,
    validate_strings=True,
    length=16,
    name="actor_type",
)

_ACTOR_VALUES = ", ".join(f"'{actor.value}'" for actor in ActorType)


class AuditEvent(Base):
    """One thing that happened, recorded once and never touched again.

    There is no `updated_at` and no soft delete flag, because there is no update and no
    delete. The row is the record.

    `resource_id` is a UUID rather than free text: every resource in this system has one,
    and a column that could hold anything would eventually hold a string nobody can join
    on.
    """

    __tablename__ = "audit_event"
    __table_args__ = (
        CheckConstraint(f"actor_type IN ({_ACTOR_VALUES})", name="actor_type_known"),
        CheckConstraint(f"event_type ~ '{EVENT_TYPE_PATTERN}'", name="event_type_format"),
        CheckConstraint("length(btrim(resource_type)) > 0", name="resource_type_not_blank"),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="payload_is_an_object"),
        # The two ways an event is ever read: everything about one resource, and
        # everything a merchant did. Both are time ordered.
        Index(None, "merchant_id", "occurred_at"),
        Index(None, "resource_type", "resource_id", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid7)
    # RESTRICT for the same reason as the mandate: history must not be removed as a side
    # effect of deleting the merchant it describes.
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("merchant.id", ondelete="RESTRICT"),
        nullable=False,
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    actor_type: Mapped[ActorType] = mapped_column(ACTOR_TYPE, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
