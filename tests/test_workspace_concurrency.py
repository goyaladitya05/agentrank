"""Two bootstrap commands arriving at once, forced rather than hoped for.

A bootstrap allocates a version number, registers a world under it and publishes a workload under
it, and every one of those is a read followed by a write. Two of them racing without a lock both
read the same highest version, both build, and the loser gets an integrity error on a fixture key
rather than the workspace the winner wrote.

Every test here uses a third transaction as a gate, exactly as the launch and payment admission
tests do. The gate holds the same per-merchant advisory lock a bootstrap takes, so both attempts
are provably queued before either can read anything. Two coroutines gathered without a gate take
their turns by accident, and a test that passes by accident would pass with no locking at all.
"""

import asyncio
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from workspace_support import catalogued, plain, source

from agentrank_api.benchmark.environment import BenchmarkEnvironmentService
from agentrank_api.benchmark.models import BenchmarkEnvironment, BenchmarkSuite
from agentrank_api.commerce.models import Merchant
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.errors import AgentRankError
from agentrank_api.representation.models import MerchantSourceSnapshot
from agentrank_api.representation.service import MerchantRepresentationService
from agentrank_api.workspace.definitions import BootstrapConfiguration
from agentrank_api.workspace.models import MerchantEvaluationWorkspace
from agentrank_api.workspace.projection import project_catalog
from agentrank_api.workspace.service import MerchantEvaluationWorkspaceService

pytestmark = pytest.mark.anyio

# A concurrent test that goes wrong blocks on a lock rather than failing, so every gather is
# bounded. Generous enough never to fire on a healthy database.
CONCURRENCY_TIMEOUT = 30

# How long an attempt is watched before concluding it is genuinely waiting on a lock.
LOCK_WAIT = 0.4


async def bootstrap_in_new_session(
    sessions: async_sessionmaker[AsyncSession],
    merchant_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    *,
    configuration: BootstrapConfiguration | None = None,
) -> uuid.UUID | AgentRankError:
    """One bootstrap on its own connection, so the two can genuinely race."""
    async with sessions() as session:
        try:
            outcome = await MerchantEvaluationWorkspaceService(session).bootstrap(
                merchant_id, source_snapshot_id=snapshot_id, configuration=configuration
            )
        except AgentRankError as refused:
            return refused
        return outcome.workspace.id


async def still_waiting(*attempts: asyncio.Task[object]) -> bool:
    """Whether every attempt is still blocked, watched for a bounded window."""
    done, _ = await asyncio.wait(set(attempts), timeout=LOCK_WAIT)
    return not done


async def merchant_with_source(
    session: AsyncSession, slug: str
) -> tuple[Merchant, MerchantSourceSnapshot]:
    merchant = await MerchantRepository(session).create(slug=slug, name=slug.title())
    await session.commit()
    snapshot = await MerchantRepresentationService(session).publish_source(catalogued(slug))
    return merchant, snapshot


