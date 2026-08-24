"""Authenticated merchant commands for reviewing deterministic compiler facts."""

import uuid
from typing import Annotated, Any

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
    service = MerchantCompilerReviewService(session)
    await _review(service, merchant.merchant_id, candidate_id, ReviewDecision.ACCEPT)
    return await service.run_view(
        merchant.merchant_id, (await service._candidate(merchant.merchant_id, candidate_id)).run_id
    )


@router.post("/candidates/{candidate_id}/reject")
async def reject_candidate(
    candidate_id: uuid.UUID, session: SessionDep, merchant: MerchantDep
) -> CompilerRunReviewView:
    service = MerchantCompilerReviewService(session)
    await _review(service, merchant.merchant_id, candidate_id, ReviewDecision.REJECT)
    return await service.run_view(
        merchant.merchant_id, (await service._candidate(merchant.merchant_id, candidate_id)).run_id
    )


@router.post("/candidates/{candidate_id}/correct")
async def correct_candidate(
    candidate_id: uuid.UUID,
    request: CorrectCandidateRequest,
    session: SessionDep,
    merchant: MerchantDep,
) -> CompilerRunReviewView:
    service = MerchantCompilerReviewService(session)
    await _review(
        service,
        merchant.merchant_id,
        candidate_id,
        ReviewDecision.CORRECT,
        value=request.value,
        provenance_field=request.provenance_field,
        provenance_excerpt=request.provenance_excerpt,
    )
    return await service.run_view(
        merchant.merchant_id, (await service._candidate(merchant.merchant_id, candidate_id)).run_id
    )


@router.post("/runs/{run_id}/publish")
async def publish_compiler_run(
    run_id: uuid.UUID, session: SessionDep, merchant: MerchantDep
) -> CompilerRunReviewView:
    await MerchantCompilerReviewService(session)._compiler.publish(merchant.merchant_id, run_id)
    return await MerchantCompilerReviewService(session).run_view(merchant.merchant_id, run_id)


async def _review(
    service: MerchantCompilerReviewService,
    merchant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    decision: ReviewDecision,
    **kwargs: Any,
) -> None:
    try:
        await service.review(merchant_id, candidate_id, decision, **kwargs)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
        ) from error
