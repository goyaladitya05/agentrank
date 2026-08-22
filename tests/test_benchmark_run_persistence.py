"""Benchmark runs: shape, lifecycle, coherence and merchant isolation, at the database."""

import uuid
from datetime import UTC, datetime

import pytest
from benchmark_support import mission, suite
from commerce_support import admit, build_shop, quote
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.benchmark.failures import FailureReason
from agentrank_api.benchmark.lifecycle import BenchmarkRunStatus, MissionRunStatus
from agentrank_api.benchmark.models import BenchmarkMissionRun, BenchmarkRun, BenchmarkSuite
from agentrank_api.benchmark.repository import BenchmarkRunRepository, BenchmarkSuiteRepository
from agentrank_api.commerce.repository import MerchantRepository

pytestmark = pytest.mark.anyio


async def published(session: AsyncSession, *keys: str) -> BenchmarkSuite:
    """A published suite with one mission per key, committed."""
    stored = await BenchmarkSuiteRepository(session).create(
        suite(*(mission(key) for key in keys or ("one",)))
    )
    await session.commit()
    return stored


async def merchant_id(session: AsyncSession, slug: str = "voltedge") -> uuid.UUID:
    merchant = await MerchantRepository(session).create(slug=slug, name=slug)
    await session.commit()
    return merchant.id


async def started(
    session: AsyncSession, *keys: str, slug: str = "voltedge"
) -> tuple[BenchmarkRun, BenchmarkSuite]:
    """A RUNNING run over a published suite, committed."""
    stored = await published(session, *keys)
    run = await BenchmarkRunRepository(session).create(
        merchant_id=await merchant_id(session, slug), suite=stored
    )
    run.status = BenchmarkRunStatus.RUNNING
    run.started_at = datetime.now(UTC)
    await session.commit()
    return run, stored


async def begin(session: AsyncSession, result: BenchmarkMissionRun) -> BenchmarkMissionRun:
    """One mission run moved to RUNNING, committed.

    Every outcome is written from RUNNING, because the trigger is a transition whitelist and
    PENDING is not a state an outcome can be recorded from. A test that wants to assert a
    coherence constraint has to get past the lifecycle first, which is the lifecycle working.

    Committed rather than flushed so that a statement written afterwards runs in a later
    transaction. PostgreSQL's `now()` is the transaction's start instant, so a completion
    stamped with it inside this same transaction would predate a start stamped from Python and
    trip the ordering constraint for a reason that has nothing to do with what is being tested.
    """
    result.status = MissionRunStatus.RUNNING
    result.started_at = datetime.now(UTC)
    await session.commit()
    return result


async def test_a_run_holds_one_mission_run_per_mission(session: AsyncSession) -> None:
    """The shape of a run is fixed by the suite, not by how far execution got."""
    stored = await published(session, "one", "two", "three")

    run = await BenchmarkRunRepository(session).create(
        merchant_id=await merchant_id(session), suite=stored
    )
    await session.commit()

    assert run.status is BenchmarkRunStatus.PENDING
    assert len(run.mission_runs) == 3
    assert {result.status for result in run.mission_runs} == {MissionRunStatus.PENDING}
    assert {result.mission_id for result in run.mission_runs} == {row.id for row in stored.missions}


async def test_a_run_is_read_only_by_its_own_merchant(session: AsyncSession) -> None:
    """Another merchant's measurement does not exist as far as this merchant is concerned."""
    stored = await published(session)
    mine = await merchant_id(session, "voltedge")
    run = await BenchmarkRunRepository(session).create(merchant_id=mine, suite=stored)
    await session.commit()

    repository = BenchmarkRunRepository(session)

    assert await repository.get(run.id, merchant_id=mine) is not None
    assert await repository.get(run.id, merchant_id=uuid.uuid7()) is None


async def test_two_merchants_run_the_same_suite_independently(session: AsyncSession) -> None:
    """A suite is a global template, so two merchants measuring against it do not collide."""
    stored = await published(session)
    first = await merchant_id(session, "voltedge")
    second = await merchant_id(session, "ampere-supply")

    repository = BenchmarkRunRepository(session)
    mine = await repository.create(merchant_id=first, suite=stored)
    theirs = await repository.create(merchant_id=second, suite=stored)
    await session.commit()

    assert mine.suite_id == theirs.suite_id
    assert await repository.get(theirs.id, merchant_id=first) is None


async def test_one_mission_has_one_result_in_one_run(session: AsyncSession) -> None:
    run, stored = await started(session, "one")

    with pytest.raises(IntegrityError, match="uq_benchmark_mission_run_mission"):
        session.add(
            BenchmarkMissionRun(
                run_id=run.id,
                merchant_id=run.merchant_id,
                mission_id=stored.missions[0].id,
                status=MissionRunStatus.PENDING,
            )
        )
        await session.flush()


async def test_runs_are_listed_newest_first_and_bounded(session: AsyncSession) -> None:
    stored = await published(session)
    mine = await merchant_id(session)
    repository = BenchmarkRunRepository(session)
    created = [await repository.create(merchant_id=mine, suite=stored) for _ in range(3)]
    await session.commit()

    listed = await repository.list_for_merchant(merchant_id=mine, limit=2)

    assert [run.id for run in listed] == [created[2].id, created[1].id]


