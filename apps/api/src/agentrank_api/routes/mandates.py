"""Spending mandate endpoints.

Routes validate, delegate and serialize. No SQL, no business rule and no error
translation: the service decides what each operation means, and the handlers installed by
`create_app` decide what an error looks like.

Four operations, and deliberately not a fifth. There is no update and no list. An
authorization is created, read, checked and revoked; changing one means creating a new
one, which is what keeps the history straight.

All four require an authenticated merchant and act only on that merchant's mandates. Creation
takes the merchant from the credential rather than from the body, and the other three resolve
the mandate through a query that carries it, so another merchant's authorization answers 404
rather than being refused.
"""

import uuid
from typing import Any

from fastapi import APIRouter, status

from agentrank_api.dependencies import MerchantDep, SessionDep
from agentrank_api.errors import ErrorResponse
from agentrank_api.mandates.schemas import (
    CreateMandateRequest,
    MandateValidationView,
    MandateView,
)
from agentrank_api.mandates.service import MandateService

router = APIRouter(prefix="/api/v1/commerce/mandates", tags=["mandates"])

# Annotated because FastAPI types this parameter as an invariant mapping of Any.
UNAUTHENTICATED: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse}
}
NOT_FOUND: dict[int | str, dict[str, Any]] = UNAUTHENTICATED | {
    status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}
}


@router.post(
    "",
    response_model=MandateView,
    status_code=status.HTTP_201_CREATED,
    responses=NOT_FOUND,
)
async def create_mandate(
    request: CreateMandateRequest, session: SessionDep, merchant: MerchantDep
) -> MandateView:
    """Authorize spending with one merchant, within an amount and a validity window.

    The mandate and the audit event recording its creation are written in one
    transaction. An unknown merchant is a 404 rather than a constraint violation.

    The merchant is the authenticated one and the body cannot name one, so a caller cannot
    create a financial authorization against somebody else's shop.
    """
    mandate = await MandateService(session).create_mandate(
        request.to_command(merchant.merchant_id), credential_id=merchant.credential_id
    )
    return MandateView.from_model(mandate)


@router.get("/{mandate_id}", response_model=MandateView, responses=NOT_FOUND)
async def get_mandate(
    mandate_id: uuid.UUID, session: SessionDep, merchant: MerchantDep
) -> MandateView:
    """Fetch one of this merchant's mandates.

    Another merchant's mandate answers 404, and so does an identifier nobody has ever used. How
    much a merchant's buyers are authorized to spend is not something a competitor should be
    able to read by walking identifiers.
    """
    mandate = await MandateService(session).get_mandate(
        mandate_id, merchant_id=merchant.merchant_id
    )
    return MandateView.from_model(mandate)


@router.get(
    "/{mandate_id}/validation",
    response_model=MandateValidationView,
    responses=NOT_FOUND,
)
async def validate_mandate(
    mandate_id: uuid.UUID, session: SessionDep, merchant: MerchantDep
) -> MandateValidationView:
    """Report whether this mandate is usable right now, and if not, why not.

    A read. It exists so that a caller gets the authoritative answer from the same rule
    execution will use, rather than reimplementing the comparison against the window and
    the status and drifting away from it.
    """
    result = await MandateService(session).validate_mandate(
        mandate_id, merchant_id=merchant.merchant_id
    )
    return MandateValidationView.from_result(result)


@router.post("/{mandate_id}/revoke", response_model=MandateView, responses=NOT_FOUND)
async def revoke_mandate(
    mandate_id: uuid.UUID, session: SessionDep, merchant: MerchantDep
) -> MandateView:
    """Withdraw a mandate.

    Idempotent: revoking an already revoked mandate returns it unchanged, moves nothing
    and records no second event. Revocation is terminal, so there is no counterpart that
    brings one back.
    """
    mandate = await MandateService(session).revoke_mandate(
        mandate_id, merchant_id=merchant.merchant_id, credential_id=merchant.credential_id
    )
    return MandateView.from_model(mandate)
