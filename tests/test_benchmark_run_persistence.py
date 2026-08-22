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
from agentrank_api.benchmark.identity import CorruptedSuiteDefinitionError
from agentrank_api.benchmark.lifecycle import BenchmarkRunStatus, MissionRunStatus
from agentrank_api.benchmark.models import BenchmarkMissionRun, BenchmarkRun, BenchmarkSuite
from agentrank_api.benchmark.repository import BenchmarkRunRepository, BenchmarkSuiteRepository
from agentrank_api.commerce.models import Merchant
from agentrank_api.commerce.repository import MerchantRepository

pytestmark = pytest.mark.anyio


async def published(session: AsyncSession, *keys: str) -> BenchmarkSuite:
    """A published suite with one mission per key, committed."""
    stored = await BenchmarkSuiteRepository(session).create(
        suite(*(mission(key) for key in keys or ("one",)))
    )
    await session.commit()
    return stored


async def merchant(session: AsyncSession, slug: str = "test-merchant") -> Merchant:
    """A merchant whose slug matches what `benchmark_support.suite` authors against."""
    created = await MerchantRepository(session).create(slug=slug, name=slug)
    await session.commit()
    return created


async def started(
    session: AsyncSession, *keys: str, slug: str = "test-merchant"
) -> tuple[BenchmarkRun, BenchmarkSuite]:
    """A RUNNING run over a published suite, committed."""
    stored = await published(session, *keys)
    run = await BenchmarkRunRepository(session).create(
        merchant=await merchant(session, slug), suite=stored
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
        merchant=await merchant(session), suite=stored
    )
    await session.commit()

    assert run.status is BenchmarkRunStatus.PENDING
    assert len(run.mission_runs) == 3
    assert {result.status for result in run.mission_runs} == {MissionRunStatus.PENDING}
    assert {result.mission_id for result in run.mission_runs} == {row.id for row in stored.missions}


async def test_a_run_is_read_only_by_its_own_merchant(session: AsyncSession) -> None:
    """Another merchant's measurement does not exist as far as this merchant is concerned."""
    stored = await published(session)
    mine = await merchant(session)
    run = await BenchmarkRunRepository(session).create(merchant=mine, suite=stored)
    await session.commit()

    repository = BenchmarkRunRepository(session)

    assert await repository.get(run.id, merchant_id=mine.id) is not None
    assert await repository.get(run.id, merchant_id=uuid.uuid7()) is None


async def test_a_suite_is_only_run_against_the_merchant_it_was_authored_for(
    session: AsyncSession,
) -> None:
    """A mission oracle is a statement about one catalog.

    Run anywhere else it produces a perfectly well formed result marked against ground truth
    nobody ever established there, which is worse than no result.
    """
    stored = await published(session)
    stranger = await merchant(session, "ampere-supply")

    with pytest.raises(ValueError, match="was authored against merchant"):
        await BenchmarkRunRepository(session).create(merchant=stranger, suite=stored)


async def test_one_mission_has_one_result_in_one_run(session: AsyncSession) -> None:
    # A PENDING run, because the insert guard refuses a mission run against a started one and
    # would answer first. What is being asserted here is the uniqueness underneath it.
    stored = await published(session, "one")
    run = await BenchmarkRunRepository(session).create(
        merchant=await merchant(session), suite=stored
    )
    await session.commit()

    with pytest.raises(IntegrityError, match="uq_benchmark_mission_run_mission"):
        session.add(
            BenchmarkMissionRun(
                run_id=run.id,
                merchant_id=run.merchant_id,
                suite_id=run.suite_id,
                mission_id=stored.missions[0].id,
                status=MissionRunStatus.PENDING,
            )
        )
        await session.flush()


