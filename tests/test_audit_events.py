"""The audit trail is append only, and the database is what makes that true.

The application offering no update and no delete is the contract. These tests check the
enforcement underneath it, because a guarantee that depends on nobody writing the wrong
function is not a guarantee.
"""

import uuid
from typing import Any, cast

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agentrank_api.audit.models import ActorType, AuditEvent
from agentrank_api.audit.repository import AuditRepository
from agentrank_api.commerce.models import Merchant
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.database import create_session_factory

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


@pytest.fixture
def factory(catalog_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Independent sessions, so two transactions can overlap for real."""
    return create_session_factory(catalog_engine)


async def test_occurred_at_is_append_time_and_not_commit_order(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession], merchant: Merchant
) -> None:
    """The honest limit of this column, asserted so nothing later assumes otherwise.

    `now()` in PostgreSQL is `transaction_timestamp()`, so `occurred_at` is when the writing
    transaction began. A transaction that starts first and commits last therefore stamps its
    event earlier than one that started later and committed first, and the log's order is
    append order rather than commit order.

    That is stated rather than fixed. Switching to `clock_timestamp()` would not make it commit
    order either, and it would cost the property that events written in one transaction share
    one instant, which is how other tests say "these were one unit of work". The consequence
    that matters is the rule this pins: nothing may infer that one thing happened before
    another from the relative order of these rows. See docs/architecture.md.
    """
    slow_resource, quick_resource = uuid.uuid7(), uuid.uuid7()

    async with factory() as slow, factory() as quick:
        # The slow transaction starts first, which is what stamps its event first.
        slow_event = await append(slow, merchant, slow_resource, "mandate.created")
        quick_event = await append(quick, merchant, quick_resource, "mandate.created")
        # And commits last.
        await quick.commit()
        await slow.commit()

        assert slow_event.occurred_at < quick_event.occurred_at

    events = await AuditRepository(session).list_for_merchant(merchant.id)
    # Read back in the order the column gives, which is the reverse of the commit order.
    assert [event.resource_id for event in events] == [slow_resource, quick_resource]
