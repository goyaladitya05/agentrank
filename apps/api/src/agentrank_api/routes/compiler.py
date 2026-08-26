"""Authenticated merchant commands for running and reviewing deterministic compiler facts.

Explicit commands rather than CRUD. A browser may say compile this snapshot, or accept, correct,
reject or publish about a candidate it owns, and nothing else: ownership, candidate state,
attribute type, unit, provenance validity, review admission, publish readiness and representation
lineage are all decided by the compiler domain from persisted evidence.

Starting a run is a merchant command and not a side effect of supplying source evidence. A
snapshot is what the merchant says about their catalog and a run is a reading of that statement,
and one endpoint producing both would leave a merchant unable to say which of the two they meant.
It is also not a side effect of a benchmark result: the compiler reads source evidence and
nothing else, and no diagnostic, oracle answer or mission outcome reaches this command or the
extraction behind it.

`OperatorDep` rather than `MerchantDep` on every route here. A credential the benchmark runner
minted for a run authenticates as its merchant, and a buyer that could compile, review or publish
could change the representation it is being measured against while the run it is inside is still
executing.

Only one translation happens here. The domain raises `ValueError` for a correction that is not
a valid correction, and that is a 422 because the request itself is wrong. Everything else the
domain raises already has a response: `NotFoundError` is a 404, including for another merchant's
identifier, and `ConflictError` is a 409 for state that refuses a well formed request.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from agentrank_api.compiler.models import ReviewDecision
from agentrank_api.compiler.schemas import (
    CompilerOverviewView,
    CompilerRunReviewView,
    CorrectCandidateRequest,
    StartCompilerRunRequest,
)
from agentrank_api.compiler.service import MerchantCompilerService
from agentrank_api.compiler.views import MerchantCompilerReviewService
from agentrank_api.dependencies import OperatorDep, SessionDep
from agentrank_api.errors import invalid_request

router = APIRouter(prefix="/api/v1/compiler", tags=["compiler"])


@router.get("/overview")
async def compiler_overview(
    session: SessionDep,
    merchant: OperatorDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> CompilerOverviewView:
    return await MerchantCompilerReviewService(session).overview(merchant.merchant_id, limit=limit)


@router.get("/runs/{run_id}")
async def compiler_run(
    run_id: uuid.UUID, session: SessionDep, merchant: OperatorDep
) -> CompilerRunReviewView:
    return await MerchantCompilerReviewService(session).run_view(merchant.merchant_id, run_id)


@router.post("/runs", status_code=status.HTTP_201_CREATED)
async def start_compiler_run(
    request: StartCompilerRunRequest, session: SessionDep, merchant: OperatorDep
) -> CompilerRunReviewView:
    """Compile one immutable source snapshot this merchant owns, and answer with the run.

    Deterministic compilation of one snapshot under one configuration is content addressed: the
    run table is unique on the pair, so a double submit, a retry after a lost response and two
    concurrent requests all resolve to one run rather than to two readings of one document.
    There is no separate idempotency key here because there is nothing for one to add.

    201 for a run this request created and for one it found, for the same reason the source
    command answers 201 twice: a caller repeating its own request has to be able to learn what
    happened, and a 409 would say something went wrong when nothing did.

    The compiler is deterministic, bounded by the source document's own size limits, and calls
    nothing external, so it runs inside this request rather than being queued. Nothing here holds
    a transaction open across anything but its own work.
    """
    run = await MerchantCompilerService(session).run(
        merchant.merchant_id, request.source_snapshot_id
    )
    return await MerchantCompilerReviewService(session).run_view(merchant.merchant_id, run.id)


@router.post("/candidates/{candidate_id}/accept")
async def accept_candidate(
    candidate_id: uuid.UUID, session: SessionDep, merchant: OperatorDep
) -> CompilerRunReviewView:
    return await _command(
        MerchantCompilerReviewService(session),
        merchant.merchant_id,
        candidate_id,
        ReviewDecision.ACCEPT,
    )


@router.post("/candidates/{candidate_id}/reject")
async def reject_candidate(
    candidate_id: uuid.UUID, session: SessionDep, merchant: OperatorDep
) -> CompilerRunReviewView:
    return await _command(
        MerchantCompilerReviewService(session),
        merchant.merchant_id,
        candidate_id,
        ReviewDecision.REJECT,
    )


@router.post("/candidates/{candidate_id}/correct")
async def correct_candidate(
    candidate_id: uuid.UUID,
    request: CorrectCandidateRequest,
    session: SessionDep,
    merchant: OperatorDep,
) -> CompilerRunReviewView:
    return await _command(
        MerchantCompilerReviewService(session),
        merchant.merchant_id,
        candidate_id,
        ReviewDecision.CORRECT,
        value=request.value,
        provenance_field=request.provenance_field,
        provenance_excerpt=request.provenance_excerpt,
    )


@router.post("/runs/{run_id}/publish")
async def publish_compiler_run(
    run_id: uuid.UUID, session: SessionDep, merchant: OperatorDep
) -> CompilerRunReviewView:
    return await MerchantCompilerReviewService(session).publish(merchant.merchant_id, run_id)


async def _command(
    service: MerchantCompilerReviewService,
    merchant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    decision: ReviewDecision,
    *,
    value: str | int | bool | None = None,
    provenance_field: str | None = None,
    provenance_excerpt: str | None = None,
) -> CompilerRunReviewView:
    try:
        return await service.command(
            merchant_id,
            candidate_id,
            decision,
            value=value,
            provenance_field=provenance_field,
            provenance_excerpt=provenance_excerpt,
        )
    except ValueError as error:
        raise invalid_request(error) from error