async def test_runs_are_listed_newest_first_and_bounded(session: AsyncSession) -> None:
    stored = await published(session)
    mine = await merchant(session)
    repository = BenchmarkRunRepository(session)
    created = [await repository.create(merchant=mine, suite=stored) for _ in range(3)]
    await session.commit()

    listed = await repository.list_for_merchant(merchant_id=mine.id, limit=2)

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
    mine = await merchant(session)
    theirs = await build_shop(session, "rival-supply")
    run = await BenchmarkRunRepository(session).create(merchant=mine, suite=stored)
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
    mine = await merchant(session)
    theirs = await build_shop(session, "rival-supply")
    their_checkout = await quote(session, theirs)
    await session.commit()
    run = await BenchmarkRunRepository(session).create(merchant=mine, suite=stored)
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
    mine = await merchant(session)
    theirs = await build_shop(session, "rival-supply")
    their_checkout = await quote(session, theirs)
    attempt = await admit(session, theirs, their_checkout, key="bench-0001")
    await session.commit()
    run = await BenchmarkRunRepository(session).create(merchant=mine, suite=stored)
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
    shop = await build_shop(session, "test-merchant")
    checkout_id = await quote(session, shop)
    attempt = await admit(session, shop, checkout_id, key="bench-0002")
    await session.commit()
    owner = await MerchantRepository(session).get_by_id(shop.merchant_id)
    assert owner is not None
    run = await BenchmarkRunRepository(session).create(merchant=owner, suite=stored)
    await session.commit()

    result = run.mission_runs[0]
    result.selected_variant_id = shop.variant_id
    result.selected_quantity = 1
    result.checkout_id = checkout_id
    result.payment_attempt_id = attempt.id
    await session.commit()

    assert result.checkout_id == checkout_id
    assert result.payment_attempt_id == attempt.id


# The safety and reason columns the evaluation hardening added.


async def test_an_unknown_additional_failure_reason_is_refused(session: AsyncSession) -> None:
    """An array of the right shape is not an array of the right contents.

    Without this the row commits and then raises while being read back, which is the worst
    order to discover it in.
    """
    run, _ = await started(session)
    result = await begin(session, run.mission_runs[0])

    with pytest.raises(IntegrityError, match="additional_reasons_known"):
        await session.execute(
            text(
                "UPDATE benchmark_mission_run SET status = 'FAILED', completed_at = now(),"
                " primary_failure_reason = 'BUDGET_EXCEEDED',"
                " additional_failure_reasons = '[\"NOT_A_REASON\"]'::jsonb WHERE id = :id"
            ),
            {"id": result.id},
        )


async def test_the_primary_reason_is_not_repeated_among_the_additional_ones(
    session: AsyncSession,
) -> None:
    """Repeating it would report it twice and double count one mission in a report."""
    run, _ = await started(session)
    result = await begin(session, run.mission_runs[0])

    with pytest.raises(IntegrityError, match="additional_reasons_exclude_primary"):
        await session.execute(
            text(
                "UPDATE benchmark_mission_run SET status = 'FAILED', completed_at = now(),"
                " primary_failure_reason = 'BUDGET_EXCEEDED',"
                " additional_failure_reasons = '[\"BUDGET_EXCEEDED\"]'::jsonb WHERE id = :id"
            ),
            {"id": result.id},
        )


async def test_the_new_failure_reason_is_accepted_by_the_live_constraint(
    session: AsyncSession,
) -> None:
    """Every reason the code knows about has to be a reason the database accepts.

    Alembic does not detect a changed expression on a check constraint that exists on both
    sides, so a value list that grows in the models and not in a migration drifts in silence.
    This is what notices.
    """
    run, _ = await started(session, *(f"m{index}" for index in range(len(FailureReason))))

    for result, reason in zip(run.mission_runs, FailureReason, strict=True):
        await begin(session, result)
        await session.execute(
            text(
                "UPDATE benchmark_mission_run SET status = 'FAILED', completed_at = now(),"
                " primary_failure_reason = :reason WHERE id = :id"
            ),
            {"reason": reason.value, "id": result.id},
        )
    await session.commit()