# Lifecycle, held by triggers rather than by the application.


async def test_run_ownership_and_identity_are_immutable(session: AsyncSession) -> None:
    run, _ = await started(session)

    with pytest.raises(DBAPIError, match="ownership and identity are immutable"):
        await session.execute(
            text("UPDATE benchmark_run SET suite_id = :other WHERE id = :id"),
            {"other": uuid.uuid7(), "id": run.id},
        )


async def test_a_run_cannot_go_backwards(session: AsyncSession) -> None:
    run, _ = await started(session)

    with pytest.raises(DBAPIError, match="cannot go from RUNNING to PENDING"):
        # The start instant is left alone deliberately. Clearing it would trip the "a start
        # time cannot be moved" guard first, and this test is about the transition.
        await session.execute(
            text("UPDATE benchmark_run SET status = 'PENDING' WHERE id = :id"),
            {"id": run.id},
        )


async def test_a_finished_run_is_terminal(session: AsyncSession) -> None:
    """A whitelist, so a status added later is refused until somebody places it."""
    run, _ = await started(session)
    await session.execute(
        text("UPDATE benchmark_run SET status = 'COMPLETED', completed_at = now() WHERE id = :id"),
        {"id": run.id},
    )

    with pytest.raises(DBAPIError, match="a completed benchmark run cannot be changed"):
        await session.execute(
            text("UPDATE benchmark_run SET status = 'ABORTED' WHERE id = :id"), {"id": run.id}
        )


async def test_a_recorded_mission_result_cannot_be_changed(session: AsyncSession) -> None:
    """The one edit that would let a bad measurement be tidied up afterwards."""
    run, _ = await started(session)
    result = await begin(session, run.mission_runs[0])
    await session.execute(
        text(
            "UPDATE benchmark_mission_run SET status = 'FAILED', completed_at = now(),"
            " primary_failure_reason = 'BUDGET_EXCEEDED' WHERE id = :id"
        ),
        {"id": result.id},
    )

    with pytest.raises(DBAPIError, match="recorded benchmark mission result cannot be changed"):
        await session.execute(
            text(
                "UPDATE benchmark_mission_run SET status = 'SUCCEEDED',"
                " primary_failure_reason = NULL WHERE id = :id"
            ),
            {"id": result.id},
        )


async def test_a_mission_run_cannot_be_reattributed(session: AsyncSession) -> None:
    run, _ = await started(session)

    with pytest.raises(DBAPIError, match="ownership and identity are immutable"):
        await session.execute(
            text("UPDATE benchmark_mission_run SET merchant_id = :other WHERE id = :id"),
            {"other": uuid.uuid7(), "id": run.mission_runs[0].id},
        )


# Status and reason are separate facts, and still not free of each other.


async def test_a_succeeded_mission_carries_no_failure_reason(session: AsyncSession) -> None:
    run, _ = await started(session)
    result = await begin(session, run.mission_runs[0])

    with pytest.raises(IntegrityError, match="failure_reason_matches_status"):
        await session.execute(
            text(
                "UPDATE benchmark_mission_run SET status = 'SUCCEEDED', completed_at = now(),"
                " primary_failure_reason = 'PAYMENT_FAILED' WHERE id = :id"
            ),
            {"id": result.id},
        )


async def test_a_failed_mission_must_say_why(session: AsyncSession) -> None:
    run, _ = await started(session)
    result = await begin(session, run.mission_runs[0])

    with pytest.raises(IntegrityError, match="failure_reason_matches_status"):
        await session.execute(
            text(
                "UPDATE benchmark_mission_run SET status = 'FAILED', completed_at = now()"
                " WHERE id = :id"
            ),
            {"id": result.id},
        )


async def test_an_abstention_may_carry_a_reason_or_none(session: AsyncSession) -> None:
    """The whole reason ABSTAINED is a status: correct abstentions have nothing to explain."""
    run, _ = await started(session, "one", "two")

    for result, reason in (
        (run.mission_runs[0], None),
        (run.mission_runs[1], FailureReason.DISCOVERY_FAILURE.value),
    ):
        await begin(session, result)
        await session.execute(
            text(
                "UPDATE benchmark_mission_run SET status = 'ABSTAINED', completed_at = now(),"
                " primary_failure_reason = :reason WHERE id = :id"
            ),
            {"reason": reason, "id": result.id},
        )
    await session.commit()


async def test_additional_reasons_need_a_primary(session: AsyncSession) -> None:
    run, _ = await started(session)

    with pytest.raises(IntegrityError, match="additional_reasons_need_a_primary"):
        await session.execute(
            text(
                "UPDATE benchmark_mission_run"
                " SET additional_failure_reasons = '[\"BUDGET_EXCEEDED\"]'::jsonb"
                " WHERE id = :id"
            ),
            {"id": run.mission_runs[0].id},
        )


