"""Mandate workflows, including the one that matters most: what commits together.

A mandate that exists with no record of being granted, or a revocation with no record of
being made, would make the audit trail decorative. These tests assert the transaction
boundary rather than assuming it.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from agentrank_api.audit.models import ActorType, AuditEvent
from agentrank_api.audit.repository import AuditRepository
from agentrank_api.commerce.models import Merchant
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.database import create_session_factory
from agentrank_api.errors import NotFoundError
from agentrank_api.mandates.intent import BuyerIntent, MaxTotalAmount
from agentrank_api.mandates.models import MandateStatus, SpendingMandate
from agentrank_api.mandates.service import MANDATE_RESOURCE, MandateService, NewMandate
from agentrank_api.mandates.validation import MandateViolation

pytestmark = pytest.mark.anyio

NOW = datetime.now(UTC)
HOUR = timedelta(hours=1)


@pytest.fixture
async def merchant(session: AsyncSession) -> Merchant:
    created = await MerchantRepository(session).create(slug="ampere-supply", name="Ampere Supply")
    await session.commit()
    return created


@pytest.fixture
async def committed(catalog_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A second session, for reading what actually reached the database."""
    factory = create_session_factory(catalog_engine)
    async with factory() as other:
        yield other


def request_for(merchant: Merchant, **overrides: object) -> NewMandate:
    fields: dict[str, object] = {
        "merchant_id": merchant.id,
        "max_total_amount_minor": 500000,
        "currency": "INR",
        "max_quantity": 1,
        "valid_from": NOW,
        "valid_until": NOW + HOUR,
    }
    return NewMandate(**(fields | overrides))  # type: ignore[arg-type]


async def test_creating_a_mandate_commits_it_with_its_creation_event(
    session: AsyncSession, committed: AsyncSession, merchant: Merchant
) -> None:
    mandate = await MandateService(session).create_mandate(request_for(merchant))

    # Read through a different session, so this is what was committed rather than what
    # the writing session happens to remember.
    stored = await committed.get(SpendingMandate, mandate.id)
    assert stored is not None
    assert stored.status is MandateStatus.ACTIVE

    events = await AuditRepository(committed).list_for_resource(
        resource_type=MANDATE_RESOURCE, resource_id=mandate.id
    )
    assert [event.event_type for event in events] == ["mandate.created"]
    assert events[0].actor_type is ActorType.BUYER
    assert events[0].payload == {
        "max_total_amount_minor": 500000,
        "currency": "INR",
        "max_quantity": 1,
        "valid_from": mandate.valid_from.isoformat(),
        "valid_until": mandate.valid_until.isoformat(),
        "status": "ACTIVE",
    }


async def test_the_creation_event_records_the_intent_behind_it(
    session: AsyncSession, committed: AsyncSession, merchant: Merchant
) -> None:
    """Why the authorization exists, alongside what it permits."""
    intent = BuyerIntent(
        merchant_id=merchant.id,
        description="One 100W USB-C charger",
        hard_constraints=(MaxTotalAmount(amount_minor=500000, currency="INR"),),
    )

    mandate = await MandateService(session).create_mandate(request_for(merchant, intent=intent))

    events = await AuditRepository(committed).list_for_resource(
        resource_type=MANDATE_RESOURCE, resource_id=mandate.id
    )
    assert events[0].payload["intent"] == intent.to_payload()


async def test_revoking_records_one_event_and_repeating_records_none(
    session: AsyncSession, committed: AsyncSession, merchant: Merchant
) -> None:
    service = MandateService(session)
    mandate = await service.create_mandate(request_for(merchant))

    revoked = await service.revoke_mandate(mandate.id, merchant_id=merchant.id)
    assert revoked.status is MandateStatus.REVOKED
    assert revoked.revoked_at is not None

    again = await service.revoke_mandate(mandate.id, merchant_id=merchant.id)
    assert again.revoked_at == revoked.revoked_at

    events = await AuditRepository(committed).list_for_resource(
        resource_type=MANDATE_RESOURCE, resource_id=mandate.id
    )
    assert [event.event_type for event in events] == ["mandate.created", "mandate.revoked"]
    # The revocation and the event that records it share the transaction clock.
    assert events[1].occurred_at == revoked.revoked_at


async def test_validation_answers_at_the_instant_it_is_asked_about(
    session: AsyncSession, merchant: Merchant
) -> None:
    service = MandateService(session)
    mandate = await service.create_mandate(request_for(merchant))

    assert (await service.validate_mandate(mandate.id, merchant_id=merchant.id)).valid
    later = await service.validate_mandate(mandate.id, merchant_id=merchant.id, at=NOW + 2 * HOUR)
    assert later.violations == (MandateViolation.MANDATE_EXPIRED,)


async def test_an_unknown_mandate_and_an_unknown_merchant_are_not_found(
    session: AsyncSession,
) -> None:
    service = MandateService(session)
    missing = uuid.uuid7()

    with pytest.raises(NotFoundError) as unknown_mandate:
        await service.get_mandate(missing, merchant_id=uuid.uuid7())
    assert unknown_mandate.value.resource == "mandate"

    absent_merchant = Merchant(id=missing, slug="nobody", name="Nobody")
    with pytest.raises(NotFoundError) as unknown_merchant:
        await service.create_mandate(request_for(absent_merchant))
    assert unknown_merchant.value.resource == "merchant"


async def test_nothing_persists_when_the_audit_append_fails(
    session: AsyncSession,
    committed: AsyncSession,
    merchant: Merchant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The transaction boundary, asserted rather than assumed.

    The mandate is written and flushed before the audit event, so if the two were not one
    transaction the row would survive this. Failing the append is the cleanest way to
    prove they are, and it needs no production code that exists only for a test.
    """

    async def refuse(*args: object, **kwargs: object) -> None:
        raise RuntimeError("audit is unavailable")

    monkeypatch.setattr(AuditRepository, "append", refuse)

    with pytest.raises(RuntimeError, match="audit is unavailable"):
        await MandateService(session).create_mandate(request_for(merchant))

    # The reader can see committed rows, so an empty result below means nothing was
    # committed rather than that this session cannot see anything.
    assert await committed.get(Merchant, merchant.id) is not None
    mandates = await committed.scalar(select(func.count()).select_from(SpendingMandate))
    events = await committed.scalar(select(func.count()).select_from(AuditEvent))
    assert mandates == 0
    assert events == 0
