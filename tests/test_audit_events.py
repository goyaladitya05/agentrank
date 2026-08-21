"""The audit trail is append only, and the database is what makes that true.

The application offering no update and no delete is the contract. These tests check the
enforcement underneath it, because a guarantee that depends on nobody writing the wrong
function is not a guarantee.
"""

import uuid
from typing import Any, cast

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.audit.models import ActorType, AuditEvent
from agentrank_api.audit.repository import AuditRepository
from agentrank_api.commerce.models import Merchant
from agentrank_api.commerce.repository import MerchantRepository

pytestmark = pytest.mark.anyio

RESOURCE = "spending_mandate"


@pytest.fixture
async def merchant(session: AsyncSession) -> Merchant:
    created = await MerchantRepository(session).create(slug="ampere-supply", name="Ampere Supply")
    await session.commit()
    return created


async def append(
    session: AsyncSession,
    merchant: Merchant,
    resource_id: uuid.UUID,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> AuditEvent:
    return await AuditRepository(session).append(
        merchant_id=merchant.id,
        actor_type=ActorType.SYSTEM,
        event_type=event_type,
        resource_type=RESOURCE,
        resource_id=resource_id,
        payload=payload if payload is not None else {},
    )


async def test_an_event_persists_with_its_structured_payload(
    session: AsyncSession, merchant: Merchant
) -> None:
    mandate_id = uuid.uuid7()

    await append(
        session,
        merchant,
        mandate_id,
        "mandate.created",
        {"max_total_amount_minor": 500000, "currency": "INR", "max_quantity": 1},
    )
    await session.commit()
    session.expunge_all()

    events = await AuditRepository(session).list_for_resource(
        resource_type=RESOURCE, resource_id=mandate_id
    )
    assert len(events) == 1
    event = events[0]
    assert event.actor_type is ActorType.SYSTEM
    assert event.event_type == "mandate.created"
    assert event.occurred_at.tzinfo is not None
    # A JSON object, not a rendered string. Reading it back needs no parsing.
    assert event.payload == {
        "max_total_amount_minor": 500000,
        "currency": "INR",
        "max_quantity": 1,
    }


async def test_events_for_a_resource_come_back_oldest_first_and_bounded(
    session: AsyncSession, merchant: Merchant
) -> None:
    """Written in one transaction, so they share an occurred_at and the identifier
    decides. Version 7 identifiers are time ordered, which is what makes that a real
    order rather than an arbitrary one."""
    mandate_id = uuid.uuid7()
    for event_type in ("mandate.created", "mandate.revoked"):
        await append(session, merchant, mandate_id, event_type)
    await append(session, merchant, uuid.uuid7(), "mandate.created")
    await session.commit()

    repository = AuditRepository(session)
    events = await repository.list_for_resource(resource_type=RESOURCE, resource_id=mandate_id)
    assert [event.event_type for event in events] == ["mandate.created", "mandate.revoked"]

    bounded = await repository.list_for_resource(
        resource_type=RESOURCE, resource_id=mandate_id, limit=1
    )
    assert [event.event_type for event in bounded] == ["mandate.created"]


async def test_events_are_scoped_to_their_merchant(
    session: AsyncSession, merchant: Merchant
) -> None:
    other = await MerchantRepository(session).create(slug="voltline-parts", name="Voltline Parts")
    await append(session, merchant, uuid.uuid7(), "mandate.created")
    await AuditRepository(session).append(
        merchant_id=other.id,
        actor_type=ActorType.BUYER,
        event_type="mandate.created",
        resource_type=RESOURCE,
        resource_id=uuid.uuid7(),
        payload={},
    )
    await session.commit()

    events = await AuditRepository(session).list_for_merchant(merchant.id)
    assert [event.merchant_id for event in events] == [merchant.id]


async def test_an_event_cannot_be_edited(session: AsyncSession, merchant: Merchant) -> None:
    event = await append(session, merchant, uuid.uuid7(), "mandate.created", {"currency": "INR"})
    await session.commit()

    event.payload = {"currency": "USD"}
    with pytest.raises(DBAPIError, match="append only"):
        await session.flush()


async def test_an_event_cannot_be_deleted(session: AsyncSession, merchant: Merchant) -> None:
    event = await append(session, merchant, uuid.uuid7(), "mandate.created")
    await session.commit()

    await session.delete(event)
    with pytest.raises(DBAPIError, match="append only"):
        await session.flush()


async def test_a_payload_that_is_not_an_object_is_rejected(
    session: AsyncSession, merchant: Merchant
) -> None:
    """The cast stands in for a writer that does not go through the typed repository."""
    with pytest.raises(IntegrityError):
        await append(
            session, merchant, uuid.uuid7(), "mandate.created", cast(dict[str, Any], [1, 2])
        )


async def test_display_text_is_not_an_event_type(session: AsyncSession, merchant: Merchant) -> None:
    """Event types are stable machine readable identifiers, not labels for a screen."""
    with pytest.raises(IntegrityError):
        await append(session, merchant, uuid.uuid7(), "Mandate Created")
