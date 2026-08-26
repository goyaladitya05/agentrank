"""Two launch commands arriving at once, forced rather than hoped for.

A launch is a write, and the invariant it defends is expensive to get wrong: two launches for
one merchant become two benchmark runs against one world, each resetting the other's shelf
between its own missions. Both would commit and both would be wrong with nothing on either to
show it.

Every test here uses a third transaction as a gate, exactly as the payment admission tests do.
The gate holds the same advisory lock a launch takes, so both attempts are provably queued
before either can read whether a launch already exists. Two coroutines gathered without a gate
take their turns by accident, and a test that passes by accident would pass with no locking at
all.
"""

import asyncio
import uuid

import pytest
from launch_support import (
    InitialWorld,
    LaunchWorld,
    build_initial_world,
    build_launch_world,
    complete_run,
    queue_launch,
    without_providers,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentrank_api.benchmark.environment import BenchmarkEnvironmentService
from agentrank_api.benchmark.evaluation_launch import (
    BenchmarkEvaluationLaunch,
    EvaluationLaunchStatus,
)
from agentrank_api.benchmark.launch import (
    EvaluationLaunchWorkerService,
    MerchantEvaluationLaunchService,
    worker_executor_kinds,
)
from agentrank_api.config import Settings
from agentrank_api.errors import ConflictError

pytestmark = pytest.mark.anyio

# A concurrent test that goes wrong blocks on a lock rather than failing, so every gather is
# bounded. Generous enough never to fire on a healthy database.
CONCURRENCY_TIMEOUT = 30

# How long an attempt is watched before concluding it is genuinely waiting on a lock.
LOCK_WAIT = 0.4


async def launch_in_new_session(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
    world: LaunchWorld | InitialWorld,
    *,
    request_key: str,
) -> uuid.UUID | ConflictError:
    """One launch on its own connection, so the two can genuinely race."""
    async with sessions() as session:
        try:
            return await queue_launch(
                session, without_providers(settings), world, request_key=request_key
            )
        except ConflictError as refused:
            return refused


async def still_waiting(*attempts: asyncio.Task[object]) -> bool:
    """Whether every attempt is still blocked, watched for a bounded window."""
    done, _ = await asyncio.wait(set(attempts), timeout=LOCK_WAIT)
    return not done


async def launch_count(session: AsyncSession) -> int:
    return int(
        await session.scalar(select(func.count()).select_from(BenchmarkEvaluationLaunch)) or 0
    )


async def test_two_different_requests_admit_one_launch(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    """One wins and the other is refused by name, rather than both queueing a run."""
    world = await build_launch_world(session, "race-shop")

    async with asyncio.timeout(CONCURRENCY_TIMEOUT):
        async with factory() as gate:
            await BenchmarkEnvironmentService(gate).claim(world.merchant_slug)
            attempts: list[asyncio.Task[uuid.UUID | ConflictError]] = [
                asyncio.create_task(
                    launch_in_new_session(factory, settings, world, request_key="race-one")
                ),
                asyncio.create_task(
                    launch_in_new_session(factory, settings, world, request_key="race-two")
                ),
            ]
            assert await still_waiting(*attempts)
            await gate.rollback()

        first, second = await asyncio.gather(*attempts)

    outcomes = [first, second]
    admitted = [outcome for outcome in outcomes if isinstance(outcome, uuid.UUID)]
    refused = [outcome for outcome in outcomes if isinstance(outcome, ConflictError)]
    assert len(admitted) == 1
    assert len(refused) == 1
    assert refused[0].reason == "evaluation_already_pending"
    assert await launch_count(session) == 1


async def test_two_identical_requests_answer_with_one_launch(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    """A double submit is one logical command, so both callers are told about one launch."""
    world = await build_launch_world(session, "double-shop")

    async with asyncio.timeout(CONCURRENCY_TIMEOUT):
        async with factory() as gate:
            await BenchmarkEnvironmentService(gate).claim(world.merchant_slug)
            attempts: list[asyncio.Task[uuid.UUID | ConflictError]] = [
                asyncio.create_task(
                    launch_in_new_session(factory, settings, world, request_key="same-submit")
                ),
                asyncio.create_task(
                    launch_in_new_session(factory, settings, world, request_key="same-submit")
                ),
            ]
            assert await still_waiting(*attempts)
            await gate.rollback()

        first, second = await asyncio.gather(*attempts)

    assert isinstance(first, uuid.UUID)
    assert isinstance(second, uuid.UUID)
    assert first == second
    assert await launch_count(session) == 1


async def test_two_first_evaluations_admit_one_launch(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    """A merchant with no evidence cannot accidentally create two first measurements.

    The same invariant the re-evaluation race proves, on the command that a merchant reaches
    before they have anything at all: two admitted launches would be two runs resetting one
    world's shelf between each other's missions, and both would look fine afterwards.
    """
    world = await build_initial_world(session, "first-race-shop")

    async with asyncio.timeout(CONCURRENCY_TIMEOUT):
        async with factory() as gate:
            await BenchmarkEnvironmentService(gate).claim(world.merchant_slug)
            attempts: list[asyncio.Task[uuid.UUID | ConflictError]] = [
                asyncio.create_task(
                    launch_in_new_session(factory, settings, world, request_key="first-race-one")
                ),
                asyncio.create_task(
                    launch_in_new_session(factory, settings, world, request_key="first-race-two")
                ),
            ]
            assert await still_waiting(*attempts)
            await gate.rollback()

        outcomes = list(await asyncio.gather(*attempts))

    admitted = [outcome for outcome in outcomes if isinstance(outcome, uuid.UUID)]
    refused = [outcome for outcome in outcomes if isinstance(outcome, ConflictError)]
    assert len(admitted) == 1
    assert len(refused) == 1
    assert refused[0].reason == "evaluation_already_pending"
    assert await launch_count(session) == 1


async def test_a_running_evaluation_cannot_be_withdrawn(
    settings: Settings, session: AsyncSession
) -> None:
    """The one branch protecting a live run from a merchant's own button.

    A queued launch has produced nothing: no mission has executed, no stock has been held and no
    payment has been attempted, so closing one destroys no evidence. An executing launch is the
    opposite of all four, and closing one from the console would leave a run nothing names.
    """
    quiet = without_providers(settings)
    world = await build_launch_world(session, "withdraw-running-shop")
    launch_id = await queue_launch(session, quiet, world, request_key="withdraw-running")
    worker = EvaluationLaunchWorkerService(session)
    claimed = await worker.claim_next(
        world.merchant_id,
        environment_id=world.environment_id,
        executor_kinds=worker_executor_kinds(quiet),
    )
    assert claimed is not None
    run_id = await complete_run(session, world)
    await worker.bind_run(launch_id, run_id=run_id)
    await session.commit()

    with pytest.raises(ConflictError) as refused:
        await MerchantEvaluationLaunchService(session, quiet).withdraw(world.merchant_id, launch_id)

    assert refused.value.reason in {"launch_not_queued", "run_already_active"}
    settled = await session.get(BenchmarkEvaluationLaunch, launch_id)
    assert settled is not None
    await session.refresh(settled)
    assert settled.status is not EvaluationLaunchStatus.FAILED
