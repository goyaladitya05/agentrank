"""Published benchmark definitions: database invariants and immutability."""

import uuid

import pytest
from benchmark_support import BLACK, CHARGERS, mission, suite
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.benchmark.definitions import ExpectedOutcome
from agentrank_api.benchmark.identity import suite_content_hash
from agentrank_api.benchmark.models import BenchmarkMission, BenchmarkSuite
from agentrank_api.benchmark.repository import BenchmarkSuiteRepository
from agentrank_api.constraints.rules import ConstraintOperator
from agentrank_api.mandates.intent import MaxQuantity, Preference, RequiredAttribute

pytestmark = pytest.mark.anyio


async def test_a_suite_and_its_missions_round_trip_through_the_database(
    session: AsyncSession,
) -> None:
    """A stored definition is the same definition, including constraint value types."""
    definition = suite(
        mission(
            "one",
            constraints=(
                BLACK,
                CHARGERS,
                RequiredAttribute("wattage", 100, ConstraintOperator.GTE),
                RequiredAttribute("connector", ("USB-C", "USB-A"), ConstraintOperator.IN),
                MaxQuantity(2),
            ),
            preferences=(Preference("prefer braided"),),
        ),
        mission("two", outcome=ExpectedOutcome.NO_ACCEPTABLE_PURCHASE),
    )

    await BenchmarkSuiteRepository(session).create(definition)
    await session.commit()
    session.expunge_all()

    stored = await BenchmarkSuiteRepository(session).get(definition.key, definition.version)
    assert stored is not None
    assert stored.to_definition() == definition


async def test_the_stored_hash_is_computed_from_the_definition(session: AsyncSession) -> None:
    """The repository computes it, so a caller cannot store a digest that disagrees."""
    definition = suite()

    stored = await BenchmarkSuiteRepository(session).create(definition)

    assert stored.definition_hash == suite_content_hash(definition)


async def test_missions_keep_the_order_they_were_authored_in(session: AsyncSession) -> None:
    """Ordering is what makes a rerun present the same workload in the same sequence."""
    definition = suite(mission("third"), mission("first"), mission("second"))

    await BenchmarkSuiteRepository(session).create(definition)
    await session.commit()
    session.expunge_all()

    stored = await BenchmarkSuiteRepository(session).get(definition.key, definition.version)
    assert stored is not None
    assert [row.ordinal for row in stored.missions] == [0, 1, 2]
    assert [row.mission_key for row in stored.missions] == ["third", "first", "second"]


async def test_one_key_and_version_hold_one_definition(session: AsyncSession) -> None:
    repository = BenchmarkSuiteRepository(session)
    await repository.create(suite(mission("one")))
    await session.flush()

    with pytest.raises(IntegrityError, match="uq_benchmark_suite_version"):
        await repository.create(suite(mission("two")))


async def test_the_same_key_may_hold_several_versions(session: AsyncSession) -> None:
    repository = BenchmarkSuiteRepository(session)
    await repository.create(suite(version=1))
    await repository.create(suite(version=2))
    await session.commit()

    versions = await repository.list_versions("test-suite")

    assert [stored.version for stored in versions] == [1, 2]


async def test_a_published_suite_cannot_be_updated(session: AsyncSession) -> None:
    """The trigger, not the ORM. A historical result must keep meaning what it meant."""
    stored = await BenchmarkSuiteRepository(session).create(suite())
    await session.commit()

    with pytest.raises(DBAPIError, match="published benchmark definitions are immutable"):
        await session.execute(
            text("UPDATE benchmark_suite SET name = 'renamed' WHERE id = :id"),
            {"id": stored.id},
        )


async def test_a_published_suite_cannot_be_deleted(session: AsyncSession) -> None:
    stored = await BenchmarkSuiteRepository(session).create(suite())
    await session.commit()

    with pytest.raises(DBAPIError, match="published benchmark definitions are immutable"):
        await session.execute(text("DELETE FROM benchmark_suite WHERE id = :id"), {"id": stored.id})


async def test_a_published_mission_cannot_be_updated(session: AsyncSession) -> None:
    """Editing the oracle of a published mission is the one edit that would be invisible."""
    stored = await BenchmarkSuiteRepository(session).create(suite())
    await session.commit()

    with pytest.raises(DBAPIError, match="published benchmark definitions are immutable"):
        await session.execute(
            text(
                "UPDATE benchmark_mission"
                " SET expected_outcome = 'NO_ACCEPTABLE_PURCHASE',"
                " simulated_value_amount_minor = 0"
                " WHERE suite_id = :id"
            ),
            {"id": stored.id},
        )


async def test_a_mission_cannot_claim_value_it_could_never_have_earned(
    session: AsyncSession,
) -> None:
    """Written around the domain, straight at the table, which is the layer that counts."""
    stored = await BenchmarkSuiteRepository(session).create(suite())
    await session.commit()

    with pytest.raises(IntegrityError, match="simulated_value_matches_outcome"):
        await session.execute(
            text(
                "INSERT INTO benchmark_mission (id, suite_id, mission_key, ordinal, objective,"
                " quantity, budget_amount_minor, currency, hard_constraints, preferences,"
                " expected_outcome, simulated_value_amount_minor)"
                " VALUES (:id, :suite_id, 'phantom', 99, 'Buy nothing', 1, 100, 'INR',"
                " '[]'::jsonb, '[]'::jsonb, 'NO_ACCEPTABLE_PURCHASE', 1)"
            ),
            {"id": uuid.uuid7(), "suite_id": stored.id},
        )


async def test_an_available_purchase_must_carry_value_at_the_database(
    session: AsyncSession,
) -> None:
    stored = await BenchmarkSuiteRepository(session).create(suite())
    await session.commit()

    with pytest.raises(IntegrityError, match="simulated_value_matches_outcome"):
        await session.execute(
            text(
                "INSERT INTO benchmark_mission (id, suite_id, mission_key, ordinal, objective,"
                " quantity, budget_amount_minor, currency, hard_constraints, preferences,"
                " expected_outcome, simulated_value_amount_minor)"
                " VALUES (:id, :suite_id, 'free-lunch', 98, 'Buy a charger', 1, 100, 'INR',"
                " '[]'::jsonb, '[]'::jsonb, 'PURCHASE_AVAILABLE', 0)"
            ),
            {"id": uuid.uuid7(), "suite_id": stored.id},
        )


async def test_a_mission_key_is_unique_within_a_suite(session: AsyncSession) -> None:
    stored = await BenchmarkSuiteRepository(session).create(suite(mission("one")))
    await session.commit()

    with pytest.raises(IntegrityError, match="uq_benchmark_mission_key"):
        session.add(
            BenchmarkMission(
                suite_id=stored.id,
                mission_key="one",
                ordinal=7,
                objective="Buy another charger",
                quantity=1,
                budget_amount_minor=100,
                currency="INR",
                hard_constraints=[],
                preferences=[],
                expected_outcome=ExpectedOutcome.PURCHASE_AVAILABLE,
                simulated_value_amount_minor=100,
            )
        )
        await session.flush()


async def test_a_suite_has_no_merchant_column(session: AsyncSession) -> None:
    """Suites are global templates. Merchant ownership belongs to runs, not to definitions."""
    columns = set(BenchmarkSuite.__table__.columns.keys())

    assert "merchant_id" not in columns
    assert "merchant_slug" in columns