async def counted(session: AsyncSession, model: type[object]) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def test_two_identical_bootstraps_build_one_workspace(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """A double submit is one command, so both callers are told about one workspace."""
    merchant, snapshot = await merchant_with_source(session, "race-shop")

    async with asyncio.timeout(CONCURRENCY_TIMEOUT):
        async with factory() as gate:
            await BenchmarkEnvironmentService(gate).claim(merchant.slug)
            attempts = [
                asyncio.create_task(bootstrap_in_new_session(factory, merchant.id, snapshot.id)),
                asyncio.create_task(bootstrap_in_new_session(factory, merchant.id, snapshot.id)),
            ]
            assert await still_waiting(*attempts)
            await gate.rollback()

        first, second = await asyncio.gather(*attempts)

    assert isinstance(first, uuid.UUID)
    assert isinstance(second, uuid.UUID)
    assert first == second
    assert await counted(session, MerchantEvaluationWorkspace) == 1
    assert await counted(session, BenchmarkEnvironment) == 1
    assert await counted(session, BenchmarkSuite) == 1


async def test_two_different_configurations_build_two_workspaces(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Version allocation is what two different commands race on, and the lock is what makes
    them queue rather than both claim version one."""
    merchant, snapshot = await merchant_with_source(session, "config-race-shop")

    async with asyncio.timeout(CONCURRENCY_TIMEOUT):
        async with factory() as gate:
            await BenchmarkEnvironmentService(gate).claim(merchant.slug)
            attempts = [
                asyncio.create_task(bootstrap_in_new_session(factory, merchant.id, snapshot.id)),
                asyncio.create_task(
                    bootstrap_in_new_session(
                        factory,
                        merchant.id,
                        snapshot.id,
                        configuration=BootstrapConfiguration(mission_budget=4),
                    )
                ),
            ]
            assert await still_waiting(*attempts)
            await gate.rollback()

        first, second = await asyncio.gather(*attempts)

    assert isinstance(first, uuid.UUID)
    assert isinstance(second, uuid.UUID)
    assert first != second
    assert await counted(session, MerchantEvaluationWorkspace) == 2
    versions = list(
        (
            await session.execute(
                select(BenchmarkEnvironment.fixture_version).order_by(
                    BenchmarkEnvironment.fixture_version
                )
            )
        ).scalars()
    )
    assert versions == [1, 2]


async def test_two_different_source_versions_do_not_collide(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Only the newest snapshot may be built from, so the older command is refused by name and
    no half-created world is left behind."""
    merchant, first = await merchant_with_source(session, "version-race-shop")
    newer = await MerchantRepresentationService(session).publish_source(
        source(*plain(merchant.slug).products, slug=merchant.slug, version=2)
    )

    async with asyncio.timeout(CONCURRENCY_TIMEOUT):
        async with factory() as gate:
            await BenchmarkEnvironmentService(gate).claim(merchant.slug)
            attempts = [
                asyncio.create_task(bootstrap_in_new_session(factory, merchant.id, first.id)),
                asyncio.create_task(bootstrap_in_new_session(factory, merchant.id, newer.id)),
            ]
            assert await still_waiting(*attempts)
            await gate.rollback()

        stale, current = await asyncio.gather(*attempts)

    assert isinstance(stale, AgentRankError)
    assert isinstance(current, uuid.UUID)
    assert await counted(session, MerchantEvaluationWorkspace) == 1
    assert await counted(session, BenchmarkEnvironment) == 1
    assert await counted(session, BenchmarkSuite) == 1


async def test_a_bootstrap_racing_a_world_registration_leaves_one_answer(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """The lock a bootstrap takes is the one an operator's world registration takes, so the two
    serialize rather than both deciding this merchant has no world yet."""
    merchant, snapshot = await merchant_with_source(session, "seed-race-shop")

    async def register() -> str:
        async with factory() as other:
            catalog = project_catalog(
                catalogued(merchant.slug),
                merchant_slug=merchant.slug,
                merchant_name=merchant.name,
                version=9,
            )
            await BenchmarkEnvironmentService(other).register(catalog.fixture)
            return catalog.fixture.label

    async with asyncio.timeout(CONCURRENCY_TIMEOUT):
        async with factory() as gate:
            await BenchmarkEnvironmentService(gate).claim(merchant.slug)
            attempts: list[asyncio.Task[object]] = [
                asyncio.create_task(bootstrap_in_new_session(factory, merchant.id, snapshot.id)),
                asyncio.create_task(register()),
            ]
            assert await still_waiting(*attempts)
            await gate.rollback()

        await asyncio.gather(*attempts)

    # Whichever went first, exactly one of the two worlds is a workspace and the other is not,
    # and no fixture version was claimed twice.
    versions = list(
        (
            await session.execute(
                select(BenchmarkEnvironment.fixture_version).order_by(
                    BenchmarkEnvironment.fixture_version
                )
            )
        ).scalars()
    )
    assert len(versions) == len(set(versions))
    assert await counted(session, MerchantEvaluationWorkspace) <= 1
