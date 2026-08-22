"""Razorpay Test Mode checkout endpoints.

Routes validate, delegate and serialize. No SQL, no business rule and no error translation: the
service decides what each operation means, and the handlers installed by `create_app` decide
what an error looks like.

Two operations, and deliberately not more. A checkout can be prepared from a quote, which admits
a payment and creates the provider order in one call, and it can be prepared from an already
admitted payment attempt, which is the primitive the first one is built on. There is no refund,
no cancel, no capture and no webhook receiver, because none of them exists behind the transport
and an endpoint with nothing behind it is a promise.

Both require an authenticated merchant and act only on that merchant's payments. The rule is
worth stating twice on this surface: a cross merchant request answers 404, and it answers 404
having called Razorpay exactly zero times. The denial is the first statement of the first
transaction, well before anything external could be involved, and there are tests that count the
gateway calls rather than checking the database afterwards, because "no order exists for
merchant B" is also true when the call was made and failed.

Preparing a checkout is not a payment. Nothing here marks an attempt SUCCEEDED, marks a checkout
PAID, consumes a reservation or moves inventory. A Razorpay Order is an invitation to pay, and
the customer may never accept it.

Neither endpoint accepts an amount, a currency or a receipt. There is no request field for any
of them and no header that reaches one. All three come from the admitted payment attempt, which
is structurally bound to the quote that was authorized.
"""

import uuid
from typing import Any

from fastapi import APIRouter, status

from agentrank_api.dependencies import (
    MerchantDep,
    RazorpayCredentialsDep,
    RazorpayDep,
    SessionDep,
)
from agentrank_api.errors import ErrorResponse
from agentrank_api.payments.schemas import CreatePaymentRequest
from agentrank_api.razorpay.schemas import RazorpayCheckoutView, RazorpayPreparationView
from agentrank_api.razorpay.service import RazorpayCheckoutService

router = APIRouter(prefix="/api/v1/commerce", tags=["razorpay"])

# Annotated because FastAPI types this parameter as an invariant mapping of Any.
RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse},
    status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
    status.HTTP_409_CONFLICT: {"model": ErrorResponse},
    status.HTTP_502_BAD_GATEWAY: {"model": ErrorResponse},
}


@router.post(
    "/payments/{attempt_id}/razorpay-checkout",
    response_model=RazorpayCheckoutView,
    responses=RESPONSES,
)
async def prepare_razorpay_checkout(
    attempt_id: uuid.UUID,
    session: SessionDep,
    client: RazorpayDep,
    credentials: RazorpayCredentialsDep,
    merchant: MerchantDep,
) -> RazorpayCheckoutView:
    """Give an admitted payment a Razorpay order, and return what the browser needs.

    The primitive. It takes no request body, because there is nothing a caller may state: the
    amount and the currency come from the payment attempt, the receipt is derived from the
    merchant and the attempt, and the merchant comes from the credential presented.

    Idempotent, and idempotent at Razorpay rather than only here. Calling this twice for one
    attempt produces one order: the second call finds it bound and does not reach the gateway at
    all. A call that follows one whose create response was lost recovers the existing order by
    its deterministic receipt rather than creating a second.

    Only an ADMITTED attempt may be prepared. A finished payment answers 409 naming which kind
    of finished it is, and a payment with a provider operation outstanding answers 409 too,
    because handing either a checkout page invites a customer to pay for something that is over
    or already being paid for.

    502 when Razorpay did not answer, refused, or answered with something unreadable. That is
    not a 409: nothing about the request or the state was wrong, and a caller that could not
    tell them apart would keep editing a request that was fine.

    409 `razorpay_order_mismatch` when the order that came back does not carry the amount, the
    currency and the receipt this payment authorized. It fails closed and the order is not
    presented, because the alternative is showing a customer a checkout that collects a number
    the mandate never approved.

    Another merchant's payment answers 404 and reaches Razorpay zero times.

    The response carries the public key id, because Standard Checkout cannot be opened without
    it. It does not carry the key secret, and there is no field it could be in.
    """
    prepared = await RazorpayCheckoutService(session, client, credentials).prepare(
        attempt_id, merchant_id=merchant.merchant_id, credential_id=merchant.credential_id
    )
    return RazorpayCheckoutView.from_preparation(prepared)


@router.post(
    "/checkouts/{checkout_id}/razorpay-checkout",
    response_model=RazorpayPreparationView,
    responses=RESPONSES,
)
async def prepare_razorpay_checkout_for_quote(
    checkout_id: uuid.UUID,
    request: CreatePaymentRequest,
    session: SessionDep,
    client: RazorpayDep,
    credentials: RazorpayCredentialsDep,
    merchant: MerchantDep,
) -> RazorpayPreparationView:
    """Admit a payment for this quote and prepare an interactive Razorpay checkout for it.

    The operation a merchant integration actually calls, and the reason it is not
    `POST /checkouts/{id}/payments` is worth stating. That route admits and then dispatches to
    the wired autonomous provider, which settles the payment immediately. An interactive payment
    has to reach ADMITTED and stop there, because the money is collected by a customer in a
    browser afterwards. Two routes, two settlement shapes, one set of admission rules.

    Admission is reused unchanged. Both authorization gates, an effective hold, an unconsumed
    mandate, no competing payment under the mandate, and the amount and currency frozen from the
    quote are all decided in one locked transaction exactly as they are for an autonomous
    payment. Nothing about interactive checkout relaxes any of it.

    200 rather than 201, and a refusal is an ordinary answer with a body rather than an error. A
    denied authorization, a lapsed hold and a payment somebody is already making are all facts
    about now, and a caller has to tell them apart to know whether to fix the request, wait, or
    stop. The same choice `pay` and `prepare-execution` make.

    Idempotent when the caller supplies `idempotency_key`, in both layers. One key against one
    quote is one payment attempt, and one payment attempt is one Razorpay order.

    A refusal reaches Razorpay zero times. Nothing is written and no order is created for a
    payment that was not allowed to start.
    """
    admission, prepared = await RazorpayCheckoutService(
        session, client, credentials
    ).prepare_for_checkout(
        checkout_id,
        merchant_id=merchant.merchant_id,
        idempotency_key=request.resolve_key(),
        credential_id=merchant.credential_id,
    )
    if prepared is None:
        return RazorpayPreparationView.refused(admission)
    return RazorpayPreparationView.prepared(
        admission, RazorpayCheckoutView.from_preparation(prepared)
    )
