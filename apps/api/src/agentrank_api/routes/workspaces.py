"""The merchant's evaluation setup: what it is, and the command that builds one.

One read and one command. The read answers what AgentRank would evaluate this merchant against
and what stops it; the command turns their current source snapshot into that setup.

Building one is a merchant command rather than an operator-only one, and the reason is what it
actually does. It is deterministic, it calls no model, it spends no provider quota, it writes no
product row, price or stock level, and everything it produces is derived from evidence the
merchant supplied themselves. There is nothing in it a merchant should have to ask somebody else
to run. The operator command line reaches the same service for a private beta operator who is
setting a merchant up alongside them, and it can do nothing the merchant cannot.

What a browser may say is the source snapshot it was shown. It may not say which merchant, which
world, which suite or how many missions: the merchant comes from the credential that
authenticated the request, and everything else is derived server side from their own frozen
evidence. The snapshot is checked against the merchant's current one rather than used to select
one, so a page rendered before a source refresh is refused rather than silently building a world
from evidence the merchant has already replaced.

`OperatorDep` rather than `MerchantDep`, exactly as the source and evaluation routes use, so a
credential the benchmark runner minted for a run is refused. A buyer that could build a workspace
could publish the benchmark suite it is about to be measured against.

Nothing here executes a benchmark or spends anything. A built setup makes the existing first
evaluation available; requesting one stays its own explicit command on its own endpoint.
"""

from typing import Annotated

from fastapi import APIRouter, Query, status

from agentrank_api.dependencies import OperatorDep, SessionDep
from agentrank_api.workspace.schemas import (
    EvaluationSetupView,
    EvaluationWorkspaceView,
    WorkspaceBuildRequest,
    WorkspaceBuildView,
)
from agentrank_api.workspace.service import MerchantEvaluationWorkspaceService

router = APIRouter(prefix="/api/v1/benchmark/workspace", tags=["benchmark"])


@router.get("")
async def read_setup(session: SessionDep, merchant: OperatorDep) -> EvaluationSetupView:
    """This merchant's evaluation setup, what building one would produce, and what stops it."""
    preflight = await MerchantEvaluationWorkspaceService(session).preflight(merchant.merchant_id)
    return EvaluationSetupView.from_domain(preflight)


@router.get("/history")
async def list_workspaces(
    session: SessionDep,
    merchant: OperatorDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> list[EvaluationWorkspaceView]:
    """Every evaluation setup this merchant has had, newest first.

    History rather than a list of things to choose from. An older workspace is what a run
    measured against and stays exactly as it was; nothing here reactivates one.
    """
    summaries = await MerchantEvaluationWorkspaceService(session).history(
        merchant.merchant_id, limit=limit
    )
    return [EvaluationWorkspaceView.from_domain(entry) for entry in summaries]


@router.post("", status_code=status.HTTP_201_CREATED)
async def build_setup(
    request: WorkspaceBuildRequest, session: SessionDep, merchant: OperatorDep
) -> WorkspaceBuildView:
    """Build this merchant's evaluation setup, or answer with the one already built.

    201 whether or not anything was written, and that is deliberate rather than sloppy. A retry
    after a lost response has to be able to learn what happened, and answering 409 to a caller
    repeating its own command would say something went wrong when nothing did. What the command
    actually did is `created`.
    """
    service = MerchantEvaluationWorkspaceService(session)
    outcome = await service.bootstrap(
        merchant.merchant_id, source_snapshot_id=request.source_snapshot_id
    )
    summary = await service.summary_of(merchant.merchant_id, outcome.workspace.id)
    return WorkspaceBuildView(
        created=outcome.created, workspace=EvaluationWorkspaceView.from_domain(summary)
    )
