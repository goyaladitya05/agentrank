"""Executing a whole suite: isolation, identity, lifecycle and what a crash leaves behind.

The first two groups are the Phase 2B properties that a benchmark is worthless without. Missions
must not contaminate each other and runs must not contaminate later runs, and the failure mode is
silent: every number still looks like a number. Both are asserted against real purchases through
the real payment kernel rather than against a helper that promises to reset something.

Everything here drives the reference executor rather than a replay fixture, because a replayed
run cannot contaminate anything and would prove nothing about isolation.
"""

import uuid
from dataclasses import replace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentrank_api.benchmark.buyer import MerchantBuyerSurface
from agentrank_api.benchmark.catalog import catalog_content_hash
from agentrank_api.benchmark.definitions import (
    AgentMissionBrief,
    BenchmarkMissionDefinition,
    BenchmarkSuiteDefinition,
    ExpectedOutcome,
    MissionOracle,
)
from agentrank_api.benchmark.environment import BenchmarkEnvironmentService
from agentrank_api.benchmark.evaluation import evaluator_version
from agentrank_api.benchmark.execution import ExecutorIdentity
from agentrank_api.benchmark.fixtures import BenchmarkFixture
from agentrank_api.benchmark.lifecycle import BenchmarkRunStatus, MissionRunStatus
from agentrank_api.benchmark.models import BenchmarkRun
from agentrank_api.benchmark.observation import ObservedResult
from agentrank_api.benchmark.reference_executor import (
    REFERENCE_EXECUTOR,
    ReferenceMissionExecutor,
)
from agentrank_api.benchmark.runner import BenchmarkRunService
from agentrank_api.benchmark.suites import BenchmarkSuiteService
from agentrank_api.commerce.catalog_fixture import SeedProduct, SeedVariant
from agentrank_api.commerce.repository import CatalogRepository
from agentrank_api.constraints.rules import ConstraintOperator
from agentrank_api.errors import ConflictError
from agentrank_api.mandates.intent import AllowedCategory, MaxTotalAmount, RequiredAttribute
from agentrank_api.payments.fake import FakePaymentProvider
from agentrank_api.payments.models import PaymentAttempt, PaymentAttemptStatus

pytestmark = pytest.mark.anyio

CURRENCY = "INR"
SLUG = "orchestration-shop"
SUITE_KEY = "orchestration-suite"

CHARGERS = AllowedCategory("chargers")
BLACK = RequiredAttribute("color", "black", ConstraintOperator.EQ)

# Two units of one variant, and every mission below wants one of them. Two is the number that
# makes contamination visible: without isolation the third mission in a run finds an empty shelf,
# and without it between runs the second run finds one already gone.
STOCK = 2
PRICE = 100000


def variant(sku: str = "OS-BLK") -> SeedVariant:
    return SeedVariant(
        sku=sku,
        label="Black",
        price_amount_minor=PRICE,
        currency=CURRENCY,
        inventory_quantity=STOCK,
        attributes={"color": "black"},
    )


WORLD = BenchmarkFixture(
    key="orchestration-catalog",
    version=1,
    merchant_slug=SLUG,
    merchant_name="Orchestration Shop",
    products=(
        SeedProduct(
            external_id="OS-CHG",
            title="Charger",
            description=None,
            category="chargers",
            variants=(variant(),),
        ),
    ),
)


def buys(key: str) -> BenchmarkMissionDefinition:
    """A mission one unit of the fixture's only variant satisfies."""
    return BenchmarkMissionDefinition(
        brief=AgentMissionBrief(
            key=key,
            objective="Buy one black charger.",
            budget=MaxTotalAmount(amount_minor=PRICE, currency=CURRENCY),
            hard_constraints=(CHARGERS, BLACK),
        ),
        oracle=MissionOracle(
            expected_outcome=ExpectedOutcome.PURCHASE_AVAILABLE,
            simulated_value_amount_minor=PRICE,
        ),
    )


def suite_of(*keys: str, version: int = 1) -> BenchmarkSuiteDefinition:
    return BenchmarkSuiteDefinition(
        key=SUITE_KEY,
        version=version,
        merchant_slug=SLUG,
        name="Orchestration suite",
        missions=tuple(buys(key) for key in keys),
    )


