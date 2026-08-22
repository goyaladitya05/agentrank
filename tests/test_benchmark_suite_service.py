"""Publishing benchmark suites, and what publishing refuses to do to history."""

import asyncio

import pytest
from benchmark_support import CHARGERS, mission, suite
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentrank_api.benchmark.definitions import ExpectedOutcome
from agentrank_api.benchmark.repository import BenchmarkSuiteRepository
from agentrank_api.benchmark.suites import BenchmarkSuiteService
from agentrank_api.errors import ConflictError, NotFoundError

pytestmark = pytest.mark.anyio

# A concurrent test that goes wrong blocks on the index rather than failing, so the wait is
# bounded. Generous enough never to fire on a healthy database.
CONCURRENCY_TIMEOUT = 30

# How long the racer is watched before concluding it is genuinely queued. A publish that
# reached no constraint would have finished in milliseconds.
LOCK_WAIT = 1.5


async def test_publishing_writes_the_definition(session: AsyncSession) -> None:
    definition = suite(mission("one"), mission("two"))

    published = await BenchmarkSuiteService(session).publish(definition)

    assert published.label == "test-suite@1"
    assert published.to_definition() == definition


async def test_publishing_the_same_definition_twice_writes_nothing_new(
    session: AsyncSession,
) -> None:
    """Convergent, exactly as seeding the development catalog is."""
    service = BenchmarkSuiteService(session)
    definition = suite(mission("one"))

    first = await service.publish(definition)
    second = await service.publish(definition)

    assert first.id == second.id
    assert [stored.version for stored in await service.versions(definition.key)] == [1]


async def test_publishing_a_changed_definition_under_an_existing_version_is_refused(
    session: AsyncSession,
) -> None:
    """The whole reproducibility guarantee. A fixture edit is a refusal, not a rewrite."""
    service = BenchmarkSuiteService(session)
    await service.publish(suite(mission("one")))

    with pytest.raises(ConflictError) as raised:
        await service.publish(suite(mission("one", constraints=(CHARGERS,))))

    assert raised.value.reason == "suite_definition_changed"
    assert "Publish a new version" in raised.value.detail


async def test_an_oracle_edit_alone_is_enough_to_be_refused(session: AsyncSession) -> None:
    """The edit that would otherwise be invisible: same brief, different ground truth."""
    service = BenchmarkSuiteService(session)
    await service.publish(suite(mission("one")))

    with pytest.raises(ConflictError, match="suite"):
        await service.publish(suite(mission("one", outcome=ExpectedOutcome.NO_ACCEPTABLE_PURCHASE)))


async def test_a_changed_definition_is_published_under_a_new_version(
    session: AsyncSession,
) -> None:
    """The supported way to change a workload, and what makes the refusal above workable."""
    service = BenchmarkSuiteService(session)
    original = await service.publish(suite(mission("one"), version=1))

    updated = await service.publish(suite(mission("one", constraints=(CHARGERS,)), version=2))

    assert original.id != updated.id
    assert original.definition_hash != updated.definition_hash


async def test_a_historical_suite_is_untouched_by_a_later_version(
    session: AsyncSession,
) -> None:
    """A result produced under version 1 still describes version 1's missions."""
    service = BenchmarkSuiteService(session)
    original = await service.publish(suite(mission("one"), version=1))
    original_definition = original.to_definition()

    await service.publish(suite(mission("one", constraints=(CHARGERS,)), version=2))
    session.expunge_all()

    reread = await service.get("test-suite", 1)
    assert reread.to_definition() == original_definition


async def test_renaming_a_suite_without_changing_its_content_is_accepted(
    session: AsyncSession,
) -> None:
    """The display name is a label, so correcting one must not force a version bump.

    The published row keeps the name it was published with, because the row is immutable.
    That is the honest outcome: the historical record says what it said.
    """
    service = BenchmarkSuiteService(session)
    original = await service.publish(suite(name="Frist draft"))

    republished = await service.publish(suite(name="First draft"))

    assert republished.id == original.id
    assert republished.name == "Frist draft"


async def test_an_unpublished_suite_is_absent_rather_than_empty(session: AsyncSession) -> None:
    """An empty workload would report a perfect result over nothing at all."""
    with pytest.raises(NotFoundError, match="benchmark_suite"):
        await BenchmarkSuiteService(session).get("nothing-here", 1)


async def test_a_concurrent_publish_of_one_version_resolves_to_one_suite(
    factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """Two publishes of a brand new version can both read that none exists.

    `session` is requested and unused on purpose: these tests write through `factory`, which
    has no teardown of its own, and it is the `session` fixture that truncates afterwards.

    The interleaving is forced rather than hoped for. One writer inserts and holds its
    transaction open; the other then runs the real publish, reads nothing because the first
    has not committed, and blocks on the unique index. Releasing the first turns that block
    into an integrity error, which is exactly the path the conflict translation exists for.

    Without the gate this test would pass with no unique constraint at all, because the first
    publish would have committed before the second one read.
    """
    definition = suite(mission("one"))

    async with factory() as holder, factory() as racer:
        winner = await BenchmarkSuiteRepository(holder).create(definition)

        publishing = asyncio.create_task(BenchmarkSuiteService(racer).publish(definition))
        # Long enough to conclude the racer is genuinely queued on the index. A publish that
        # reached no constraint would have finished in milliseconds.
        await asyncio.sleep(LOCK_WAIT)
        assert not publishing.done()

        await holder.commit()
        published = await asyncio.wait_for(publishing, timeout=CONCURRENCY_TIMEOUT)

    # The loser read no existing suite, so it reached the constraint rather than the content
    # comparison, and then re-read and took the same decision the winner did. Convergence has
    # to mean the same thing under concurrency as it does in sequence.
    assert published.id == winner.id


async def test_a_concurrent_publish_of_two_different_definitions_refuses_one(
    factory: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """Losing the race is not a licence to redefine the version that won it.

    `session` is requested and unused for the same reason as the test above.
    """
    async with factory() as holder, factory() as racer:
        await BenchmarkSuiteRepository(holder).create(suite(mission("one")))

        publishing = asyncio.create_task(
            BenchmarkSuiteService(racer).publish(suite(mission("one", constraints=(CHARGERS,))))
        )
        await asyncio.sleep(LOCK_WAIT)
        assert not publishing.done()

        await holder.commit()
        with pytest.raises(ConflictError) as raised:
            await asyncio.wait_for(publishing, timeout=CONCURRENCY_TIMEOUT)

    assert raised.value.reason == "suite_definition_changed"
