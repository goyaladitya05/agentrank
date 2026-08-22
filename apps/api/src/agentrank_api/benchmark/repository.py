"""Persistence access for published benchmark definitions.

Create and read. There is deliberately no update and no delete: a published suite is the
historical record of a workload, and the database refuses both through a trigger, so this is
a contract rather than a convention.

The repository owns SQLAlchemy and does not commit. The caller sets the transaction boundary,
which is what lets a suite and every mission under it be one unit of work.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from agentrank_api.benchmark.definitions import BenchmarkSuiteDefinition
from agentrank_api.benchmark.identity import suite_content_hash
from agentrank_api.benchmark.models import BenchmarkMission, BenchmarkSuite


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
        return (await self._session.execute(statement)).scalar_one_or_none()

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
        return (await self._session.execute(statement)).scalar_one_or_none()

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
