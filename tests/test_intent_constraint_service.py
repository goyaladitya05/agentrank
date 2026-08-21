"""Turning stated hard constraints into authorization data, once, atomically.

Reads go through a second session, so what is asserted is what was committed rather than
what the writing session happens to remember.

The two load bearing properties are the financial separation and the atomicity. The first
is what keeps one ceiling in one place; the second is what keeps authorization data from
existing without a record of having been granted.
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from agentrank_api.audit.models import ActorType, AuditEvent
from agentrank_api.audit.repository import AuditRepository
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.constraints.models import IntentConstraint, IntentConstraintSet
from agentrank_api.constraints.rules import ConstraintOperator, PersistedConstraintKind
from agentrank_api.constraints.service import (
    CONSTRAINTS_RESOURCE,
    IntentConstraintService,
    NewIntentConstraints,
)
from agentrank_api.database import create_session_factory
from agentrank_api.errors import ConflictError, NotFoundError
from agentrank_api.mandates.intent import (
    AllowedCategory,
    HardConstraint,
    MaxQuantity,
    MaxTotalAmount,
    RequiredAttribute,
)
from agentrank_api.mandates.models import SpendingMandate
from agentrank_api.mandates.repository import MandateRepository

pytestmark = pytest.mark.anyio

NOW = datetime.now(UTC)
HOUR = timedelta(hours=1)
CEILING = 500000

BLACK = RequiredAttribute(name="color", value="black")
CHARGERS = AllowedCategory(category="chargers")


@dataclass(frozen=True, slots=True)
class Authorization:
    merchant_id: uuid.UUID
    mandate_id: uuid.UUID


async def build(
    session: AsyncSession, slug: str = "ampere-supply", **overrides: object
) -> Authorization:
    merchant = await MerchantRepository(session).create(slug=slug, name=slug.title())
    fields: dict[str, object] = {
        "merchant_id": merchant.id,
        "max_total_amount_minor": CEILING,
        "currency": "INR",
        "max_quantity": 1,
        "valid_from": NOW,
        "valid_until": NOW + HOUR,
    }
    mandate = await MandateRepository(session).create(**(fields | overrides))  # type: ignore[arg-type]
    await session.commit()
    return Authorization(merchant_id=merchant.id, mandate_id=mandate.id)


@pytest.fixture
async def authorization(session: AsyncSession) -> Authorization:
    return await build(session)


@pytest.fixture
async def committed(catalog_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A second session, for reading what actually reached the database."""
    factory = create_session_factory(catalog_engine)
    async with factory() as other:
        yield other


def request_for(authorization: Authorization, *constraints: HardConstraint) -> NewIntentConstraints:
    return NewIntentConstraints(
        merchant_id=authorization.merchant_id,
        mandate_id=authorization.mandate_id,
        hard_constraints=constraints or (BLACK,),
    )


async def test_constraints_are_persisted_and_recorded(
    session: AsyncSession, committed: AsyncSession, authorization: Authorization
) -> None:
    written = await IntentConstraintService(session).create_constraints(
        request_for(authorization, BLACK, CHARGERS)
    )

    stored = await committed.get(IntentConstraintSet, written.id)
    assert stored is not None
    assert stored.mandate_id == authorization.mandate_id
    assert stored.merchant_id == authorization.merchant_id

    events = await AuditRepository(committed).list_for_resource(
        resource_type=CONSTRAINTS_RESOURCE, resource_id=written.id
    )
    assert [event.event_type for event in events] == ["intent_constraints.created"]
    assert events[0].actor_type is ActorType.BUYER
    assert events[0].payload["mandate_id"] == str(authorization.mandate_id)
    assert events[0].payload["constraint_count"] == 2
    assert events[0].payload["constraints"] == [
        {"kind": "required_attribute", "attribute": "color", "operator": "EQ", "value": "black"},
        {"kind": "allowed_category", "attribute": None, "operator": "IN", "value": ["chargers"]},
    ]


async def test_several_allowed_categories_become_one_membership_rule(
    session: AsyncSession, authorization: Authorization
) -> None:
    """Any one of them, not all of them, so they are one rule rather than several."""
    written = await IntentConstraintService(session).create_constraints(
        request_for(authorization, CHARGERS, BLACK, AllowedCategory(category="Cables"))
    )

    assert len(written.constraints) == 2
    category = written.constraints[0]
    assert category.kind is PersistedConstraintKind.ALLOWED_CATEGORY
    assert category.operator is ConstraintOperator.IN
    # Folded into the position of the first category the buyer named.
    assert category.value == ["chargers", "Cables"]


