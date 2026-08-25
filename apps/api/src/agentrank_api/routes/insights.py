"""Merchant scoped read APIs over the diagnostics layer.

Six reads, and nothing that writes: a merchant can see their overview, their runs, one run
in full, one mission's diagnosis, that mission's trace, and any controlled experiment they
created. Every route resolves through the authenticated merchant exactly the way the
commerce routes do, so an identifier belonging to somebody else is indistinguishable from
one that never existed.

These endpoints exist so the product frontend has a stable contract to build against. They
expose findings, ownership, evidence references, simulated demand and methodology warnings;
they do not expose compiler review internals beyond what a merchant already owns, provider
payloads beyond their redacted trace form, or anything weighted that could stand in for a
score.

They take an operator merchant rather than any merchant, which is to say they refuse a
credential the benchmark runner minted for a run. That credential is a merchant credential and
authenticates, and what it must not do is read the evaluator's findings about the run the buyer
process holding it is executing inside. The loopback endpoint an isolated buyer is actually given
mounts none of this, which is the layer that holds regardless; this is the second one, and it
holds for any deployment where a benchmark credential can reach the ordinary API. A console
session never carries a benchmark capability, so nothing a merchant does is affected.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from agentrank_api.dependencies import OperatorDep, SessionDep
from agentrank_api.diagnostics.schemas import (
    ExperimentComparisonView,
    MerchantOverviewView,
    MissionDiagnosisView,
    RunDiagnosticsView,
    RunSummaryView,
    TraceProjectionView,
    experiment_view,
    overview_view,
    run_summary_view,
)
from agentrank_api.diagnostics.service import MAX_TRACE_LIMIT, DiagnosticsService
from agentrank_api.errors import NotFoundError

router = APIRouter(prefix="/api/v1/insights", tags=["insights"])


@router.get("/overview")
async def read_overview(
    session: SessionDep,
    merchant: OperatorDep,
) -> MerchantOverviewView:
    """Benchmark health, recent runs, top findings, simulated demand and system state."""
    return overview_view(await DiagnosticsService(session).merchant_overview(merchant.merchant_id))


@router.get("/runs")
async def list_runs(
    session: SessionDep,
    merchant: OperatorDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> list[RunSummaryView]:
    """The merchant's runs, newest first, bounded by the requested page size."""
    summaries = await DiagnosticsService(session).recent_run_summaries(
        merchant.merchant_id, limit=limit
    )
    return [run_summary_view(summary) for summary in summaries]


@router.get("/runs/{run_id}")
async def read_run(
    run_id: uuid.UUID,
    session: SessionDep,
    merchant: OperatorDep,
) -> RunDiagnosticsView:
    """One run's complete deterministic reading, including every mission and finding."""
    diagnostics = await DiagnosticsService(session).run_diagnostics(
        run_id, merchant_id=merchant.merchant_id
    )
    return RunDiagnosticsView.from_domain(diagnostics)


@router.get("/runs/{run_id}/missions/{mission_run_id}")
async def read_mission(
    run_id: uuid.UUID,
    mission_run_id: uuid.UUID,
    session: SessionDep,
    merchant: OperatorDep,
) -> MissionDiagnosisView:
    """One mission's diagnosis within one of the merchant's runs."""
    service = DiagnosticsService(session)
    diagnostics = await service.run_diagnostics(run_id, merchant_id=merchant.merchant_id)
    mission = next(
        (entry for entry in diagnostics.missions if entry.mission_run_id == mission_run_id),
        None,
    )
    if mission is None:
        raise NotFoundError("benchmark_mission_run", str(mission_run_id))
    return MissionDiagnosisView.from_domain(mission)


@router.get("/runs/{run_id}/missions/{mission_run_id}/trace")
async def read_trace(
    run_id: uuid.UUID,
    mission_run_id: uuid.UUID,
    session: SessionDep,
    merchant: OperatorDep,
    limit: Annotated[int, Query(ge=1, le=MAX_TRACE_LIMIT)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TraceProjectionView:
    """A bounded ordered page of one mission's trace, redacted as it was captured."""
    projection = await DiagnosticsService(session).mission_trace(
        run_id,
        mission_run_id,
        merchant_id=merchant.merchant_id,
        limit=limit,
        offset=offset,
    )
    return TraceProjectionView.from_domain(projection)


@router.get("/experiments/{experiment_id}")
async def read_experiment(
    experiment_id: uuid.UUID,
    session: SessionDep,
    merchant: OperatorDep,
) -> ExperimentComparisonView:
    """One controlled raw versus compiled experiment, with its methodology warnings."""
    result = await DiagnosticsService(session).experiment_diagnosis(
        experiment_id, merchant_id=merchant.merchant_id
    )
    return experiment_view(result)
