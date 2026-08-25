"""The database's own rules about an evaluation launch, with the service out of the path.

Every write here is raw SQL. A launch row carries invariants the application would also enforce,
and enforcing them only in the application means enforcing them only for callers who go through
it. What is asserted is what the database refuses when nobody is being careful: two pending
launches for one merchant, a repeated request key, a settled status that disagrees with the run
it names, a transition nobody should be able to make, an edited identity and a deletion.

The purpose rules are here for the same reason. Which artifact a launch names is decided by
which kind of evaluation it is, and an initial evaluation carries no prior run to be read
against; both are check constraints rather than service code, so a writer that never went
through the service cannot record an evaluation of something it did not measure.
"""

import uuid

import pytest
from launch_support import LaunchWorld, build_launch_world
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.benchmark.evaluation_launch import (
    BenchmarkEvaluationLaunch,
    EvaluationPurpose,
)
from agentrank_api.benchmark.execution import ExecutorIdentity
from agentrank_api.benchmark.lifecycle import BenchmarkRunStatus
from agentrank_api.benchmark.runner import BenchmarkRunService

pytestmark = pytest.mark.anyio

INSERT = text("""
INSERT INTO benchmark_evaluation_launch (
    id, merchant_id, request_key, purpose, representation_id, compiler_run_id,
    source_snapshot_id, suite_id,
    environment_id, buyer_profile, buyer_configuration, buyer_configuration_digest,
    executor_kind, status, run_id, baseline_run_id, requested_at
) VALUES (
    :id, :merchant_id, :request_key, :purpose, :representation_id, :compiler_run_id,
    :source_snapshot_id, :suite_id,
    :environment_id, 'REFERENCE_BUYER', NULL, NULL,
    'reference-isolated', :status, NULL, :baseline_run_id, now()
)
""")


async def insert_launch(
    session: AsyncSession,
    world: LaunchWorld,
    *,
    request_key: str,
    status: str = "QUEUED",
    merchant_id: uuid.UUID | None = None,
    representation_id: uuid.UUID | None = None,
    purpose: str = "REEVALUATION",
    compiler_run_id: uuid.UUID | None = None,
    source_snapshot_id: uuid.UUID | None = None,
    baseline_run_id: uuid.UUID | None = None,
    omit: str | None = None,
) -> uuid.UUID:
    """One launch row written with no service in the path.

    The re-evaluation shape is the default because it is what most of these assertions are
    about. Every identity column is still overridable one at a time, and `omit` writes null into
    one the purpose would otherwise fill, which is what lets a test write the combinations a
    service would never produce.
    """
    initial = purpose == "INITIAL"
    identifier = uuid.uuid7()
    await session.execute(
        INSERT,
        {
            "id": identifier,
            "merchant_id": merchant_id or world.merchant_id,
            "request_key": request_key,
            "purpose": purpose,
            "representation_id": (
                representation_id
                if representation_id is not None
                else (None if initial else world.representation_id)
            ),
            "compiler_run_id": (
                compiler_run_id
                if compiler_run_id is not None
                else (None if initial else world.compiler_run_id)
            ),
            "source_snapshot_id": (
                source_snapshot_id
                if source_snapshot_id is not None
                else (world.source_snapshot_id if initial else None)
            ),
            "suite_id": world.suite_id,
            "environment_id": world.environment_id,
            "status": status,
            "baseline_run_id": baseline_run_id,
        }
        | ({} if omit is None else {omit: None}),
    )
    await session.commit()
    return identifier


class TestAdmissionRules:
    async def test_a_second_pending_launch_for_one_merchant_is_refused(
        self, session: AsyncSession
    ) -> None:
        world = await build_launch_world(session, "pending-shop")
        await insert_launch(session, world, request_key="first-request")

        with pytest.raises(IntegrityError):
            await insert_launch(session, world, request_key="second-request")
        await session.rollback()

    async def test_one_request_key_per_merchant(self, session: AsyncSession) -> None:
        world = await build_launch_world(session, "keyed-shop")
        await insert_launch(session, world, request_key="same-request")

        with pytest.raises(IntegrityError):
            await insert_launch(session, world, request_key="same-request")
        await session.rollback()

    async def test_a_launch_is_admitted_queued_and_never_written_settled(
        self, session: AsyncSession
    ) -> None:
        world = await build_launch_world(session, "written-shop")

        with pytest.raises(DBAPIError) as refused:
            await insert_launch(session, world, request_key="fabricated", status="COMPLETED")
        assert "admitted queued" in str(refused.value)
        await session.rollback()

    async def test_another_merchants_representation_cannot_be_named(
        self, session: AsyncSession
    ) -> None:
        mine = await build_launch_world(session, "mine-launch-shop")
        theirs = await build_launch_world(session, "theirs-launch-shop")

        with pytest.raises(IntegrityError):
            await insert_launch(
                session,
                mine,
                request_key="cross-tenant",
                representation_id=theirs.representation_id,
            )
        await session.rollback()


