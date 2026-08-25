"""Persistence access for merchant evaluation workspaces.

Create and read only, like every other historical artifact in this schema and for the same
reason: a benchmark run points at the world and the workload a workspace names, and the database
refuses UPDATE and DELETE through a trigger.

Deliberately narrow in what it imports. The evaluation launch preflight and the operator
dispatcher both need to know which workspace is a merchant's current one, and neither should
have to import the bootstrap service to find out, so the reads live here and this module depends
on nothing but its own table.
"""

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.benchmark.models import BenchmarkMission
from agentrank_api.workspace.models import MerchantEvaluationWorkspace


class MerchantEvaluationWorkspaceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        merchant_id: uuid.UUID,
        source_snapshot_id: uuid.UUID,
        environment_id: uuid.UUID,
        suite_id: uuid.UUID,
        generator_version: str,
        configuration_digest: str,
        catalog_hash: str,
        suite_hash: str,
        catalog_fixture: dict[str, Any],
        composition: dict[str, Any],
    ) -> MerchantEvaluationWorkspace:
        """Write one workspace and flush. The caller owns the transaction."""
        workspace = MerchantEvaluationWorkspace(
            merchant_id=merchant_id,
            source_snapshot_id=source_snapshot_id,
            environment_id=environment_id,
            suite_id=suite_id,
            generator_version=generator_version,
            configuration_digest=configuration_digest,
            catalog_hash=catalog_hash,
            suite_hash=suite_hash,
            catalog_fixture=catalog_fixture,
            composition=composition,
        )
        self._session.add(workspace)
        await self._session.flush()
        return workspace

    async def current(self, merchant_id: uuid.UUID) -> MerchantEvaluationWorkspace | None:
        """The merchant's newest workspace, or None if nobody has bootstrapped one.

        Ordered by `write_order`, which PostgreSQL assigns at INSERT and no writer can supply.
        The reasoning is the one `MerchantSourceIntakeService.current` writes down and it
        applies unchanged: `created_at` is `transaction_timestamp()`, so a transaction that
        began first and committed second carries the earlier stamp, and a version 7 UUID is
        monotonic only within one process.

        Getting this wrong points a merchant's next evaluation at a superseded world.
        """
        return (
            await self._session.execute(
                select(MerchantEvaluationWorkspace)
                .where(MerchantEvaluationWorkspace.merchant_id == merchant_id)
                .order_by(MerchantEvaluationWorkspace.write_order.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def by_identity(
        self,
        merchant_id: uuid.UUID,
        *,
        source_snapshot_id: uuid.UUID,
        configuration_digest: str,
    ) -> MerchantEvaluationWorkspace | None:
        """The workspace one bootstrap command already produced, if it produced one.

        The idempotency read. A repeat of the same command, a retry after a lost response and a
        concurrent duplicate all resolve here rather than writing a second identical world.
        """
        return (
            await self._session.execute(
                select(MerchantEvaluationWorkspace).where(
                    MerchantEvaluationWorkspace.merchant_id == merchant_id,
                    MerchantEvaluationWorkspace.source_snapshot_id == source_snapshot_id,
                    MerchantEvaluationWorkspace.configuration_digest == configuration_digest,
                )
            )
        ).scalar_one_or_none()

    async def for_source(
        self, merchant_id: uuid.UUID, source_snapshot_id: uuid.UUID
    ) -> list[MerchantEvaluationWorkspace]:
        """Every workspace built from one source snapshot, oldest first."""
        return list(
            (
                await self._session.execute(
                    select(MerchantEvaluationWorkspace)
                    .where(
                        MerchantEvaluationWorkspace.merchant_id == merchant_id,
                        MerchantEvaluationWorkspace.source_snapshot_id == source_snapshot_id,
                    )
                    .order_by(MerchantEvaluationWorkspace.write_order)
                )
            ).scalars()
        )

    async def environment_ids(self, merchant_id: uuid.UUID) -> set[uuid.UUID]:
        """Every benchmark world this merchant's workspaces generated.

        What the bootstrap preflight compares a merchant's registered worlds against. A world
        outside this set was registered by something else, which for now means an operator
        authored it from files, and generating a second world beside it would leave two answers
        to which one a launch should use.
        """
        return set(
            (
                await self._session.execute(
                    select(MerchantEvaluationWorkspace.environment_id).where(
                        MerchantEvaluationWorkspace.merchant_id == merchant_id
                    )
                )
            ).scalars()
        )

    async def mission_count(self, suite_id: uuid.UUID) -> int:
        """How many missions one generated suite holds, counted rather than remembered.

        From the mission rows rather than from the stored composition, for the same reason
        benchmark metrics are derived rather than stored: a remembered count is a count that can
        disagree with the rows it describes.
        """
        return int(
            (
                await self._session.execute(
                    select(func.count())
                    .select_from(BenchmarkMission)
                    .where(BenchmarkMission.suite_id == suite_id)
                )
            ).scalar_one()
        )
