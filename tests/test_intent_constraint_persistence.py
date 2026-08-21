"""Authoritative intent constraints, at both levels that protect them.

Two levels, because they guard the same rule from different directions. The domain refuses
a constraint that cannot be compared before it is ever written; the database refuses one
that arrived some other way. A rule only the application enforces is a rule that ends the
first time something writes around it.

What is mostly under test here is the database. The rows are reached through the repository
and the ORM, but every negative case tries to break a constraint or a trigger.
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.constraints.models import IntentConstraint, IntentConstraintSet
from agentrank_api.constraints.repository import IntentConstraintRepository
from agentrank_api.constraints.rules import (
    ConstraintOperator,
    IntentConstraintSpec,
    PersistedConstraintKind,
)
from agentrank_api.mandates.models import SpendingMandate
from agentrank_api.mandates.repository import MandateRepository

pytestmark = pytest.mark.anyio

NOW = datetime.now(UTC)
HOUR = timedelta(hours=1)

BLACK = IntentConstraintSpec.required_attribute("color", ConstraintOperator.EQ, "black")
CHARGERS = IntentConstraintSpec.allowed_categories(("chargers",))


async def a_mandate(session: AsyncSession, slug: str) -> SpendingMandate:
    merchant = await MerchantRepository(session).create(slug=slug, name=slug.title())
    return await MandateRepository(session).create(
        merchant_id=merchant.id,
        max_total_amount_minor=500000,
        currency="INR",
        valid_from=NOW,
        valid_until=NOW + HOUR,
    )


async def a_set(session: AsyncSession, slug: str = "shop") -> IntentConstraintSet:
    mandate = await a_mandate(session, slug)
    constraints = await IntentConstraintRepository(session).create(
        merchant_id=mandate.merchant_id,
        mandate_id=mandate.id,
        specs=[BLACK, CHARGERS],
    )
    await session.commit()
    return constraints


async def raw_insert(
    session: AsyncSession, constraint_set: IntentConstraintSet, **row: object
) -> None:
    """Write a constraint row without the ORM.

    SQLAlchemy refuses a value outside a mapped enum before the statement is sent, which
    proves the mapping rather than the schema. The schema is what has to hold when
    something else writes the row.
    """
    columns: dict[str, object] = {
        "id": uuid.uuid7(),
        "constraint_set_id": constraint_set.id,
        "merchant_id": constraint_set.merchant_id,
        "kind": PersistedConstraintKind.REQUIRED_ATTRIBUTE.value,
        "attribute_key": "color",
        "operator": ConstraintOperator.EQ.value,
        "value": '"black"',
    }
    await session.execute(
        text(
            "INSERT INTO intent_constraint"
            " (id, constraint_set_id, merchant_id, kind, attribute_key, operator, value)"
            " VALUES (:id, :constraint_set_id, :merchant_id, :kind, :attribute_key,"
            " :operator, CAST(:value AS jsonb))"
        ),
        columns | row,
    )


async def test_a_constraint_set_round_trips_with_its_constraints(session: AsyncSession) -> None:
    written = await a_set(session)
    session.expunge_all()

    found = await IntentConstraintRepository(session).get_for_mandate(
        written.mandate_id, merchant_id=written.merchant_id
    )

    assert found is not None
    assert found.id == written.id
    assert [constraint.to_spec() for constraint in found.constraints] == [BLACK, CHARGERS]
    # Stored as JSON, so a scalar arrives as a scalar and a list as a list.
    assert [constraint.value for constraint in found.constraints] == ["black", ["chargers"]]


async def test_a_mandate_without_constraints_has_no_set(session: AsyncSession) -> None:
    mandate = await a_mandate(session, "bare")
    await session.commit()

    missing = await IntentConstraintRepository(session).get_for_mandate(
        mandate.id, merchant_id=mandate.merchant_id
    )
    assert missing is None


async def test_a_mandate_cannot_hold_two_constraint_sets(session: AsyncSession) -> None:
    written = await a_set(session)

    # The unique constraint fires on the flush inside create, before any commit.
    with pytest.raises(IntegrityError):
        await IntentConstraintRepository(session).create(
            merchant_id=written.merchant_id,
            mandate_id=written.mandate_id,
            specs=[CHARGERS],
        )
    await session.rollback()


async def test_a_constraint_set_cannot_name_another_merchants_mandate(
    session: AsyncSession,
) -> None:
    mine = await a_mandate(session, "mine")
    theirs = await a_mandate(session, "theirs")
    await session.commit()

    # Structural rather than behavioural: the composite foreign key has no row to point
    # at, so this fails whether or not a service would have declined it.
    with pytest.raises(IntegrityError):
        await IntentConstraintRepository(session).create(
            merchant_id=mine.merchant_id,
            mandate_id=theirs.id,
            specs=[BLACK],
        )
    await session.rollback()


async def test_a_constraint_cannot_join_another_merchants_set(session: AsyncSession) -> None:
    written = await a_set(session)
    other = await a_mandate(session, "other")
    await session.commit()

    session.add(
        IntentConstraint(
            constraint_set_id=written.id,
            merchant_id=other.merchant_id,
            kind=PersistedConstraintKind.REQUIRED_ATTRIBUTE,
            attribute_key="wattage",
            operator=ConstraintOperator.GTE,
            value=100,
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_a_constraint_set_cannot_name_a_mandate_that_does_not_exist(
    session: AsyncSession,
) -> None:
    merchant = await MerchantRepository(session).create(slug="ghost", name="Ghost")
    await session.commit()

    with pytest.raises(IntegrityError):
        await IntentConstraintRepository(session).create(
            merchant_id=merchant.id,
            mandate_id=uuid.uuid7(),
            specs=[BLACK],
        )
    await session.rollback()


async def test_an_authorized_constraint_cannot_be_edited(session: AsyncSession) -> None:
    written = await a_set(session)

    # The one thing this whole phase exists to prevent: loosening "black only" into "any
    # colour" after the authorization was granted.
    with pytest.raises(DBAPIError, match="immutable"):
        await session.execute(
            text("UPDATE intent_constraint SET value = '\"blue\"'::jsonb WHERE id = :id"),
            {"id": written.constraints[0].id},
        )
    await session.rollback()


async def test_an_authorized_constraint_cannot_be_deleted(session: AsyncSession) -> None:
    written = await a_set(session)

    with pytest.raises(DBAPIError, match="immutable"):
        await session.execute(
            text("DELETE FROM intent_constraint WHERE id = :id"),
            {"id": written.constraints[0].id},
        )
    await session.rollback()


async def test_a_constraint_set_cannot_be_edited(session: AsyncSession) -> None:
    written = await a_set(session)

    with pytest.raises(DBAPIError, match="immutable"):
        await session.execute(
            text("UPDATE intent_constraint_set SET mandate_id = :id WHERE id = :id"),
            {"id": written.id},
        )
    await session.rollback()


async def test_an_unknown_constraint_kind_is_refused(session: AsyncSession) -> None:
    written = await a_set(session)

    # max_total_amount is a real BuyerIntent constraint kind, and it is exactly the one
    # that must never become a second authoritative copy of a mandate ceiling.
    with pytest.raises(IntegrityError):
        await raw_insert(session, written, kind="max_total_amount", attribute_key=None)
    await session.rollback()


async def test_an_unknown_operator_is_refused(session: AsyncSession) -> None:
    written = await a_set(session)

    with pytest.raises(IntegrityError):
        await raw_insert(session, written, operator="MATCHES")
    await session.rollback()


@pytest.mark.parametrize(
    "row",
    [
        pytest.param({"value": '{"color": "black"}'}, id="an object is not a value"),
        pytest.param({"operator": "IN", "value": "[]"}, id="an empty list asks nothing"),
        pytest.param({"operator": "GTE", "value": '"100"'}, id="ordering against text"),
        pytest.param({"operator": "IN", "value": '"black"'}, id="a scalar where a list is due"),
        pytest.param({"value": '["black", "blue"]'}, id="a list where a scalar is due"),
        pytest.param(
            {"kind": "allowed_category", "operator": "IN", "value": '["chargers"]'},
            id="a category rule that also names an attribute",
        ),
        pytest.param({"attribute_key": None}, id="an attribute rule that names none"),
    ],
)
async def test_a_constraint_the_operator_cannot_evaluate_is_refused(
    session: AsyncSession, row: dict[str, object]
) -> None:
    written = await a_set(session)

    with pytest.raises(IntegrityError):
        await raw_insert(session, written, **row)
    await session.rollback()


async def test_one_rule_per_target(session: AsyncSession) -> None:
    written = await a_set(session)

    # Two EQ rules for one attribute are a contradiction rather than a tighter bound.
    session.add(
        IntentConstraint(
            constraint_set_id=written.id,
            merchant_id=written.merchant_id,
            kind=PersistedConstraintKind.REQUIRED_ATTRIBUTE,
            attribute_key="color",
            operator=ConstraintOperator.EQ,
            value="blue",
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_two_category_rules_cannot_split_one_membership_rule(
    session: AsyncSession,
) -> None:
    written = await a_set(session)

    # attribute_key is null on both rows, and NULLS NOT DISTINCT is what lets the unique
    # constraint see them as the same target at all.
    session.add(
        IntentConstraint(
            constraint_set_id=written.id,
            merchant_id=written.merchant_id,
            kind=PersistedConstraintKind.ALLOWED_CATEGORY,
            attribute_key=None,
            operator=ConstraintOperator.IN,
            value=["cables"],
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_a_range_is_two_rules_on_one_attribute(session: AsyncSession) -> None:
    mandate = await a_mandate(session, "range")
    written = await IntentConstraintRepository(session).create(
        merchant_id=mandate.merchant_id,
        mandate_id=mandate.id,
        specs=[
            IntentConstraintSpec.required_attribute("wattage", ConstraintOperator.GTE, 100),
            IntentConstraintSpec.required_attribute("wattage", ConstraintOperator.LTE, 140),
        ],
    )
    await session.commit()

    assert len(written.constraints) == 2


async def test_an_empty_constraint_set_is_not_an_authorization(session: AsyncSession) -> None:
    mandate = await a_mandate(session, "empty")

    with pytest.raises(ValueError, match="at least one constraint"):
        await IntentConstraintRepository(session).create(
            merchant_id=mandate.merchant_id,
            mandate_id=mandate.id,
            specs=[],
        )


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(
            lambda: IntentConstraintSpec.required_attribute(
                "wattage", ConstraintOperator.GTE, "100"
            ),
            id="text where a number is promised",
        ),
        pytest.param(
            lambda: IntentConstraintSpec.required_attribute(
                "wattage", ConstraintOperator.LTE, True
            ),
            id="a boolean is not a number",
        ),
        pytest.param(
            lambda: IntentConstraintSpec.required_attribute(
                "color", ConstraintOperator.EQ, ("black", "blue")
            ),
            id="a list where a scalar is due",
        ),
        pytest.param(
            lambda: IntentConstraintSpec.required_attribute(
                "color", ConstraintOperator.IN, ("black", 100)
            ),
            id="a mixture of kinds in one list",
        ),
        pytest.param(
            lambda: IntentConstraintSpec.allowed_categories(()),
            id="an empty list",
        ),
        pytest.param(
            lambda: IntentConstraintSpec.required_attribute("color", ConstraintOperator.EQ, "  "),
            id="a blank value",
        ),
        pytest.param(
            lambda: IntentConstraintSpec.required_attribute(" ", ConstraintOperator.EQ, "black"),
            id="no attribute named",
        ),
    ],
)
def test_the_domain_refuses_a_constraint_that_cannot_be_compared(
    build: Callable[[], IntentConstraintSpec],
) -> None:
    with pytest.raises(ValueError):
        build()
