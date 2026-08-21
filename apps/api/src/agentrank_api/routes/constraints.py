"""Intent constraint and intent authorization endpoints.

Routes validate, delegate and serialize. No SQL, no business rule and no error translation:
the service decides what each operation means, and the handlers installed by `create_app`
decide what an error looks like.

Three operations, and deliberately not a fourth. Constraints are created once, read, and
evaluated against a checkout. There is no update and no delete, because an authorization
that can be edited is not an authorization, and changing what a buyer requires means
creating a new mandate with a new constraint set.

The evaluation endpoint is a read that writes nothing at all, exactly like the financial one
beside it. Neither combines with the other: two questions, two answers, and no endpoint that
folds them into a permission, because nothing here may act on one.
"""

import uuid
from typing import Any

from fastapi import APIRouter, status

from agentrank_api.checkout.service import CheckoutService
from agentrank_api.constraints.schemas import (
    CreateIntentConstraintsRequest,
    IntentAuthorizationView,
    IntentConstraintSetView,
)
from agentrank_api.constraints.service import IntentConstraintService
from agentrank_api.dependencies import MerchantDep, SessionDep
from agentrank_api.errors import ErrorResponse

router = APIRouter(prefix="/api/v1/commerce", tags=["intent constraints"])

# Annotated because FastAPI types this parameter as an invariant mapping of Any.
UNAUTHENTICATED: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse}
}
NOT_FOUND: dict[int | str, dict[str, Any]] = UNAUTHENTICATED | {
    status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}
}
NOT_FOUND_OR_CONFLICT: dict[int | str, dict[str, Any]] = NOT_FOUND | {
    status.HTTP_409_CONFLICT: {"model": ErrorResponse}
}


@router.post(
    "/mandates/{mandate_id}/constraints",
    response_model=IntentConstraintSetView,
    status_code=status.HTTP_201_CREATED,
    responses=NOT_FOUND_OR_CONFLICT,
)
async def create_intent_constraints(
    mandate_id: uuid.UUID,
    request: CreateIntentConstraintsRequest,
    session: SessionDep,
    merchant: MerchantDep,
) -> IntentConstraintSetView:
    """Qualify a mandate with the hard constraints a purchase must satisfy.

    The constraint set, its constraints and the audit event recording them are written in
    one transaction. Once only: a mandate already carrying a set answers 409, and a revoked
    mandate answers 409 as well, because it authorizes nothing to qualify.

    A financial constraint may be stated and is validated against the mandate rather than
    stored. A mandate that permits more than the buyer said is refused, so a stated limit
    cannot be quietly widened by the authorization that replaces it.

    The merchant is the authenticated one and the body cannot name one. A mandate granted to
    anybody else answers 404, so knowing a mandate identifier is no longer enough to decide
    what that mandate may buy.
    """
    command = request.to_command(mandate_id, merchant.merchant_id)
    constraint_set = await IntentConstraintService(session).create_constraints(
        command, credential_id=merchant.credential_id
    )
    return IntentConstraintSetView.from_model(constraint_set)


@router.get(
    "/mandates/{mandate_id}/constraints",
    response_model=IntentConstraintSetView,
    responses=NOT_FOUND,
)
async def get_intent_constraints(
    mandate_id: uuid.UUID, session: SessionDep, merchant: MerchantDep
) -> IntentConstraintSetView:
    """Fetch the constraints qualifying one mandate.

    A mandate with none answers 404 rather than an empty set. Absence of a semantic
    authorization is not a permissive one, and a body that looked like "no requirements"
    would invite exactly that reading.

    Another merchant's constraint set answers 404 for the same reason a missing one does. What
    a buyer required of a purchase is as private as the mandate it qualifies.
    """
    constraint_set = await IntentConstraintService(session).get_constraints(
        mandate_id, merchant_id=merchant.merchant_id
    )
    return IntentConstraintSetView.from_model(constraint_set)


@router.get(
    "/checkouts/{checkout_id}/intent-authorization",
    response_model=IntentAuthorizationView,
    responses=NOT_FOUND,
)
async def evaluate_intent_authorization(
    checkout_id: uuid.UUID, session: SessionDep
) -> IntentAuthorizationView:
    """Report whether this checkout is what the buyer asked for, and if not, why.

    A read. Nothing is written, including no audit event. The constraints are resolved
    through the mandate the quote was written against, so a caller cannot choose the terms,
    and the decision is made against the semantic snapshot the quote recorded, so a catalog
    change since then cannot move the answer.

    This is not `GET /checkouts/{id}/authorization`, which answers the financial question.
    A checkout can pass either and fail the other, and both have to allow before any payment
    could be considered safe.
    """
    decision = await CheckoutService(session).evaluate_intent_constraints(checkout_id)
    return IntentAuthorizationView.from_decision(decision)