class TestPurposeShape:
    """Which artifact a launch names, decided by the kind of evaluation it is."""

    async def test_an_initial_evaluation_cannot_also_name_a_representation(
        self, session: AsyncSession
    ) -> None:
        world = await build_launch_world(session, "initial-shape-shop")

        with pytest.raises(IntegrityError):
            await insert_launch(
                session,
                world,
                request_key="initial-with-ir",
                purpose="INITIAL",
                source_snapshot_id=world.source_snapshot_id,
                representation_id=world.representation_id,
                compiler_run_id=world.compiler_run_id,
            )
        await session.rollback()

    async def test_an_initial_evaluation_names_the_merchant_state_it_measured(
        self, session: AsyncSession
    ) -> None:
        world = await build_launch_world(session, "initial-source-shop")

        with pytest.raises(IntegrityError):
            await insert_launch(
                session,
                world,
                request_key="initial-no-source",
                purpose="INITIAL",
                omit="source_snapshot_id",
            )
        await session.rollback()

        launched = await insert_launch(
            session, world, request_key="initial-with-source", purpose="INITIAL"
        )
        stored = await session.get(BenchmarkEvaluationLaunch, launched)
        assert stored is not None
        assert stored.purpose is EvaluationPurpose.INITIAL
        assert stored.source_snapshot_id == world.source_snapshot_id
        assert stored.representation_id is None
        assert stored.compiler_run_id is None

    async def test_a_reevaluation_cannot_name_a_source_snapshot(
        self, session: AsyncSession
    ) -> None:
        world = await build_launch_world(session, "reeval-shape-shop")

        with pytest.raises(IntegrityError):
            await insert_launch(
                session,
                world,
                request_key="reeval-with-source",
                source_snapshot_id=world.source_snapshot_id,
            )
        await session.rollback()

    async def test_a_reevaluation_names_the_representation_it_measured(
        self, session: AsyncSession
    ) -> None:
        world = await build_launch_world(session, "reeval-artifact-shop")

        with pytest.raises(IntegrityError):
            await insert_launch(
                session, world, request_key="reeval-no-ir", omit="representation_id"
            )
        await session.rollback()

        with pytest.raises(IntegrityError):
            await insert_launch(session, world, request_key="reeval-no-run", omit="compiler_run_id")
        await session.rollback()

    async def test_an_initial_evaluation_cannot_carry_a_baseline_run(
        self, session: AsyncSession
    ) -> None:
        """A merchant's first evaluation has no before, and the schema holds that rather than
        trusting every writer to remember it."""
        world = await build_launch_world(session, "initial-baseline-shop")
        run = await BenchmarkRunService(session).start_run(
            suite_key=world.suite_key,
            suite_version=world.suite_version,
            merchant_slug=world.merchant_slug,
            environment=world.environment,
            executor=ExecutorIdentity(kind="reference-isolated", version=1),
        )

        with pytest.raises(IntegrityError):
            await insert_launch(
                session,
                world,
                request_key="initial-baseline",
                purpose="INITIAL",
                baseline_run_id=run.id,
            )
        await session.rollback()

    async def test_another_merchants_source_snapshot_cannot_be_named(
        self, session: AsyncSession
    ) -> None:
        mine = await build_launch_world(session, "mine-source-shop")
        theirs = await build_launch_world(session, "theirs-source-shop")

        with pytest.raises(IntegrityError):
            await insert_launch(
                session,
                mine,
                request_key="cross-tenant-source",
                purpose="INITIAL",
                source_snapshot_id=theirs.source_snapshot_id,
            )
        await session.rollback()