async def test_a_category_repeated_in_another_capitalisation_collapses(
    session: AsyncSession, authorization: Authorization
) -> None:
    written = await IntentConstraintService(session).create_constraints(
        request_for(authorization, CHARGERS, AllowedCategory(category="Chargers"))
    )

    assert written.constraints[0].value == ["chargers"]


async def test_a_financial_constraint_is_checked_against_the_mandate_and_not_stored(
    session: AsyncSession, committed: AsyncSession, authorization: Authorization
) -> None:
    """One ceiling, one home. The mandate is the only authority on money."""
    written = await IntentConstraintService(session).create_constraints(
        request_for(
            authorization,
            MaxTotalAmount(amount_minor=CEILING, currency="INR"),
            MaxQuantity(quantity=1),
            BLACK,
        )
    )

    stored = (
        (
            await committed.execute(
                select(IntentConstraint).where(IntentConstraint.constraint_set_id == written.id)
            )
        )
        .scalars()
        .all()
    )
    assert [constraint.kind for constraint in stored] == [
        PersistedConstraintKind.REQUIRED_ATTRIBUTE
    ]

    events = await AuditRepository(committed).list_for_resource(
        resource_type=CONSTRAINTS_RESOURCE, resource_id=written.id
    )
    # Counted rather than copied: a number in the log that looked like a ceiling and was
    # not one would be worse than no number at all.
    assert events[0].payload["financial_constraints_checked"] == 2


async def test_a_mandate_looser_than_the_stated_limit_is_refused(
    session: AsyncSession, authorization: Authorization
) -> None:
    """The silent widening this whole separation exists to prevent.

    The buyer said 4000 rupees and the mandate authorizes 5000. The stated limit is not
    stored, so accepting this would authorize more than was asked for and nothing would
    ever notice.
    """
    with pytest.raises(ConflictError) as refusal:
        await IntentConstraintService(session).create_constraints(
            request_for(authorization, MaxTotalAmount(amount_minor=400000, currency="INR"), BLACK)
        )

    assert refusal.value.reason == "mandate_exceeds_intent_limit"


async def test_a_mandate_stricter_than_the_stated_limit_is_accepted(
    session: AsyncSession, authorization: Authorization
) -> None:
    """Stricter denies more, never less, so there is nothing to refuse."""
    written = await IntentConstraintService(session).create_constraints(
        request_for(authorization, MaxTotalAmount(amount_minor=900000, currency="INR"), BLACK)
    )

    assert len(written.constraints) == 1


async def test_a_stated_limit_in_another_currency_is_refused(
    session: AsyncSession, authorization: Authorization
) -> None:
    with pytest.raises(ConflictError) as refusal:
        await IntentConstraintService(session).create_constraints(
            request_for(authorization, MaxTotalAmount(amount_minor=CEILING, currency="EUR"), BLACK)
        )

    assert refusal.value.reason == "mandate_currency_mismatch"


async def test_a_mandate_with_no_quantity_ceiling_cannot_cover_a_stated_one(
    session: AsyncSession,
) -> None:
    """Null means no limit, which is looser than any stated one rather than equal to it."""
    unlimited = await build(session, "unlimited", max_quantity=None)

    with pytest.raises(ConflictError) as refusal:
        await IntentConstraintService(session).create_constraints(
            request_for(unlimited, MaxQuantity(quantity=1), BLACK)
        )

    assert refusal.value.reason == "mandate_exceeds_intent_limit"


async def test_a_mandate_may_be_qualified_only_once(
    session: AsyncSession, authorization: Authorization
) -> None:
    service = IntentConstraintService(session)
    await service.create_constraints(request_for(authorization, BLACK))

    with pytest.raises(ConflictError) as refusal:
        await service.create_constraints(request_for(authorization, CHARGERS))

    assert refusal.value.reason == "constraints_already_exist"


