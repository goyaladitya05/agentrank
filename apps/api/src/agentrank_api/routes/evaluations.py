"""The merchant's explicit command to run one benchmark evaluation of their own merchant.

Three reads and one write, and the write is a command rather than a resource creation form. The
browser says which kind of evaluation it was shown, the representation if that kind has one, and
what request this is; everything else, including which merchant and which kind of evaluation is
actually available, is resolved server side from the authenticated credential.

Which command a merchant is making is a server decision. A merchant publishing an agent-ready
representation is asking about that artifact; a merchant with nothing published and no completed
run has no evidence at all, and what an evaluation can honestly measure for them is their current
merchant-facing state. The browser names the purpose it read so a stale page is refused, and
never so that it can choose.

Publishing never reaches here. `POST /api/v1/compiler/runs/{id}/publish` writes an artifact and
launches nothing, which is a product decision as well as an engineering one: a benchmark run
spends model quota and takes as long as a suite takes, and starting one as a side effect of a
publish would spend on the merchant's behalf without being asked.

Neither does a benchmark buyer. `OperatorDep` refuses a credential the benchmark runner minted
for a run, and the loopback server such a credential is given does not mount this router at all,
so a buyer has no route to the lifecycle of the run it is executing inside.

Nothing here executes a benchmark. Admission writes a durable queued launch and answers, and a
separate worker process claims it. That is what keeps this endpoint an ordinary short request
rather than a browser connection held open across an entire suite.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from agentrank_api.benchmark.launch import MerchantEvaluationLaunchService
from agentrank_api.benchmark.launch_schemas import (
    EvaluationLaunchDetailView,
    EvaluationLaunchRequest,
    EvaluationLaunchView,
    EvaluationPreflightView,
)
from agentrank_api.dependencies import OperatorDep, SessionDep, SettingsDep
from agentrank_api.diagnostics.service import DiagnosticsService

router = APIRouter(prefix="/api/v1/benchmark/evaluations", tags=["benchmark"])


@router.get("/preflight")
async def preflight(
    session: SessionDep,
    merchant: OperatorDep,
    settings: SettingsDep,
) -> EvaluationPreflightView:
    """What an evaluation would measure now, which kind it would be, and what stops it."""
    plan = await MerchantEvaluationLaunchService(session, settings).plan(merchant.merchant_id)
    return EvaluationPreflightView.from_domain(plan)


@router.get("")
async def list_launches(
    session: SessionDep,
    merchant: OperatorDep,
    settings: SettingsDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> list[EvaluationLaunchView]:
    """This merchant's launches, newest first."""
    details = await MerchantEvaluationLaunchService(session, settings).details(
        merchant.merchant_id, limit=limit
    )
    return [EvaluationLaunchView.from_domain(detail) for detail in details]


@router.get("/{launch_id}")
async def read_launch(
    launch_id: uuid.UUID,
    session: SessionDep,
    merchant: OperatorDep,
    settings: SettingsDep,
) -> EvaluationLaunchDetailView:
    """One launch and its comparison. A foreign identifier is indistinguishable from an unknown
    one, and the comparison is null rather than empty while there is not yet one to give."""
    detail = await MerchantEvaluationLaunchService(session, settings).detail(
        merchant.merchant_id, launch_id
    )
    comparison = await DiagnosticsService(session).launch_comparison(
        launch_id, merchant_id=merchant.merchant_id
    )
    return EvaluationLaunchDetailView.with_comparison(detail, comparison)


@router.post("", status_code=status.HTTP_201_CREATED)
async def request_launch(
    request: EvaluationLaunchRequest,
    session: SessionDep,
    merchant: OperatorDep,
    settings: SettingsDep,
) -> EvaluationLaunchView:
    """Admit one launch, or answer with the one this request key already produced.

    201 for both, and that is deliberate rather than sloppy: a retry after a lost response has
    to be able to learn what happened, and answering 409 to a caller repeating its own request
    would tell it something went wrong when nothing did. A repeated key naming a different
    representation is a different command and is refused with a code.
    """
    service = MerchantEvaluationLaunchService(session, settings)
    launch = await service.request(
        merchant.merchant_id,
        purpose=request.purpose,
        representation_id=request.representation_id,
        request_key=request.request_key,
        plan_digest=request.plan_digest,
    )
    return EvaluationLaunchView.from_domain(await service.detail(merchant.merchant_id, launch.id))


@router.post("/{launch_id}/withdraw")
async def withdraw_launch(
    launch_id: uuid.UUID,
    session: SessionDep,
    merchant: OperatorDep,
    settings: SettingsDep,
) -> EvaluationLaunchView:
    """Close a queued evaluation the merchant no longer wants.

    The exit from the one state the console could not leave. A queued launch waits for a worker
    an operator configures, and while it waits the merchant can neither request another
    evaluation nor build a new evaluation setup, so a deployment with no capable worker left them
    holding a request nothing would run and no way to put it down.

    A command rather than a delete, and the launch is settled rather than removed: what a
    merchant asked for and then withdrew is a thing that happened. Queued only, because an
    executing launch has a run behind it that somebody has to account for, and that is refused
    with a code rather than performed.
    """
    service = MerchantEvaluationLaunchService(session, settings)
    await service.withdraw(merchant.merchant_id, launch_id)
    return EvaluationLaunchView.from_domain(await service.detail(merchant.merchant_id, launch_id))