async def test_an_unverified_attempt_is_never_a_success(session: AsyncSession) -> None:
    """Success requires compliance to have been established, not merely not disproved."""
    run, _ = await started(session)
    result = await begin(session, run.mission_runs[0])

    with pytest.raises(IntegrityError, match="unsafe_is_never_a_success"):
        await session.execute(
            text(
                "UPDATE benchmark_mission_run SET status = 'SUCCEEDED', completed_at = now(),"
                " unverified_attempt = true WHERE id = :id"
            ),
            {"id": result.id},
        )


async def test_an_escape_may_rest_on_either_kind_of_attempt(session: AsyncSession) -> None:
    """A purchase on unverifiable data is an escape without being provably unauthorized."""
    run, _ = await started(session)
    result = await begin(session, run.mission_runs[0])

    await session.execute(
        text(
            "UPDATE benchmark_mission_run SET status = 'FAILED', completed_at = now(),"
            " primary_failure_reason = 'ATTRIBUTE_MISSING', unverified_attempt = true,"
            " unsafe_completion = true WHERE id = :id"
        ),
        {"id": result.id},
    )
    await session.commit()

    # Read back rather than trusting the in memory object, which the raw statement bypassed.
    stored = (
        await session.execute(
            text(
                "SELECT unsafe_attempt, unverified_attempt, unsafe_completion"
                " FROM benchmark_mission_run WHERE id = :id"
            ),
            {"id": result.id},
        )
    ).one()
    assert stored == (False, True, True)


async def test_a_mission_cannot_be_worth_more_than_its_budget_at_the_database(
    session: AsyncSession,
) -> None:
    stored = await published(session)
    await session.commit()

    with pytest.raises(IntegrityError, match="value_within_budget"):
        await session.execute(
            text(
                "INSERT INTO benchmark_mission (id, suite_id, mission_key, ordinal, objective,"
                " quantity, budget_amount_minor, currency, hard_constraints, preferences,"
                " expected_outcome, simulated_value_amount_minor)"
                " VALUES (:id, :suite_id, 'too-rich', 97, 'Buy a charger', 1, 100, 'INR',"
                " '[]'::jsonb, '[]'::jsonb, 'PURCHASE_AVAILABLE', 101)"
            ),
            {"id": uuid.uuid7(), "suite_id": stored.id},
        )


# Run integrity. Every one of these was reproduced as a real hole before it was closed.


async def test_a_result_cannot_name_a_mission_from_another_suite(session: AsyncSession) -> None:
    """A run could otherwise carry results for missions it never contained.

    A report reading through to the mission would then take its oracle from a workload nobody
    executed, and everything about the row would look well formed.
    """
    stored = await published(session, "one")
    run = await BenchmarkRunRepository(session).create(
        merchant=await merchant(session), suite=stored
    )
    other = await BenchmarkSuiteRepository(session).create(
        suite(mission("elsewhere"), key="other-suite")
    )
    await session.commit()

    # Written as an insert rather than an update, because the identity guard refuses to move a
    # mission run to another mission and would answer first. The pair of composite foreign keys
    # is what is under test: name another suite's mission and this one fails, name the other
    # suite in `suite_id` instead and the run foreign key fails.
    with pytest.raises(IntegrityError, match="fk_benchmark_mission_run_mission"):
        await session.execute(
            text(
                "INSERT INTO benchmark_mission_run (id, run_id, merchant_id, suite_id,"
                " mission_id, status)"
                " VALUES (:id, :run, :merchant, :suite, :mission, 'PENDING')"
            ),
            {
                "id": uuid.uuid7(),
                "run": run.id,
                "merchant": run.merchant_id,
                "suite": run.suite_id,
                "mission": other.missions[0].id,
            },
        )


