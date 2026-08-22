"""At most one benchmark run may own a merchant's world, and PostgreSQL is what says so.

Two runs against one merchant are not two measurements. Each puts the world back before every
mission, so the second run's preparation resets the first run's shelf between two of its missions
and releases what it was holding. Both runs commit, both carry a catalog pin, and both are quietly
wrong.

The mechanism is a partial unique index over `benchmark_run (merchant_id) WHERE status =
'RUNNING'`, not an application check and not a lock held for the duration of a run. So the tests
that matter here are the ones that go around the application: a second run written straight
through the repository on its own connection has to be refused by the database, and a test that
still passed with the index dropped would be a test of a Python `if`.

Everything cross connection uses the `factory` fixture rather than one session. Two coroutines on
one connection take their turns because they have to, which proves nothing about what PostgreSQL
would do with two.
"""

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentrank_api.benchmark.buyer import MerchantBuyerSurface
from agentrank_api.benchmark.definitions import (
    AgentMissionBrief,
    BenchmarkMissionDefinition,
    BenchmarkSuiteDefinition,
    ExpectedOutcome,
    MissionOracle,
)
from agentrank_api.benchmark.environment import BenchmarkEnvironmentService
from agentrank_api.benchmark.fixtures import BenchmarkFixture
from agentrank_api.benchmark.lifecycle import BenchmarkRunStatus
from agentrank_api.benchmark.models import BenchmarkRun
from agentrank_api.benchmark.reference_executor import ReferenceMissionExecutor
from agentrank_api.benchmark.repository import BenchmarkRunRepository, BenchmarkSuiteRepository
from agentrank_api.benchmark.runner import BenchmarkRunService
from agentrank_api.benchmark.suites import BenchmarkSuiteService
from agentrank_api.commerce.catalog_fixture import SeedProduct, SeedVariant
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.conflicts import translated_conflicts
from agentrank_api.constraints.rules import ConstraintOperator
from agentrank_api.errors import ConflictError
from agentrank_api.mandates.intent import AllowedCategory, MaxTotalAmount, RequiredAttribute
from agentrank_api.payments.fake import FakePaymentProvider

pytestmark = pytest.mark.anyio

CURRENCY = "INR"
PRICE = 100000
SLUG = "exclusivity-shop"
OTHER_SLUG = "exclusivity-other-shop"


def world(slug: str) -> BenchmarkFixture:
    """A registered benchmark world for one merchant, with one thing on the shelf."""
    return BenchmarkFixture(
        key=f"{slug}-catalog",
        version=1,
        merchant_slug=slug,
        merchant_name=slug,
        products=(
            SeedProduct(
                external_id=f"{slug}-chg",
                title="Charger",
                description=None,
                category="chargers",
                variants=(
                    SeedVariant(
                        sku=f"{slug}-blk".upper(),
                        label="Black",
                        price_amount_minor=PRICE,
                        currency=CURRENCY,
                        inventory_quantity=3,
                        attributes={"color": "black"},
                    ),
                ),
            ),
        ),
    )


WORLD = world(SLUG)
OTHER_WORLD = world(OTHER_SLUG)


def suite_key(slug: str) -> str:
    """One suite per merchant. A suite key is global, and it names the merchant it was authored
    against, so two worlds in one test cannot share one."""
    return f"{slug}-suite"


def suite_of(slug: str, *keys: str) -> BenchmarkSuiteDefinition:
    return BenchmarkSuiteDefinition(
        key=suite_key(slug),
        version=1,
        merchant_slug=slug,
        name="Exclusivity suite",
        missions=tuple(
            BenchmarkMissionDefinition(
                brief=AgentMissionBrief(
                    key=key,
                    objective="Buy one black charger.",
                    budget=MaxTotalAmount(amount_minor=PRICE, currency=CURRENCY),
                    hard_constraints=(
                        AllowedCategory("chargers"),
                        RequiredAttribute("color", "black", ConstraintOperator.EQ),
                    ),
                ),
                oracle=MissionOracle(
                    expected_outcome=ExpectedOutcome.PURCHASE_AVAILABLE,
                    simulated_value_amount_minor=PRICE,
                ),
            )
            for key in keys
        ),
    )


async def prepared(session: AsyncSession, fixture: BenchmarkFixture = WORLD) -> uuid.UUID:
    """Register the world, publish a one mission suite against it, and return the merchant."""
    environments = BenchmarkEnvironmentService(session)
    environment = await environments.register(fixture)
    await environments.prepare(fixture)
    await BenchmarkSuiteService(session).publish(suite_of(fixture.merchant_slug, "one"))
    await session.commit()
    return environment.merchant_id


async def started(session: AsyncSession, slug: str = SLUG) -> BenchmarkRun:
    return await BenchmarkRunService(session).start_run(
        suite_key=suite_key(slug), suite_version=1, merchant_slug=slug
    )


async def forced(session: AsyncSession, slug: str = SLUG) -> None:
    """Write a RUNNING run straight through the repository, with no service check in the way.

    This is what makes the tests below tests of the database. `BenchmarkRunService.start_run`
    reads the active run first and refuses politely, so a test that only drove the service would
    keep passing with the index dropped.
    """
    merchant = await MerchantRepository(session).get_by_slug(slug)
    assert merchant is not None
    suite = await BenchmarkSuiteRepository(session).get(suite_key(slug), 1)
    assert suite is not None
    run = await BenchmarkRunRepository(session).create(merchant=merchant, suite=suite)
    run.status = BenchmarkRunStatus.RUNNING
    run.started_at = datetime.now(UTC)
    async with translated_conflicts(session, identifier=str(merchant.id)):
        await session.commit()


# One owner at a time.


