"""Permission to mutate a merchant while a benchmark run owns its world.

A running benchmark is a durable claim, not a transaction held across a buyer's work.  That
means every mutation that could change its catalog, authorization state, effective inventory or
payment state needs an application-level decision at the operation boundary.

The one exception is deliberately narrow.  The runner has a run identity, and the loopback
worker authenticates with a credential that is bound to that same persisted run.  Both become a
``BenchmarkRunCapability``.  It is not a caller supplied flag: the guard reads the active run
from the database and accepts the capability only when its merchant and run identifiers match
that row.  No active row means ordinary merchant operations proceed unchanged.

This is an application isolation mechanism for the trusted executor and model-tool threat
model.  It is not a security boundary against arbitrary native code with database access.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.benchmark.execution import BenchmarkRunCapability
from agentrank_api.benchmark.lifecycle import BenchmarkRunStatus
from agentrank_api.benchmark.repository import BenchmarkRunRepository
from agentrank_api.errors import ConflictError

BENCHMARK_ENVIRONMENT_RESOURCE = "benchmark_environment"
BENCHMARK_WORLD_ACTIVE = "benchmark_world_active"
BENCHMARK_RUN_NOT_ACTIVE = "benchmark_run_not_active"


class BenchmarkMutationGuard:
    """Refuse external mutation of a merchant an active benchmark run owns."""

    def __init__(self, session: AsyncSession) -> None:
        self._runs = BenchmarkRunRepository(session)

    async def require_allowed(
        self, merchant_id: uuid.UUID, *, capability: BenchmarkRunCapability | None = None
    ) -> None:
        """Allow an ordinary caller, or only the exact run that holds this merchant's claim.

        A bound credential never becomes an ordinary credential after its run closes. Otherwise
        a worker that survived an operator abort could keep making commerce changes in the gap
        before another run starts.
        """
        active = await self._runs.active_run_id(merchant_id=merchant_id)
        if capability is None and active is None:
            return
        if capability is not None:
            if capability.merchant_id == merchant_id and capability.run_id == active:
                return
            if active is None:
                raise ConflictError(
                    BENCHMARK_RUN_NOT_ACTIVE,
                    f"benchmark run {capability.run_id} no longer owns this merchant's world",
                    resource=BENCHMARK_ENVIRONMENT_RESOURCE,
                    identifier=str(capability.run_id),
                )
        raise ConflictError(
            BENCHMARK_WORLD_ACTIVE,
            f"benchmark run {active} owns this merchant's world until it completes or is aborted",
            resource=BENCHMARK_ENVIRONMENT_RESOURCE,
            identifier=str(active),
        )

    async def require_active(self, capability: BenchmarkRunCapability) -> None:
        """Require and hold the run that a new benchmark credential will be bound to.

        Credential issuance writes in the same transaction, so locking the run prevents an abort
        from closing it between this check and the credential's durable binding.
        """
        run = await self._runs.get_for_update(capability.run_id, merchant_id=capability.merchant_id)
        if run is not None and run.status is BenchmarkRunStatus.RUNNING:
            return
        raise ConflictError(
            BENCHMARK_WORLD_ACTIVE,
            "a benchmark credential may be issued only for the run that currently owns its world",
            resource=BENCHMARK_ENVIRONMENT_RESOURCE,
            identifier=str(capability.run_id),
        )