async def test_failure_reasons_are_reassembled_primary_first(session: AsyncSession) -> None:
    run, _ = await started(session)
    result = await begin(session, run.mission_runs[0])
    result.status = MissionRunStatus.FAILED
    result.completed_at = datetime.now(UTC)
    result.primary_failure_reason = FailureReason.CONSTRAINT_VIOLATION
    result.additional_failure_reasons = [FailureReason.BUDGET_EXCEEDED.value]
    await session.commit()

    assert result.failure_reasons == (
        FailureReason.CONSTRAINT_VIOLATION,
        FailureReason.BUDGET_EXCEEDED,
    )


# Safety, constrained rather than trusted.


async def test_an_unsafe_attempt_is_never_a_success(session: AsyncSession) -> None:
    """Success requires full compliance, so the two cannot coexist."""
    run, _ = await started(session)
    result = await begin(session, run.mission_runs[0])

    with pytest.raises(IntegrityError, match="unsafe_is_never_a_success"):
        await session.execute(
            text(
                "UPDATE benchmark_mission_run SET status = 'SUCCEEDED', completed_at = now(),"
                " unsafe_attempt = true WHERE id = :id"
            ),
            {"id": result.id},
        )


async def test_an_escape_implies_an_attempt(session: AsyncSession) -> None:
    run, _ = await started(session)

    with pytest.raises(IntegrityError, match="completion_implies_attempt"):
        await session.execute(
            text("UPDATE benchmark_mission_run SET unsafe_completion = true WHERE id = :id"),
            {"id": run.mission_runs[0].id},
        )


async def test_a_selection_is_a_variant_and_a_count_together(session: AsyncSession) -> None:
    run, _ = await started(session)

    with pytest.raises(IntegrityError, match="selection_shape"):
        await session.execute(
            text("UPDATE benchmark_mission_run SET selected_quantity = 1 WHERE id = :id"),
            {"id": run.mission_runs[0].id},
        )


# Merchant isolation, structural through composite foreign keys.


async def test_a_mission_run_cannot_record_another_merchants_variant(
    session: AsyncSession,
) -> None:
    stored = await published(session)
    mine = await merchant_id(session, "voltedge")
    theirs = await build_shop(session, "rival-supply")
    run = await BenchmarkRunRepository(session).create(merchant_id=mine, suite=stored)
    await session.commit()

    with pytest.raises(IntegrityError, match="fk_benchmark_mission_run_variant"):
        await session.execute(
            text(
                "UPDATE benchmark_mission_run SET selected_variant_id = :variant,"
                " selected_quantity = 1 WHERE id = :id"
            ),
            {"variant": theirs.variant_id, "id": run.mission_runs[0].id},
        )


async def test_a_mission_run_cannot_record_another_merchants_checkout(
    session: AsyncSession,
) -> None:
    stored = await published(session)
    mine = await merchant_id(session, "voltedge")
    theirs = await build_shop(session, "rival-supply")
    their_checkout = await quote(session, theirs)
    await session.commit()
    run = await BenchmarkRunRepository(session).create(merchant_id=mine, suite=stored)
    await session.commit()

    with pytest.raises(IntegrityError, match="fk_benchmark_mission_run_checkout"):
        await session.execute(
            text("UPDATE benchmark_mission_run SET checkout_id = :checkout WHERE id = :id"),
            {"checkout": their_checkout, "id": run.mission_runs[0].id},
        )


async def test_a_mission_run_cannot_record_another_merchants_payment(
    session: AsyncSession,
) -> None:
    """The reference the payment table gained a unique target for."""
    stored = await published(session)
    mine = await merchant_id(session, "voltedge")
    theirs = await build_shop(session, "rival-supply")
    their_checkout = await quote(session, theirs)
    attempt = await admit(session, theirs, their_checkout, key="bench-0001")
    await session.commit()
    run = await BenchmarkRunRepository(session).create(merchant_id=mine, suite=stored)
    await session.commit()

    with pytest.raises(IntegrityError, match="fk_benchmark_mission_run_payment"):
        await session.execute(
            text("UPDATE benchmark_mission_run SET payment_attempt_id = :attempt WHERE id = :id"),
            {"attempt": attempt.id, "id": run.mission_runs[0].id},
        )


async def test_a_mission_run_records_its_own_merchants_commerce_rows(
    session: AsyncSession,
) -> None:
    """The positive case, so the isolation tests above are not passing for the wrong reason."""
    stored = await published(session)
    shop = await build_shop(session, "voltedge")
    checkout_id = await quote(session, shop)
    attempt = await admit(session, shop, checkout_id, key="bench-0002")
    await session.commit()
    run = await BenchmarkRunRepository(session).create(merchant_id=shop.merchant_id, suite=stored)
    await session.commit()

    result = run.mission_runs[0]
    result.selected_variant_id = shop.variant_id
    result.selected_quantity = 1
    result.checkout_id = checkout_id
    result.payment_attempt_id = attempt.id
    await session.commit()

    assert result.checkout_id == checkout_id
    assert result.payment_attempt_id == attempt.id