class TestLifecycle:
    async def test_a_settled_status_must_agree_with_the_run_it_names(
        self, session: AsyncSession
    ) -> None:
        world = await build_launch_world(session, "settled-shop")
        identifier = await insert_launch(session, world, request_key="settle-request")
        started = await BenchmarkRunService(session).start_run(
            suite_key=world.suite.suite_key,
            suite_version=world.suite.version,
            merchant_slug=world.merchant_slug,
        )
        # Held as plain values: the deliberate rollbacks below expire every loaded instance.
        run_id, merchant_id = started.id, world.merchant_id
        await session.execute(
            text(
                "UPDATE benchmark_evaluation_launch SET status = 'EXECUTING', run_id = :run,"
                " started_at = now() WHERE id = :id"
            ),
            {"run": run_id, "id": identifier},
        )
        await session.commit()

        # The run is still RUNNING, so neither settled status is allowed to claim otherwise.
        with pytest.raises(DBAPIError) as completed:
            await session.execute(
                text(
                    "UPDATE benchmark_evaluation_launch"
                    " SET status = 'COMPLETED', settled_at = now() WHERE id = :id"
                ),
                {"id": identifier},
            )
        assert "completed benchmark run" in str(completed.value)
        await session.rollback()

        with pytest.raises(DBAPIError) as failed:
            await session.execute(
                text(
                    "UPDATE benchmark_evaluation_launch SET status = 'FAILED',"
                    " failure_code = 'run_aborted', settled_at = now() WHERE id = :id"
                ),
                {"id": identifier},
            )
        assert "aborted one" in str(failed.value)
        await session.rollback()

        await BenchmarkRunService(session).abort_run(run_id, merchant_id=merchant_id)
        await session.execute(
            text(
                "UPDATE benchmark_evaluation_launch SET status = 'FAILED',"
                " failure_code = 'run_aborted', settled_at = now() WHERE id = :id"
            ),
            {"id": identifier},
        )
        await session.commit()
        settled = await session.get(BenchmarkEvaluationLaunch, identifier)
        assert settled is not None
        assert settled.status.value == "FAILED"

    async def test_a_settled_launch_frees_the_merchants_pending_slot(
        self, session: AsyncSession
    ) -> None:
        world = await build_launch_world(session, "slot-shop")
        identifier = await insert_launch(session, world, request_key="slot-request")
        await session.execute(
            text(
                "UPDATE benchmark_evaluation_launch SET status = 'FAILED',"
                " failure_code = 'provider_credential_unavailable', settled_at = now()"
                " WHERE id = :id"
            ),
            {"id": identifier},
        )
        await session.commit()

        second = await insert_launch(session, world, request_key="slot-request-two")
        assert second != identifier

    async def test_status_transitions_are_whitelisted(self, session: AsyncSession) -> None:
        world = await build_launch_world(session, "transition-shop")
        identifier = await insert_launch(session, world, request_key="transition-request")

        with pytest.raises(DBAPIError) as refused:
            await session.execute(
                text(
                    "UPDATE benchmark_evaluation_launch"
                    " SET status = 'COMPLETED', settled_at = now() WHERE id = :id"
                ),
                {"id": identifier},
            )
        assert "cannot go from QUEUED to COMPLETED" in str(refused.value)
        await session.rollback()

    async def test_frozen_identity_cannot_be_edited(self, session: AsyncSession) -> None:
        world = await build_launch_world(session, "frozen-shop")
        other = await build_launch_world(session, "other-frozen-shop")
        identifier = await insert_launch(session, world, request_key="frozen-request")

        with pytest.raises(DBAPIError) as refused:
            await session.execute(
                text("UPDATE benchmark_evaluation_launch SET suite_id = :suite WHERE id = :id"),
                {"suite": other.suite.id, "id": identifier},
            )
        assert "frozen at admission" in str(refused.value)
        await session.rollback()

    async def test_a_launch_cannot_settle_before_it_started(self, session: AsyncSession) -> None:
        """All three instants come from the database's clock, so ordering is the table's rule."""
        world = await build_launch_world(session, "ordered-shop")
        identifier = await insert_launch(session, world, request_key="ordered-request")

        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "UPDATE benchmark_evaluation_launch SET status = 'FAILED',"
                    " failure_code = 'run_aborted', started_at = now(),"
                    " settled_at = now() - interval '1 hour' WHERE id = :id"
                ),
                {"id": identifier},
            )
            await session.commit()
        await session.rollback()

    async def test_a_launch_cannot_be_deleted(self, session: AsyncSession) -> None:
        world = await build_launch_world(session, "durable-shop")
        identifier = await insert_launch(session, world, request_key="durable-request")

        with pytest.raises(DBAPIError) as refused:
            await session.execute(
                text("DELETE FROM benchmark_evaluation_launch WHERE id = :id"), {"id": identifier}
            )
        assert "cannot be deleted" in str(refused.value)
        await session.rollback()


class TestRunBinding:
    async def test_one_benchmark_run_belongs_to_at_most_one_launch(
        self, session: AsyncSession
    ) -> None:
        world = await build_launch_world(session, "binding-shop")
        first = await insert_launch(session, world, request_key="binding-one")
        started = await BenchmarkRunService(session).start_run(
            suite_key=world.suite.suite_key,
            suite_version=world.suite.version,
            merchant_slug=world.merchant_slug,
        )
        run_id, merchant_id = started.id, world.merchant_id
        await session.execute(
            text(
                "UPDATE benchmark_evaluation_launch SET status = 'EXECUTING', run_id = :run,"
                " started_at = now() WHERE id = :id"
            ),
            {"run": run_id, "id": first},
        )
        await session.commit()
        closed = await BenchmarkRunService(session).abort_run(run_id, merchant_id=merchant_id)
        assert closed.status is BenchmarkRunStatus.ABORTED
        await session.execute(
            text(
                "UPDATE benchmark_evaluation_launch SET status = 'FAILED',"
                " failure_code = 'run_aborted', settled_at = now() WHERE id = :id"
            ),
            {"id": first},
        )
        await session.commit()

        second = await insert_launch(session, world, request_key="binding-two")
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "UPDATE benchmark_evaluation_launch SET status = 'EXECUTING', run_id = :run,"
                    " started_at = now() WHERE id = :id"
                ),
                {"run": run_id, "id": second},
            )
            await session.commit()
        await session.rollback()