async def registered(session: AsyncSession, fixture: BenchmarkFixture = WORLD) -> uuid.UUID:
    environments = BenchmarkEnvironmentService(session)
    environment = await environments.register(fixture)
    return environment.merchant_id


def executor(
    sessions: async_sessionmaker[AsyncSession], merchant_id: uuid.UUID
) -> ReferenceMissionExecutor:
    """The reference executor over a buyer surface that owns its own sessions.

    Not the runner's session. A mission's commerce runs on its own connections and commits
    there, so what the runner reads afterwards is what the database holds rather than what one
    shared transaction happens to be carrying.
    """
    surface = MerchantBuyerSurface(
        sessions, merchant_id=merchant_id, provider=FakePaymentProvider()
    )
    return ReferenceMissionExecutor(surface)


async def run(
    sessions: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    merchant_id: uuid.UUID,
    *,
    version: int = 1,
    fixture: BenchmarkFixture = WORLD,
) -> BenchmarkRun:
    return await BenchmarkRunService(session).run_suite(
        executor(sessions, merchant_id),
        suite_key=SUITE_KEY,
        suite_version=version,
        fixture=fixture,
    )


def outcomes(finished: BenchmarkRun) -> dict[str, MissionRunStatus]:
    """Every mission's semantic outcome, keyed by mission. Never a timestamp or an identifier."""
    return {result.mission.mission_key: result.status for result in finished.mission_runs}


async def stock_of(session: AsyncSession, merchant_id: uuid.UUID, sku: str = "OS-BLK") -> int:
    found = await CatalogRepository(session).get_variant_by_sku(merchant_id, sku)
    assert found is not None
    return found.inventory_quantity


# Mission isolation.


