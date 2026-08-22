"""Persistence access for benchmark definitions and benchmark runs.

Definitions are create and read only. There is deliberately no update and no delete for a
published suite: it is the historical record of a workload, and the database refuses both
through a trigger, so this is a contract rather than a convention.

Runs are written once and then transitioned, and the transitions are held by a trigger too, so
a recorded mission result cannot be re-classified after the fact.

Both repositories own SQLAlchemy and neither commits. The caller sets the transaction boundary,
which is what lets a suite and its missions, or a run and its mission runs, be one unit of work.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from agentrank_api.benchmark.definitions import BenchmarkSuiteDefinition
from agentrank_api.benchmark.identity import CorruptedSuiteDefinitionError, suite_content_hash
from agentrank_api.benchmark.lifecycle import BenchmarkRunStatus, MissionRunStatus
from agentrank_api.benchmark.models import (
    BenchmarkMission,
    BenchmarkMissionRun,
    BenchmarkRun,
    BenchmarkSuite,
)
from agentrank_api.commerce.models import Merchant

# A listing is a work list, not an export. The bound is here rather than at a caller so that
# there is no way to ask for an unbounded read.
DEFAULT_RUN_LIMIT = 20
MAX_RUN_LIMIT = 100


class BenchmarkSuiteRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, definition: BenchmarkSuiteDefinition) -> BenchmarkSuite:
        """Write a suite and its missions, and flush.

        The content hash is computed here rather than accepted from a caller. A hash somebody
        else supplied is a hash that can disagree with what was stored, and the one property
        this column exists for is that it cannot.

        `ordinal` comes from the position of a mission in the definition, so the stored order
        is the authored order and a rerun presents the same workload in the same sequence.
        """
        suite = BenchmarkSuite(
            suite_key=definition.key,
            version=definition.version,
            merchant_slug=definition.merchant_slug,
            name=definition.name,
            definition_hash=suite_content_hash(definition),
        )
        suite.missions = [
            BenchmarkMission(
                mission_key=mission.key,
                ordinal=ordinal,
                objective=mission.brief.objective,
                quantity=mission.brief.quantity,
                budget_amount_minor=mission.brief.budget.amount_minor,
                currency=mission.brief.currency,
                hard_constraints=[
                    constraint.to_payload() for constraint in mission.brief.hard_constraints
                ],
                preferences=[preference.statement for preference in mission.brief.preferences],
                expected_outcome=mission.oracle.expected_outcome,
                simulated_value_amount_minor=mission.oracle.simulated_value_amount_minor,
            )
            for ordinal, mission in enumerate(definition.missions)
        ]
        self._session.add(suite)
        await self._session.flush()
        return suite

    async def get(self, key: str, version: int) -> BenchmarkSuite | None:
        """One published suite by its key and version, with every mission loaded.

        Loaded eagerly and completely. A run executes every mission a suite defines, and a
        collection that was loaded lazily or partially would make a run's shape depend on how
        the object happened to be fetched. `lazy="raise_on_sql"` means an unloaded collection
        raises rather than quietly producing a suite with no missions.

        There is no merchant argument. Suites are global templates, and the merchant a suite
        was authored against is a field on it rather than an owner of it.
        """
        statement = (
            select(BenchmarkSuite)
            .options(selectinload(BenchmarkSuite.missions))
            .where(BenchmarkSuite.suite_key == key, BenchmarkSuite.version == version)
        )
        return _verified((await self._session.execute(statement)).scalar_one_or_none())

    async def get_by_id(self, suite_id: uuid.UUID) -> BenchmarkSuite | None:
        """One published suite by identifier, with every mission loaded.

        The read a run performs, since a run stores the suite it was executed against by
        identifier rather than by key and version.
        """
        statement = (
            select(BenchmarkSuite)
            .options(selectinload(BenchmarkSuite.missions))
            .where(BenchmarkSuite.id == suite_id)
        )
        return _verified((await self._session.execute(statement)).scalar_one_or_none())

    async def list_versions(self, key: str) -> list[BenchmarkSuite]:
        """Every published version of one suite key, oldest first.

        Missions are not loaded. This answers "which versions exist", which is a question
        about identity rather than about content, and loading every mission of every version
        to answer it would be a read nobody asked for.
        """
        statement = (
            select(BenchmarkSuite)
            .where(BenchmarkSuite.suite_key == key)
            .order_by(BenchmarkSuite.version)
        )
        return list((await self._session.execute(statement)).scalars())


class BenchmarkRunRepository:
    """Persistence access for benchmark runs and their per mission results.

    Every read takes a merchant and puts it in the query rather than comparing it afterwards.
    A run is merchant owned, and there is deliberately no unscoped read here: an unscoped read
    is what a caller reaches for when isolation is inconvenient, and not having one is what
    makes it not reachable. The composite foreign keys already make cross merchant rows
    unwritable; this is the reading half of the same rule.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        merchant: Merchant,
        suite: BenchmarkSuite,
        representation_label: str | None = None,
        catalog_hash: str | None = None,
        evaluator_version: str | None = None,
    ) -> BenchmarkRun:
        """Write a PENDING run and one PENDING mission run per mission, and flush.

        Every mission run exists before anything executes, so the shape of a run is fixed by
        the suite rather than by how far execution got. Without that, a run that stopped early
        would look like a run over a shorter suite, and the completion rate would be computed
        against a denominator nobody chose.

        The merchant is passed as a row rather than an identifier so that its slug can be
        compared with the one the suite was authored against. A mission oracle is a statement
        about one catalog, and running the suite anywhere else would produce a perfectly well
        formed result marked against ground truth nobody ever established there.

        The two pins are written with the row rather than updated onto it afterwards. They are
        in the run guard's immutable list, so an update would be refused, which is the behavior
        a pin should have: what a run was measured against is decided when it is created.

        Requires `suite.missions` to be loaded. `lazy="raise_on_sql"` makes an unloaded
        collection raise here rather than quietly producing a run with no missions to execute.
        """
        if merchant.slug != suite.merchant_slug:
            raise ValueError(
                f"benchmark suite {suite.label} was authored against merchant"
                f" {suite.merchant_slug!r} and cannot be run against {merchant.slug!r}"
            )

        run = BenchmarkRun(
            merchant_id=merchant.id,
            suite_id=suite.id,
            representation_label=representation_label,
            catalog_hash=catalog_hash,
            evaluator_version=evaluator_version,
            status=BenchmarkRunStatus.PENDING,
        )
        run.mission_runs = [
            BenchmarkMissionRun(
                merchant_id=merchant.id,
                suite_id=suite.id,
                mission_id=mission.id,
                status=MissionRunStatus.PENDING,
            )
            for mission in suite.missions
        ]
        self._session.add(run)
        await self._session.flush()
        return run

    async def get(self, run_id: uuid.UUID, *, merchant_id: uuid.UUID) -> BenchmarkRun | None:
        """One merchant's run, with its mission runs and their mission definitions loaded.

        The definitions come with it because nothing about a result can be interpreted without
        them: whether a mission succeeded depends on what its ground truth said, and reading
        that lazily would make a report depend on how the objects were fetched.
        """
        statement = (
            select(BenchmarkRun)
            .options(
                selectinload(BenchmarkRun.mission_runs).selectinload(BenchmarkMissionRun.mission)
            )
            .where(BenchmarkRun.id == run_id, BenchmarkRun.merchant_id == merchant_id)
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get_for_update(
        self, run_id: uuid.UUID, *, merchant_id: uuid.UUID
    ) -> BenchmarkRun | None:
        """One merchant's run, held against other transactions.

        Without the mission runs. A caller that locks a run is about to change the run's own
        status, and loading a collection under a row lock would hold it for longer than the
        decision needs.
        """
        statement = (
            select(BenchmarkRun)
            .where(BenchmarkRun.id == run_id, BenchmarkRun.merchant_id == merchant_id)
            .with_for_update()
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get_mission_run(
        self, run_id: uuid.UUID, mission_id: uuid.UUID, *, merchant_id: uuid.UUID
    ) -> BenchmarkMissionRun | None:
        """One mission's result within one merchant's run, held against other transactions.

        Locked because recording a result is a transition, and two writers recording the same
        mission at once would otherwise both read PENDING. The trigger refuses the second write
        either way; the lock is what turns that into a queue rather than an error.
        """
        statement = (
            select(BenchmarkMissionRun)
            .where(
                BenchmarkMissionRun.run_id == run_id,
                BenchmarkMissionRun.mission_id == mission_id,
                BenchmarkMissionRun.merchant_id == merchant_id,
            )
            .with_for_update()
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def list_for_merchant(
        self, *, merchant_id: uuid.UUID, limit: int = DEFAULT_RUN_LIMIT
    ) -> list[BenchmarkRun]:
        """One merchant's runs, newest first, bounded.

        Version 7 identifiers are time ordered, so ordering by identifier is ordering by
        creation and is total. Ordering by `created_at` would not be: two runs created in the
        same transaction share a timestamp, and a tie broken arbitrarily is a listing that can
        change between two identical reads.
        """
        statement = (
            select(BenchmarkRun)
            .where(BenchmarkRun.merchant_id == merchant_id)
            .order_by(BenchmarkRun.id.desc())
            .limit(min(limit, MAX_RUN_LIMIT))
        )
        return list((await self._session.execute(statement)).scalars())


def _verified(suite: BenchmarkSuite | None) -> BenchmarkSuite | None:
    """Hand back a published suite only if its content still matches its own digest.

    The immutability triggers refuse UPDATE and DELETE, and appending a mission to an already
    published suite is neither, so the digest was blind to exactly the edit that would change
    what a workload measured without changing any row that already existed. Recomputing it here
    closes that and every other route around this application at once, and costs one hash over
    a bounded document per read.
    """
    if suite is None:
        return None
    actual = suite_content_hash(suite.to_definition())
    if actual != suite.definition_hash:
        raise CorruptedSuiteDefinitionError(suite.label, suite.definition_hash, actual)
    return suite
