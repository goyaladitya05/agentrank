"""Authenticated merchant commands for reviewing deterministic compiler facts.

Explicit commands rather than CRUD. A browser may say accept, correct, reject or publish about
a candidate it owns, and nothing else: ownership, candidate state, attribute type, unit,
provenance validity, review admission, publish readiness and representation lineage are all
decided by the compiler domain from persisted evidence.

Only one translation happens here. The domain raises `ValueError` for a correction that is not
a valid correction, and that is a 422 because the request itself is wrong. Everything else the
domain raises already has a response: `NotFoundError` is a 404, including for another merchant's
identifier, and `ConflictError` is a 409 for state that refuses a well formed request.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from agentrank_api.compiler.models import ReviewDecision
from agentrank_api.compiler.schemas import (
    CompilerOverviewView,
    CompilerRunReviewView,
    CorrectCandidateRequest,
)
from agentrank_api.compiler.views import MerchantCompilerReviewService
from agentrank_api.dependencies import MerchantDep, SessionDep

router = APIRouter(prefix="/api/v1/compiler", tags=["compiler"])


@router.get("/overview")
async def compiler_overview(
    session: SessionDep,
    merchant: MerchantDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> CompilerOverviewView:
    return await MerchantCompilerReviewService(session).overview(merchant.merchant_id, limit=limit)


@router.get("/runs/{run_id}")
async def compiler_run(
    run_id: uuid.UUID, session: SessionDep, merchant: MerchantDep
) -> CompilerRunReviewView:
    return await MerchantCompilerReviewService(session).run_view(merchant.merchant_id, run_id)


@router.post("/candidates/{candidate_id}/accept")
async def accept_candidate(
    candidate_id: uuid.UUID, session: SessionDep, merchant: MerchantDep
) -> CompilerRunReviewView:
    return await _command(
        MerchantCompilerReviewService(session),
        merchant.merchant_id,
        candidate_id,
        ReviewDecision.ACCEPT,
    )


@router.post("/candidates/{candidate_id}/reject")
async def reject_candidate(
    candidate_id: uuid.UUID, session: SessionDep, merchant: MerchantDep
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
    merchant: MerchantDep,
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
    run_id: uuid.UUID, session: SessionDep, merchant: MerchantDep
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
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
        ) from error