async def test_a_second_run_is_refused_while_one_is_executing(session: AsyncSession) -> None:
    """The ordinary answer, and it names the run an operator has to close."""
    await prepared(session)
    first = await started(session)

    with pytest.raises(ConflictError) as raised:
        await started(session)

    assert raised.value.reason == "run_already_active"
    assert raised.value.identifier == str(first.id)


async def test_postgresql_refuses_a_second_active_run_when_nothing_checks_first(
    factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """The invariant, on the layer that cannot be bypassed.

    The second run is written by the repository on its own connection, so no application check
    is involved and nothing in Python decides the answer. Drop the partial unique index and this
    test is the one that turns red, which is the whole reason it is written this way.
    """
    await prepared(session)
    await started(session)
    await session.commit()

    async with factory() as other:
        with pytest.raises(ConflictError) as raised:
            await forced(other)

    assert raised.value.reason == "run_already_active"


async def test_two_concurrent_starts_produce_one_owner_and_one_refusal(
    factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """Two processes, one world, and no schedule in which both proceed.

    Independent connections, so both can read that no run is active before either commits. One
    of them wins the index and the other is refused, whichever order they happen to arrive in.
    """
    await prepared(session)

    async def start() -> BenchmarkRun:
        async with factory() as own:
            return await started(own)

    outcomes = await asyncio.gather(start(), start(), return_exceptions=True)

    owners = [result for result in outcomes if isinstance(result, BenchmarkRun)]
    refusals = [result for result in outcomes if isinstance(result, ConflictError)]
    assert len(owners) == 1
    assert len(refusals) == 1
    assert refusals[0].reason == "run_already_active"


async def test_two_merchants_run_at_the_same_time(session: AsyncSession) -> None:
    """The claim is per world. Serializing the whole benchmark system would be a different bug."""
    await prepared(session)
    await prepared(session, OTHER_WORLD)

    mine = await started(session)
    theirs = await started(session, OTHER_SLUG)

    assert mine.status is BenchmarkRunStatus.RUNNING
    assert theirs.status is BenchmarkRunStatus.RUNNING
    assert mine.merchant_id != theirs.merchant_id


# Releasing the claim.


async def test_aborting_a_run_releases_the_world(session: AsyncSession) -> None:
    merchant_id = await prepared(session)
    service = BenchmarkRunService(session)
    first = await started(session)
    await service.start_mission(first.id, "one", merchant_id=merchant_id)
    await service.abort_run(first.id, merchant_id=merchant_id)

    second = await started(session)

    assert second.status is BenchmarkRunStatus.RUNNING


async def test_a_finished_suite_leaves_the_world_free_for_the_next_run(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """A whole run through `run_suite`, twice. The second one is what the claim must not block."""
    merchant_id = await prepared(session)
    service = BenchmarkRunService(session)
    surface = MerchantBuyerSurface(factory, merchant_id=merchant_id, provider=FakePaymentProvider())

    first = await service.run_suite(
        ReferenceMissionExecutor(surface),
        suite_key=suite_key(SLUG),
        suite_version=1,
        fixture=WORLD,
    )
    second = await service.run_suite(
        ReferenceMissionExecutor(surface),
        suite_key=suite_key(SLUG),
        suite_version=1,
        fixture=WORLD,
    )

    assert first.status is BenchmarkRunStatus.COMPLETED
    assert second.status is BenchmarkRunStatus.COMPLETED


# What a crash leaves behind.


async def test_a_run_left_running_by_a_crash_keeps_the_world_claimed(
    factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """A process that dies owning a world leaves the claim standing, on purpose.

    What that run did is unknown. Letting the next one start and reset the world would release
    whatever it was holding and destroy the only evidence of it, so the refusal is the correct
    behaviour rather than a leak somebody has to sweep up.
    """
    await prepared(session)
    async with factory() as dying:
        abandoned = await started(dying)
        await dying.commit()

    with pytest.raises(ConflictError) as raised:
        await started(session)

    assert raised.value.identifier == str(abandoned.id)


async def test_aborting_an_abandoned_run_is_what_makes_the_world_reusable(
    factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """The documented recovery, and it is auditable: the run keeps its own honest status."""
    merchant_id = await prepared(session)
    async with factory() as dying:
        abandoned = await started(dying)
        await dying.commit()

    closed = await BenchmarkRunService(session).abort_run(abandoned.id, merchant_id=merchant_id)
    recovered = await started(session)

    assert closed.status is BenchmarkRunStatus.ABORTED
    assert recovered.status is BenchmarkRunStatus.RUNNING


# Resetting the world is claimed too.


async def test_preparing_a_world_is_refused_while_another_run_owns_it(
    factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """The half a unique index cannot state.

    An operator reseeding the world mid run does not create a second run, so no constraint on
    the run table sees it. It does exactly the damage two runs would do, by a shorter route.
    """
    await prepared(session)
    async with factory() as owner:
        await started(owner)
        await owner.commit()

    with pytest.raises(ConflictError) as raised:
        await BenchmarkEnvironmentService(session).prepare(WORLD)

    assert raised.value.reason == "run_already_active"


async def test_a_run_may_put_its_own_world_back_between_missions(session: AsyncSession) -> None:
    """Naming the run is what separates its own preparation from somebody else's."""
    await prepared(session)
    run = await started(session)

    prepared_again = await BenchmarkEnvironmentService(session).prepare(WORLD, for_run=run.id)

    assert prepared_again.environment.merchant_id == run.merchant_id


async def test_preparing_another_merchants_world_is_never_blocked(session: AsyncSession) -> None:
    await prepared(session)
    await prepared(session, OTHER_WORLD)
    await started(session)

    elsewhere = await BenchmarkEnvironmentService(session).prepare(OTHER_WORLD)

    assert elsewhere.environment.merchant_slug == OTHER_SLUG