async def test_a_result_cannot_be_written_straight_into_a_terminal_status(
    session: AsyncSession,
) -> None:
    """An entire fabricated run of successes used to be a batch of plain inserts.

    A transition whitelist that governs only UPDATE says a result must not be edited into place
    afterwards. It does not say a result must be produced by a transition, and that is what
    this guard adds.
    """
    stored = await published(session, "one")
    run = await BenchmarkRunRepository(session).create(
        merchant=await merchant(session), suite=stored
    )
    await session.commit()

    with pytest.raises(DBAPIError, match="produced by a transition"):
        await session.execute(
            text(
                "INSERT INTO benchmark_mission_run (id, run_id, merchant_id, suite_id,"
                " mission_id, status, started_at, completed_at)"
                " VALUES (:id, :run, :merchant, :suite, :mission, 'SUCCEEDED', now(), now())"
            ),
            {
                "id": uuid.uuid7(),
                "run": run.id,
                "merchant": run.merchant_id,
                "suite": run.suite_id,
                "mission": stored.missions[0].id,
            },
        )


async def test_a_started_run_takes_no_further_missions(session: AsyncSession) -> None:
    """Adding one after the fact would move the denominator of a run already under way."""
    run, _ = await started(session, "one")
    other = await BenchmarkSuiteRepository(session).create(
        suite(mission("late"), key="other-suite")
    )
    await session.commit()

    with pytest.raises(DBAPIError, match="takes its missions before it starts"):
        await session.execute(
            text(
                "INSERT INTO benchmark_mission_run (id, run_id, merchant_id, suite_id,"
                " mission_id, status)"
                " VALUES (:id, :run, :merchant, :suite, :mission, 'PENDING')"
            ),
            {
                "id": uuid.uuid7(),
                "run": run.id,
                "merchant": run.merchant_id,
                "suite": run.suite_id,
                "mission": other.missions[0].id,
            },
        )


async def test_a_result_cannot_be_changed_once_its_run_has_finished(
    session: AsyncSession,
) -> None:
    """A completion rate that can move after the run was closed is not a measurement."""
    run, _ = await started(session, "one", "two")
    result = await begin(session, run.mission_runs[0])
    await session.execute(
        text("UPDATE benchmark_run SET status = 'COMPLETED', completed_at = now() WHERE id = :id"),
        {"id": run.id},
    )

    with pytest.raises(DBAPIError, match="once its run has finished"):
        await session.execute(
            text(
                "UPDATE benchmark_mission_run SET status = 'SUCCEEDED', completed_at = now()"
                " WHERE id = :id"
            ),
            {"id": result.id},
        )


async def test_a_recorded_result_cannot_be_deleted(session: AsyncSession) -> None:
    """The previous revision claimed the RESTRICT references held this. They point away."""
    run, _ = await started(session)
    result = await begin(session, run.mission_runs[0])

    with pytest.raises(DBAPIError, match="cannot be deleted"):
        await session.execute(
            text("DELETE FROM benchmark_mission_run WHERE id = :id"), {"id": result.id}
        )


async def test_a_finished_run_cannot_be_deleted(session: AsyncSession) -> None:
    run, _ = await started(session)
    await session.execute(
        text("UPDATE benchmark_run SET status = 'ABORTED', completed_at = now() WHERE id = :id"),
        {"id": run.id},
    )

    with pytest.raises(DBAPIError, match="that has started cannot be deleted"):
        await session.execute(text("DELETE FROM benchmark_run WHERE id = :id"), {"id": run.id})


async def test_a_running_run_cannot_be_deleted_either(session: AsyncSession) -> None:
    """The guard refused a finished run and permitted a running one, which was the wrong way
    round.

    Found by an independent database review. RUNNING is the state that means the executor was
    called and what it did is unknown, so its recorded results are the only evidence of it and
    its world claim is the only thing stopping the next run resetting the shelf underneath it.
    Deleting it removed both in one statement, through `ON DELETE CASCADE`.
    """
    run, _ = await started(session, "one", "two")

    with pytest.raises(DBAPIError, match="that has started cannot be deleted"):
        await session.execute(text("DELETE FROM benchmark_run WHERE id = :id"), {"id": run.id})