async def test_a_revoked_mandate_cannot_be_qualified(
    session: AsyncSession, authorization: Authorization
) -> None:
    mandate = await MandateRepository(session).get(
        authorization.mandate_id, merchant_id=authorization.merchant_id
    )
    assert mandate is not None
    await MandateRepository(session).revoke(mandate)
    await session.commit()

    with pytest.raises(ConflictError) as refusal:
        await IntentConstraintService(session).create_constraints(request_for(authorization))

    assert refusal.value.reason == "mandate_revoked"


async def test_another_merchants_mandate_is_reported_as_not_found(
    session: AsyncSession, authorization: Authorization
) -> None:
    theirs = await build(session, "rival")

    with pytest.raises(NotFoundError) as missing:
        await IntentConstraintService(session).create_constraints(
            NewIntentConstraints(
                merchant_id=authorization.merchant_id,
                mandate_id=theirs.mandate_id,
                hard_constraints=(BLACK,),
            )
        )

    assert missing.value.resource == "mandate"


async def test_an_unknown_merchant_is_reported_as_not_found(
    session: AsyncSession, authorization: Authorization
) -> None:
    with pytest.raises(NotFoundError) as missing:
        await IntentConstraintService(session).create_constraints(
            NewIntentConstraints(
                merchant_id=uuid.uuid7(),
                mandate_id=authorization.mandate_id,
                hard_constraints=(BLACK,),
            )
        )

    assert missing.value.resource == "merchant"


async def test_constraints_are_read_back_by_mandate(
    session: AsyncSession, authorization: Authorization
) -> None:
    service = IntentConstraintService(session)
    written = await service.create_constraints(request_for(authorization, BLACK, CHARGERS))
    session.expunge_all()

    found = await service.get_constraints(
        authorization.mandate_id, merchant_id=authorization.merchant_id
    )

    assert found.id == written.id
    assert len(found.constraints) == 2


async def test_a_mandate_with_no_constraints_raises_rather_than_reporting_none(
    session: AsyncSession, authorization: Authorization
) -> None:
    """Absence is not satisfaction, and it must not be reachable as a permissive default."""
    with pytest.raises(NotFoundError) as missing:
        await IntentConstraintService(session).get_constraints(
            authorization.mandate_id, merchant_id=authorization.merchant_id
        )

    assert missing.value.resource == "intent_constraints"


@pytest.mark.parametrize(
    "constraints",
    [
        pytest.param((), id="nothing at all"),
        pytest.param(
            (MaxTotalAmount(amount_minor=CEILING, currency="INR"),),
            id="only a financial limit, which stores nothing",
        ),
        pytest.param(
            (BLACK, RequiredAttribute(name="Color", value="blue")),
            id="two rules for one attribute, differing only in case",
        ),
    ],
)
def test_a_request_that_cannot_become_an_authorization_is_refused(
    constraints: tuple[HardConstraint, ...],
) -> None:
    with pytest.raises(ValueError):
        NewIntentConstraints(
            merchant_id=uuid.uuid7(), mandate_id=uuid.uuid7(), hard_constraints=constraints
        )


def test_a_range_over_one_attribute_is_two_rules_and_is_allowed() -> None:
    request = NewIntentConstraints(
        merchant_id=uuid.uuid7(),
        mandate_id=uuid.uuid7(),
        hard_constraints=(
            RequiredAttribute(name="wattage", operator=ConstraintOperator.GTE, value=100),
            RequiredAttribute(name="wattage", operator=ConstraintOperator.LTE, value=140),
        ),
    )

    assert len(request.semantic_specs()) == 2


async def test_the_constraint_set_and_its_event_commit_together(
    session: AsyncSession,
    committed: AsyncSession,
    authorization: Authorization,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authorization data with no record of being granted must not be reachable.

    The set and its constraints are written and flushed before the event, so if the two
    were not one transaction the rows would survive the failure.
    """

    async def explode(*_: object, **__: object) -> None:
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(AuditRepository, "append", explode)

    with pytest.raises(RuntimeError):
        await IntentConstraintService(session).create_constraints(request_for(authorization))

    assert await committed.scalar(select(func.count()).select_from(IntentConstraintSet)) == 0
    assert await committed.scalar(select(func.count()).select_from(IntentConstraint)) == 0
    assert await committed.scalar(select(func.count()).select_from(AuditEvent)) == 0
    # The reader can see the committed mandate, which rules out three empty results that
    # only mean the reading session is blind.
    assert await committed.get(SpendingMandate, authorization.mandate_id) is not None
