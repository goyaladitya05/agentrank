"""Payment endpoints.

Routes validate, delegate and serialize. No SQL, no business rule and no error translation:
the service decides what each operation means, and the handlers installed by `create_app`
decide what an error looks like.

Three operations, and deliberately not a fourth. A payment is created for a checkout, read
back, and reconciled when its result is unresolved. There is no refund, no capture, no void, no
cancellation of a payment and no webhook receiver, because none of them exists behind the
provider interface and an endpoint with nothing behind it is a promise.

There is no way to choose what a provider does. No query parameter, no header and no request
field selects a decline, a timeout or a lost response. The provider is chosen when the
application is built and configured by whoever built it, which for now is a deterministic fake.

Nothing here exposes a provider's vocabulary. What a caller reads is this application's own
record of what happened, which is the only thing they should be acting on.
"""

import uuid
from typing import Any

from fastapi import APIRouter, status

from agentrank_api.dependencies import ProviderDep, SessionDep
from agentrank_api.errors import ErrorResponse
from agentrank_api.payments.schemas import (
    CreatePaymentRequest,
    PaymentAttemptView,
    PaymentView,
    ReconciliationView,
)
from agentrank_api.payments.service import PaymentService

router = APIRouter(prefix="/api/v1/commerce", tags=["payments"])

# Annotated because FastAPI types this parameter as an invariant mapping of Any.
NOT_FOUND: dict[int | str, dict[str, Any]] = {status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}}
NOT_FOUND_OR_CONFLICT: dict[int | str, dict[str, Any]] = NOT_FOUND | {
    status.HTTP_409_CONFLICT: {"model": ErrorResponse}
}


@router.post(
    "/checkouts/{checkout_id}/payments",
    response_model=PaymentView,
    responses=NOT_FOUND_OR_CONFLICT,
)
async def pay_checkout(
    checkout_id: uuid.UUID,
    request: CreatePaymentRequest,
    session: SessionDep,
    provider: ProviderDep,
) -> PaymentView:
    """Pay for this checkout, or say exactly why it may not be paid for.

    The authoritative operation. Both authorization gates, an effective hold, an unconsumed
    mandate and no competing payment are all required in one locked transaction, and only then
    is a `PaymentAttempt` written and a provider called with what that attempt froze.

    200 rather than 201, and a refusal is an ordinary answer with a body rather than an error.
    A denied authorization, a lapsed hold and a payment somebody is already making are all
    facts about now, and a caller has to tell them apart to know whether to fix the request,
    wait, or stop. The same choice `prepare-execution` makes, for the same reason.

    Idempotent when the caller supplies `idempotency_key`. Two requests carrying the same key
    against the same checkout are one operation: the second answers with the first one's
    attempt, `created: false`, and no second provider call. That holds when the two arrive at
    the same instant as well as when one follows the other, so a client that retried on a
    timeout gets the payment rather than a conflict. Without a key each request is a new
    identity, which is safe and is not the same as idempotent.

    An attempt that a previous request admitted and never dispatched is dispatched by the
    retry. ADMITTED means the provider has provably never heard of it, so completing it is the
    recovery path for a request whose process died after admission committed. Every other
    state is returned as it stands and is never sent again.

    Admitted does not mean paid. Read `attempt.status`: SUCCEEDED means the money moved, FAILED
    means it definitively did not, and UNKNOWN means nobody knows yet and the payment has to be
    reconciled rather than retried.
    """
    result = await PaymentService(session, provider).pay(
        checkout_id, idempotency_key=request.resolve_key()
    )
    return PaymentView.from_admission(result.admission, result.attempt)


@router.get("/payments/{attempt_id}", response_model=PaymentAttemptView, responses=NOT_FOUND)
async def get_payment(
    attempt_id: uuid.UUID, session: SessionDep, provider: ProviderDep
) -> PaymentAttemptView:
    """Fetch one payment, with the amount and the currency that were frozen for it.

    A read. Nothing is written, no provider is asked and no clock decides anything: this is
    what the application currently believes, which for a resolved payment is final and for an
    unresolved one is exactly the uncertainty reconciliation exists to end.
    """
    attempt = await PaymentService(session, provider).get_attempt(attempt_id)
    return PaymentAttemptView.from_model(attempt)


@router.post(
    "/payments/{attempt_id}/reconcile",
    response_model=ReconciliationView,
    responses=NOT_FOUND_OR_CONFLICT,
)
async def reconcile_payment(
    attempt_id: uuid.UUID, session: SessionDep, provider: ProviderDep
) -> ReconciliationView:
    """Ask the provider what happened to a payment whose result is unresolved.

    A query, never a charge. It is the only way out of UNKNOWN, and it is a request rather than
    a background job on purpose: an ambiguous payment retried on a timer is a payment charged
    twice.

    Safe to call repeatedly. A settled payment is returned without asking the provider
    anything, and an attempt that is still unresolved is queried again and resolved at most
    once. A payment that has never been dispatched answers 409 `payment_not_dispatched`,
    because what it needs is a payment request rather than a query.
    """
    outcome = await PaymentService(session, provider).reconcile(attempt_id)
    return ReconciliationView.from_outcome(outcome)