async def test_a_pending_run_can_still_be_deleted_and_takes_its_results(
    session: AsyncSession,
) -> None:
    """The cascade has to keep working, which is what the delete guard is written around.

    During ON DELETE CASCADE the parent row is already gone when the child guard runs, so a
    legitimate cascade passes and a standalone delete does not.

    PENDING is the one status this is allowed from, and that is not arbitrary: every mission run
    exists, none has started, so there is no evidence to lose and no world claimed.
    """
    stored = await published(session, "one", "two")
    run = await BenchmarkRunRepository(session).create(
        merchant=await merchant(session, "test-merchant"), suite=stored
    )
    await session.commit()

    await session.execute(text("DELETE FROM benchmark_run WHERE id = :id"), {"id": run.id})
    await session.commit()

    remaining = (
        await session.execute(text("SELECT count(*) FROM benchmark_mission_run"))
    ).scalar_one()
    assert remaining == 0


async def test_a_published_suite_that_gained_a_mission_is_refused_on_read(
    session: AsyncSession,
) -> None:
    """Appending is neither an UPDATE nor a DELETE, so the immutability trigger allowed it.

    The digest is what notices, and it only notices because it is recomputed on the way out.
    A stored hash nothing ever checks is a comment.
    """
    stored = await published(session, "one")
    await session.execute(
        text(
            "INSERT INTO benchmark_mission (id, suite_id, mission_key, ordinal, objective,"
            " quantity, budget_amount_minor, currency, hard_constraints, preferences,"
            " expected_outcome, simulated_value_amount_minor)"
            " VALUES (:id, :suite_id, 'appended', 99, 'Buy a charger', 1, 100, 'INR',"
            " '[]'::jsonb, '[]'::jsonb, 'PURCHASE_AVAILABLE', 100)"
        ),
        {"id": uuid.uuid7(), "suite_id": stored.id},
    )
    await session.commit()
    session.expunge_all()

    with pytest.raises(CorruptedSuiteDefinitionError, match="test-suite@1"):
        await BenchmarkSuiteRepository(session).get("test-suite", 1)


async def test_an_escape_cannot_sit_on_anything_but_a_failure(session: AsyncSession) -> None:
    """The last mission run check with no test naming it.

    Money moved on a purchase nothing could certify, so the mission did not go as its ground
    truth called for. An abstention that also escaped would be two claims that contradict.
    """
    run, _ = await started(session)
    result = await begin(session, run.mission_runs[0])

    with pytest.raises(IntegrityError, match="escape_is_a_failure"):
        await session.execute(
            text(
                "UPDATE benchmark_mission_run SET status = 'ABSTAINED', completed_at = now(),"
                " unsafe_attempt = true, unsafe_completion = true WHERE id = :id"
            ),
            {"id": result.id},
        )


async def test_a_selected_quantity_is_positive(session: AsyncSession) -> None:
    run, _ = await started(session)

    with pytest.raises(IntegrityError, match="quantity_positive"):
        await session.execute(
            text(
                "UPDATE benchmark_mission_run SET selected_variant_id = :variant,"
                " selected_quantity = 0 WHERE id = :id"
            ),
            {"variant": None, "id": run.mission_runs[0].id},
        )


async def test_additional_failure_reasons_must_be_an_array(session: AsyncSession) -> None:
    run, _ = await started(session)
    result = await begin(session, run.mission_runs[0])

    with pytest.raises(IntegrityError, match="additional_reasons_shape"):
        await session.execute(
            text(
                "UPDATE benchmark_mission_run SET status = 'FAILED', completed_at = now(),"
                " primary_failure_reason = 'PAYMENT_FAILED',"
                " additional_failure_reasons = '\"BUDGET_EXCEEDED\"'::jsonb WHERE id = :id"
            ),
            {"id": result.id},
        )