async def test_a_mission_is_not_short_of_stock_an_earlier_mission_bought(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """The property the whole phase turns on, and the shelf is deliberately too small for it.

    Three missions each want one unit and the merchant stocks two. Without a world put back
    before each mission, the third finds nothing and is marked down for a discovery failure it
    never had a chance at.
    """
    merchant_id = await registered(session)
    await BenchmarkSuiteService(session).publish(suite_of("first", "second", "third"))

    finished = await run(factory, session, merchant_id)

    assert finished.status is BenchmarkRunStatus.COMPLETED
    assert outcomes(finished) == {
        "first": MissionRunStatus.SUCCEEDED,
        "second": MissionRunStatus.SUCCEEDED,
        "third": MissionRunStatus.SUCCEEDED,
    }


async def test_the_order_missions_run_in_does_not_change_what_they_mean(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Two suites, the same independent missions, two orders, and one set of outcomes.

    This is what fails first if world preparation is removed: the last mission in whichever
    order was used runs out of stock, so the two orders disagree about which mission failed.
    """
    merchant_id = await registered(session)
    suites = BenchmarkSuiteService(session)
    await suites.publish(suite_of("alpha", "beta", "gamma"))
    await suites.publish(suite_of("gamma", "beta", "alpha", version=2))

    forwards = await run(factory, session, merchant_id)
    backwards = await run(factory, session, merchant_id, version=2)

    assert outcomes(forwards) == outcomes(backwards)
    assert set(outcomes(forwards)) == {"alpha", "beta", "gamma"}
    assert all(status is MissionRunStatus.SUCCEEDED for status in outcomes(forwards).values())


async def test_the_world_is_prepared_before_the_run_and_not_only_before_each_mission(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """The catalog pin has to describe the intended initial state rather than the leftovers.

    Named rather than protected incidentally. Without the preparation at the top of the run, the
    pin would be taken against whatever a previous run left behind, and two runs of one suite
    against one world would carry different pins while measuring the same thing.
    """
    merchant_id = await registered(session)
    await BenchmarkSuiteService(session).publish(suite_of("one"))
    intended = catalog_content_hash(
        await BenchmarkRunService(session).catalog(
            (await BenchmarkEnvironmentService(session).prepare(WORLD)).environment.merchant_id
        )
    )
    # Leave the shelf visibly not as the fixture describes it, without going through a mission.
    found = await CatalogRepository(session).get_variant_by_sku(merchant_id, "OS-BLK")
    assert found is not None
    found.inventory_quantity = STOCK - 1
    await session.commit()

    finished = await run(factory, session, merchant_id)

    assert finished.catalog_hash == intended


# Run isolation.


async def test_a_second_run_does_not_inherit_the_first_ones_stock(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Two runs of one suite against one world, and the second must not measure a poorer shop."""
    merchant_id = await registered(session)
    await BenchmarkSuiteService(session).publish(suite_of("first", "second"))

    first = await run(factory, session, merchant_id)
    second = await run(factory, session, merchant_id)

    assert outcomes(first) == outcomes(second)
    assert first.catalog_hash == second.catalog_hash


async def test_a_repeated_run_produces_the_same_semantic_result(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Reproducibility, compared on meaning rather than on timestamps or identifiers.

    Same suite, same intended world, same executor version, so the same result. Nothing here
    compares `created_at` or a row identifier, which necessarily differ and say nothing.
    """
    merchant_id = await registered(session)
    await BenchmarkSuiteService(session).publish(suite_of("one", "two"))
    service = BenchmarkRunService(session)

    first = await run(factory, session, merchant_id)
    first_metrics = await service.metrics(first.id, merchant_id=merchant_id)
    second = await run(factory, session, merchant_id)
    second_metrics = await service.metrics(second.id, merchant_id=merchant_id)

    assert outcomes(first) == outcomes(second)
    assert first_metrics == second_metrics


async def test_the_world_is_the_fixtures_again_when_a_run_finishes(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """The shelf a run leaves behind is one purchase short, and the next run puts it back."""
    merchant_id = await registered(session)
    await BenchmarkSuiteService(session).publish(suite_of("one"))

    await run(factory, session, merchant_id)
    assert await stock_of(session, merchant_id) == STOCK - 1

    await run(factory, session, merchant_id)
    assert await stock_of(session, merchant_id) == STOCK - 1


# Inventory, exactly once.


async def test_one_purchase_decrements_inventory_exactly_once(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Inside the mission's own world, and a second payment request must not do it again."""
    merchant_id = await registered(session)
    await BenchmarkSuiteService(session).publish(suite_of("one"))

    await run(factory, session, merchant_id)

    assert await stock_of(session, merchant_id) == STOCK - 1
    paid = list((await session.execute(select(PaymentAttempt))).scalars())
    assert [attempt.status for attempt in paid] == [PaymentAttemptStatus.SUCCEEDED]


async def test_paying_the_same_quote_again_writes_no_second_attempt(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """The idempotency identity is derived from the quote, so a repeat is the same operation."""
    from agentrank_api.benchmark.reference_executor import idempotency_key
    from agentrank_api.payments.schemas import CreatePaymentRequest

    merchant_id = await registered(session)
    await BenchmarkSuiteService(session).publish(suite_of("one"))
    provider = FakePaymentProvider()
    surface = MerchantBuyerSurface(factory, merchant_id=merchant_id, provider=provider)

    finished = await BenchmarkRunService(session).run_suite(
        ReferenceMissionExecutor(surface),
        suite_key=SUITE_KEY,
        suite_version=1,
        fixture=WORLD,
    )
    checkout_id = finished.mission_runs[0].checkout_id
    assert checkout_id is not None

    repeat = await surface.complete_checkout(
        checkout_id, CreatePaymentRequest(idempotency_key=idempotency_key(checkout_id))
    )

    assert not repeat.created
    assert repeat.attempt is not None
    assert repeat.attempt.status is PaymentAttemptStatus.SUCCEEDED
    assert len(list((await session.execute(select(PaymentAttempt))).scalars())) == 1
    assert await stock_of(session, merchant_id) == STOCK - 1
    assert provider.charges == 1


# Identity on the run.


async def test_a_run_records_which_executor_produced_it(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """A benchmark whose history cannot say what did the shopping compares two things."""
    merchant_id = await registered(session)
    await BenchmarkSuiteService(session).publish(suite_of("one"))

    finished = await run(factory, session, merchant_id)

    assert finished.executor_kind == REFERENCE_EXECUTOR.kind
    assert finished.executor_version == REFERENCE_EXECUTOR.version
    assert finished.executor_label == "reference-v1"


async def test_a_run_records_every_pin_it_has(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Four dimensions, and none of them stands in for another."""
    merchant_id = await registered(session)
    await BenchmarkSuiteService(session).publish(suite_of("one"))

    environments = BenchmarkEnvironmentService(session)
    environment = await environments.prepare(WORLD)
    published = await BenchmarkSuiteService(session).get(SUITE_KEY, 1)
    # The digest of the world as the fixture describes it, taken before anything buys from it.
    # The run pins where it began, not where it ended, which is why this is read here.
    intended = catalog_content_hash(await BenchmarkRunService(session).catalog(merchant_id))

    finished = await run(factory, session, merchant_id)

    assert finished.suite_id == published.id
    assert finished.environment_id == environment.environment.id
    assert finished.catalog_hash == intended
    assert finished.evaluator_version == evaluator_version()
    assert finished.executor_label == REFERENCE_EXECUTOR.label


async def test_a_run_with_no_executor_identity_says_so_rather_than_guessing(
    session: AsyncSession,
) -> None:
    await registered(session)
    await BenchmarkSuiteService(session).publish(suite_of("one"))

    started = await BenchmarkRunService(session).start_run(
        suite_key=SUITE_KEY, suite_version=1, merchant_slug=SLUG
    )

    assert started.executor_kind is None
    assert started.executor_label is None


def test_an_executor_identity_is_a_slug_and_a_positive_version() -> None:
    with pytest.raises(ValueError, match="executor kind"):
        ExecutorIdentity(kind="Reference V1", version=1)
    with pytest.raises(ValueError, match="executor version"):
        ExecutorIdentity(kind="reference", version=0)


# Lifecycle and what a crash leaves behind.


async def test_a_mission_is_running_before_the_executor_is_handed_anything(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """The transition a crash is read from. PENDING never started; RUNNING may have paid.

    Asserted by looking at the row from inside the executor, which is the only moment the state
    exists, and by committing so that what is read is what a separate process would see.
    """
    merchant_id = await registered(session)
    await BenchmarkSuiteService(session).publish(suite_of("one"))
    seen: list[MissionRunStatus] = []
    service = BenchmarkRunService(session)

    class Watching(ReferenceMissionExecutor):
        async def __call__(
            self, brief: AgentMissionBrief, *, merchant_id: uuid.UUID
        ) -> ObservedResult:
            # A second session on its own connection, which is what "a separate process would
            # see" actually means. Reading through the runner's own session would be satisfied
            # by a flush, and a flush is exactly what a crash discards.
            async with factory() as other:
                run_row = (await other.execute(select(BenchmarkRun))).scalars().one()
                loaded = await BenchmarkRunService(other).load(run_row.id, merchant_id=merchant_id)
                seen.extend(result.status for result in loaded.mission_runs)
            return await super().__call__(brief, merchant_id=merchant_id)

    surface = MerchantBuyerSurface(factory, merchant_id=merchant_id, provider=FakePaymentProvider())
    await service.run_suite(Watching(surface), suite_key=SUITE_KEY, suite_version=1, fixture=WORLD)

    assert seen == [MissionRunStatus.RUNNING]


async def test_an_executor_that_raises_stops_the_run_and_leaves_the_mission_running(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """A crash after side effects is the state that must never be replayed.

    The runner does not swallow it, because carrying on would produce a COMPLETED run with a
    mission nobody executed, and it does not roll the mission back to PENDING, because PENDING
    means nothing happened and something may well have.
    """
    merchant_id = await registered(session)
    await BenchmarkSuiteService(session).publish(suite_of("one", "two"))
    service = BenchmarkRunService(session)

    class Failing(ReferenceMissionExecutor):
        async def __call__(
            self, brief: AgentMissionBrief, *, merchant_id: uuid.UUID
        ) -> ObservedResult:
            if brief.key == "two":
                raise RuntimeError("the harness fell over")
            return await super().__call__(brief, merchant_id=merchant_id)

    surface = MerchantBuyerSurface(factory, merchant_id=merchant_id, provider=FakePaymentProvider())
    with pytest.raises(RuntimeError, match="fell over"):
        await service.run_suite(
            Failing(surface), suite_key=SUITE_KEY, suite_version=1, fixture=WORLD
        )

    stopped = (await session.execute(select(BenchmarkRun))).scalars().one()
    loaded = await service.load(stopped.id, merchant_id=merchant_id)
    assert loaded.status is BenchmarkRunStatus.RUNNING
    assert outcomes(loaded) == {
        "one": MissionRunStatus.SUCCEEDED,
        "two": MissionRunStatus.RUNNING,
    }


async def test_a_run_with_a_mission_still_running_cannot_be_completed(
    session: AsyncSession,
) -> None:
    """A mission whose outcome nobody knows is not a finished mission."""
    merchant_id = await registered(session)
    await BenchmarkSuiteService(session).publish(suite_of("one"))
    service = BenchmarkRunService(session)
    started = await service.start_run(suite_key=SUITE_KEY, suite_version=1, merchant_slug=SLUG)
    await service.start_mission(started.id, "one", merchant_id=merchant_id)

    with pytest.raises(ConflictError) as raised:
        await service.complete_run(started.id, merchant_id=merchant_id)

    assert raised.value.reason == "run_incomplete"


async def test_a_stopped_run_is_closed_honestly_rather_than_completed(
    session: AsyncSession,
) -> None:
    """ABORTED is its own status because a partial run must not present as a whole workload."""
    merchant_id = await registered(session)
    await BenchmarkSuiteService(session).publish(suite_of("one"))
    service = BenchmarkRunService(session)
    started = await service.start_run(suite_key=SUITE_KEY, suite_version=1, merchant_slug=SLUG)
    await service.start_mission(started.id, "one", merchant_id=merchant_id)

    aborted = await service.abort_run(started.id, merchant_id=merchant_id)

    assert aborted.status is BenchmarkRunStatus.ABORTED
    assert aborted.completed_at is not None


async def test_a_mission_that_already_produced_a_result_is_never_started_again(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """The whole of the no blind replay rule, at the one place a replay would begin."""
    merchant_id = await registered(session)
    await BenchmarkSuiteService(session).publish(suite_of("one"))
    service = BenchmarkRunService(session)
    finished = await run(factory, session, merchant_id)

    with pytest.raises(ConflictError) as raised:
        await service.start_mission(finished.id, "one", merchant_id=merchant_id)

    assert raised.value.reason in {"run_not_running", "mission_already_recorded"}


async def test_starting_a_running_mission_again_changes_nothing(
    session: AsyncSession,
) -> None:
    """A retry after a crash has to be able to describe the state it found, not refuse it."""
    merchant_id = await registered(session)
    await BenchmarkSuiteService(session).publish(suite_of("one"))
    service = BenchmarkRunService(session)
    started = await service.start_run(suite_key=SUITE_KEY, suite_version=1, merchant_slug=SLUG)

    first = await service.start_mission(started.id, "one", merchant_id=merchant_id)
    second = await service.start_mission(started.id, "one", merchant_id=merchant_id)

    assert first == second
    loaded = await service.load(started.id, merchant_id=merchant_id)
    assert outcomes(loaded) == {"one": MissionRunStatus.RUNNING}


# The world a mission is marked against.


async def test_a_mission_is_marked_against_the_catalog_it_was_given(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Reading the catalog after the mission would compare a mission against its own purchase.

    The last unit is bought, so a catalog read afterwards says nothing qualifies and the oracle
    would be reported as stale when it was exactly right.
    """
    scarce = replace(WORLD, products=(replace(WORLD.products[0], variants=(_one_unit(),)),))
    merchant_id = await registered(session, scarce)
    await BenchmarkSuiteService(session).publish(suite_of("one"))

    finished = await run(factory, session, merchant_id, fixture=scarce)

    assert outcomes(finished) == {"one": MissionRunStatus.SUCCEEDED}
    assert [result.oracle_confirmed for result in finished.mission_runs] == [True]


def _one_unit() -> SeedVariant:
    return replace(variant(), inventory_quantity=1)
