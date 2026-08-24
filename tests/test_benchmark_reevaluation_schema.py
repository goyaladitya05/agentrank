"""The database's own rules about a re-evaluation launch, with the service out of the path.

Every write here is raw SQL. A launch row carries invariants the application would also enforce,
and enforcing them only in the application means enforcing them only for callers who go through
it. What is asserted is what the database refuses when nobody is being careful: two pending
launches for one merchant, a repeated request key, a settled status that disagrees with the run
it names, a transition nobody should be able to make, an edited identity and a deletion.
"""

import uuid

import pytest
from reevaluation_support import LaunchWorld, build_launch_world
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.benchmark.lifecycle import BenchmarkRunStatus
from agentrank_api.benchmark.reevaluation import BenchmarkReevaluation
from agentrank_api.benchmark.runner import BenchmarkRunService

pytestmark = pytest.mark.anyio

INSERT = text("""
INSERT INTO benchmark_reevaluation (
    id, merchant_id, request_key, representation_id, compiler_run_id, suite_id,
    environment_id, buyer_profile, buyer_configuration, buyer_configuration_digest,
    executor_kind, status, run_id, baseline_run_id, requested_at
) VALUES (
    :id, :merchant_id, :request_key, :representation_id, :compiler_run_id, :suite_id,
    :environment_id, 'REFERENCE_BUYER', NULL, NULL,
    'reference-isolated', :status, NULL, NULL, now()
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
) -> uuid.UUID:
    identifier = uuid.uuid7()
    await session.execute(
        INSERT,
        {
            "id": identifier,
            "merchant_id": merchant_id or world.merchant_id,
            "request_key": request_key,
            "representation_id": representation_id or world.representation.id,
            "compiler_run_id": world.compiler_run_id,
            "suite_id": world.suite.id,
            "environment_id": world.environment.id,
            "status": status,
        },
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
                representation_id=theirs.representation.id,
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
                "UPDATE benchmark_reevaluation SET status = 'EXECUTING', run_id = :run,"
                " started_at = now() WHERE id = :id"
            ),
            {"run": run_id, "id": identifier},
        )
        await session.commit()

        # The run is still RUNNING, so neither settled status is allowed to claim otherwise.
        with pytest.raises(DBAPIError) as completed:
            await session.execute(
                text(
                    "UPDATE benchmark_reevaluation SET status = 'COMPLETED', settled_at = now()"
                    " WHERE id = :id"
                ),
                {"id": identifier},
            )
        assert "completed benchmark run" in str(completed.value)
        await session.rollback()

        with pytest.raises(DBAPIError) as failed:
            await session.execute(
                text(
                    "UPDATE benchmark_reevaluation SET status = 'FAILED',"
                    " failure_code = 'run_aborted', settled_at = now() WHERE id = :id"
                ),
                {"id": identifier},
            )
        assert "aborted one" in str(failed.value)
        await session.rollback()

        await BenchmarkRunService(session).abort_run(run_id, merchant_id=merchant_id)
        await session.execute(
            text(
                "UPDATE benchmark_reevaluation SET status = 'FAILED',"
                " failure_code = 'run_aborted', settled_at = now() WHERE id = :id"
            ),
            {"id": identifier},
        )
        await session.commit()
        settled = await session.get(BenchmarkReevaluation, identifier)
        assert settled is not None
        assert settled.status.value == "FAILED"

    async def test_a_settled_launch_frees_the_merchants_pending_slot(
        self, session: AsyncSession
    ) -> None:
        world = await build_launch_world(session, "slot-shop")
        identifier = await insert_launch(session, world, request_key="slot-request")
        await session.execute(
            text(
                "UPDATE benchmark_reevaluation SET status = 'FAILED',"
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
                    "UPDATE benchmark_reevaluation SET status = 'COMPLETED', settled_at = now()"
                    " WHERE id = :id"
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
                text("UPDATE benchmark_reevaluation SET suite_id = :suite WHERE id = :id"),
                {"suite": other.suite.id, "id": identifier},
            )
        assert "frozen at admission" in str(refused.value)
        await session.rollback()

    async def test_a_launch_cannot_be_deleted(self, session: AsyncSession) -> None:
        world = await build_launch_world(session, "durable-shop")
        identifier = await insert_launch(session, world, request_key="durable-request")

        with pytest.raises(DBAPIError) as refused:
            await session.execute(
                text("DELETE FROM benchmark_reevaluation WHERE id = :id"), {"id": identifier}
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
                "UPDATE benchmark_reevaluation SET status = 'EXECUTING', run_id = :run,"
                " started_at = now() WHERE id = :id"
            ),
            {"run": run_id, "id": first},
        )
        await session.commit()
        closed = await BenchmarkRunService(session).abort_run(run_id, merchant_id=merchant_id)
        assert closed.status is BenchmarkRunStatus.ABORTED
        await session.execute(
            text(
                "UPDATE benchmark_reevaluation SET status = 'FAILED',"
                " failure_code = 'run_aborted', settled_at = now() WHERE id = :id"
            ),
            {"id": first},
        )
        await session.commit()

        second = await insert_launch(session, world, request_key="binding-two")
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "UPDATE benchmark_reevaluation SET status = 'EXECUTING', run_id = :run,"
                    " started_at = now() WHERE id = :id"
                ),
                {"run": run_id, "id": second},
            )
            await session.commit()
        await session.rollback()
