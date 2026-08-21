"""Checkout endpoints.

Routes validate, delegate and serialize. No SQL, no business rule and no error translation:
the service decides what each operation means, and the handlers installed by `create_app`
decide what an error looks like.

Six operations, and deliberately not a seventh. A quote is created, read, checked twice,
prepared and withdrawn. There is no update, because a quote is written once and repricing one
means quoting again, and no list, because nothing needs one yet and one without paging would
be a promise that a caller has seen everything when they have not.

There is no completion endpoint and no payment endpoint. Nothing here moves money, and
nothing here can. `prepare-execution` says a checkout is safe to attempt, which is not the
same as attempted and is very much not the same as paid.
"""

import uuid
from typing import Any

from fastapi import APIRouter, status

from agentrank_api.checkout.execution import CheckoutExecutionService
from agentrank_api.checkout.schemas import (
    CheckoutAuthorizationView,
    CheckoutView,
    CreateCheckoutRequest,
    ExecutionAuthorizationView,
    ExecutionPreparationView,
)
from agentrank_api.checkout.service import CheckoutService
from agentrank_api.dependencies import SessionDep
from agentrank_api.errors import ErrorResponse

router = APIRouter(prefix="/api/v1/commerce/checkouts", tags=["checkouts"])

# Annotated because FastAPI types this parameter as an invariant mapping of Any.
NOT_FOUND: dict[int | str, dict[str, Any]] = {status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}}
NOT_FOUND_OR_CONFLICT: dict[int | str, dict[str, Any]] = NOT_FOUND | {
    status.HTTP_409_CONFLICT: {"model": ErrorResponse}
}


@router.post(
    "",
    response_model=CheckoutView,
    status_code=status.HTTP_201_CREATED,
    responses=NOT_FOUND_OR_CONFLICT,
)
async def create_checkout(request: CreateCheckoutRequest, session: SessionDep) -> CheckoutView:
    """Quote a selection of variants against a mandate.

    Prices come from the catalog, never from the request. The checkout, its lines and the
    audit event recording it are written in one transaction.

    This creates a quote. It does not authorize one: a total above what the mandate permits
    is quoted successfully and denied by the authorization read below.
    """
    checkout = await CheckoutService(session).create_checkout(request.to_command())
    return CheckoutView.from_model(checkout)


@router.get("/{checkout_id}", response_model=CheckoutView, responses=NOT_FOUND)
async def get_checkout(checkout_id: uuid.UUID, session: SessionDep) -> CheckoutView:
    """Fetch one quote with its lines, priced as they were quoted."""
    checkout = await CheckoutService(session).get_checkout(checkout_id)
    return CheckoutView.from_model(checkout)


@router.get(
    "/{checkout_id}/authorization",
    response_model=CheckoutAuthorizationView,
    responses=NOT_FOUND,
)
async def authorize_checkout(
    checkout_id: uuid.UUID, session: SessionDep
) -> CheckoutAuthorizationView:
    """Report whether this checkout is financially authorized right now, and if not, why.

    A read. Nothing is written, including no audit event: an event per evaluation would
    turn the trail into a request log. It answers about the mandate the checkout was written
    against, and about the quote as recorded, so a catalog change since then cannot move the
    answer.
    """
    decision = await CheckoutService(session).authorize_checkout(checkout_id)
    return CheckoutAuthorizationView.from_decision(decision)


@router.get(
    "/{checkout_id}/execution-authorization",
    response_model=ExecutionAuthorizationView,
    responses=NOT_FOUND,
)
async def read_execution_authorization(
    checkout_id: uuid.UUID, session: SessionDep
) -> ExecutionAuthorizationView:
    """Report what both authorization gates say about this checkout right now.

    Informational, and named so. It writes nothing, locks nothing and reserves nothing, so it
    grants nothing: a caller reading `authorized: true` here has learned what was true when
    they asked and nothing more. Preparation below evaluates all of it again, because a
    mandate can be revoked and a checkout can expire in between.

    It is not called `execution-readiness`, because readiness also needs stock and this
    endpoint deliberately holds none.
    """
    decision = await CheckoutExecutionService(session).execution_authorization(checkout_id)
    return ExecutionAuthorizationView.from_decision(decision)


@router.post(
    "/{checkout_id}/prepare-execution",
    response_model=ExecutionPreparationView,
    responses=NOT_FOUND,
)
async def prepare_execution(
    checkout_id: uuid.UUID, session: SessionDep
) -> ExecutionPreparationView:
    """Make this checkout safe to attempt, or say exactly why it is not.

    The authoritative operation. Both authorization gates are evaluated against the current
    time, and only if both allow is inventory locked and reserved. A refusal writes nothing
    and holds nothing.

    Idempotent while the reservation it made is still effective: preparing again returns the
    same reservation and records no second event.

    A denied authorization and an empty shelf are both ordinary outcomes and both answer 200
    with `ready: false` and the reasons in the body. Neither is an error: the request was well
    formed and the resources exist. A 404 means the checkout does not exist.

    Nothing here pays. Ready means a payment could be attempted, and no payment provider
    exists to attempt one.
    """
    readiness = await CheckoutExecutionService(session).prepare_execution(checkout_id)
    return ExecutionPreparationView.from_readiness(readiness)


@router.post("/{checkout_id}/cancel", response_model=CheckoutView, responses=NOT_FOUND)
async def cancel_checkout(checkout_id: uuid.UUID, session: SessionDep) -> CheckoutView:
    """Withdraw a quote.

    Idempotent: cancelling an already cancelled checkout returns it unchanged, moves nothing
    and records no second event. Cancellation is terminal, so there is no counterpart that
    reopens one.
    """
    checkout = await CheckoutService(session).cancel_checkout(checkout_id)
    return CheckoutView.from_model(checkout)
