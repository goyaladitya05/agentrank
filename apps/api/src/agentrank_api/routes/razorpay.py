"""Razorpay Test Mode checkout endpoints.

Routes validate, delegate and serialize. No SQL, no business rule and no error translation: the
service decides what each operation means, and the handlers installed by `create_app` decide
what an error looks like.

Three operations, and deliberately not more. A checkout can be prepared from a quote, which
admits a payment and creates the provider order in one call; it can be prepared from an already
admitted payment attempt, which is the primitive the first one is built on; and a Standard
Checkout success payload can be submitted for verification. There is no refund, no cancel, no
capture and no webhook receiver, because none of them exists behind the transport and an
endpoint with nothing behind it is a promise.

All three require an authenticated merchant and act only on that merchant's payments. The rule is
worth stating twice on this surface: a cross merchant request answers 404, and it answers 404
having called Razorpay exactly zero times. The denial is the first statement of the first
transaction, well before anything external could be involved, and there are tests that count the
gateway calls rather than checking the database afterwards, because "no order exists for
merchant B" is also true when the call was made and failed.

Preparing a checkout is not a payment. Nothing here marks an attempt SUCCEEDED, marks a checkout
PAID, consumes a reservation or moves inventory. A Razorpay Order is an invitation to pay, and
the customer may never accept it.

No endpoint here accepts an amount, a currency or a receipt. There is no request field for any
of them and no header that reaches one. All three come from the admitted payment attempt, which
is structurally bound to the quote that was authorized. The verification endpoint accepts three
identifiers from a browser and trusts none of them.
"""

import uuid
from typing import Any

from fastapi import APIRouter, status

from agentrank_api.dependencies import (
    MerchantDep,
    ProviderDep,
    RazorpayCredentialsDep,
    RazorpayDep,
    SessionDep,
)
from agentrank_api.errors import ErrorResponse
from agentrank_api.payments.schemas import CreatePaymentRequest
from agentrank_api.razorpay.schemas import (
    RazorpayCallbackRequest,
    RazorpayCheckoutView,
    RazorpayPreparationView,
    RazorpayVerificationView,
)
from agentrank_api.razorpay.service import RazorpayCheckoutService
from agentrank_api.razorpay.verification import RazorpayVerificationService

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


@router.post(
    "/payments/{attempt_id}/razorpay-checkout/verify",
    response_model=RazorpayVerificationView,
    responses=RESPONSES,
)
async def verify_razorpay_payment(
    attempt_id: uuid.UUID,
    request: RazorpayCallbackRequest,
    session: SessionDep,
    client: RazorpayDep,
    credentials: RazorpayCredentialsDep,
    provider: ProviderDep,
    merchant: MerchantDep,
) -> RazorpayVerificationView:
    """Submit a Standard Checkout success payload and let the server decide what it proves.

    Every field in the body arrives from a customer's browser and none of them is trusted. Two
    independent checks stand between this call and a merchant's stock:

    The signature must verify, as an HMAC-SHA256 over the order identifier this application
    stored and the payment identifier supplied, compared in constant time. The stored one is
    used deliberately: verifying a payload against itself proves nothing about which order this
    payment was prepared for. The supplied order identifier is compared to the column separately
    and a mismatch answers 409 by name, which is a clearer answer than a signature failure for
    what is usually a client bug.

    And the payment must be confirmed with Razorpay directly. A valid signature proves the
    callback is authentic and says nothing about state, because it is computed over two
    identifiers and would verify just as well for a payment that was authorized and never
    captured. So the provider's own record decides the outcome: captured is a success, and
    created, authorized, refunded and anything unrecognized are reported honestly and applied to
    nothing.

    A confirmed capture converges on exactly the machinery an autonomous payment uses. The
    attempt becomes SUCCEEDED, the checkout becomes PAID, the reservation is consumed and the
    stock is decremented, in one transaction, with the same locks in the same order. There is no
    second definition of paid in this application.

    Idempotent. A repeated callback verifies its signature, finds the checkout already confirmed
    and answers with the settled state, asking Razorpay nothing and writing no second outcome.
    Inventory decrements once, the reservation consumes once and the checkout becomes PAID once,
    however many times this is called.

    409 for anything that fails closed: no checkout prepared, no order on it, an order that
    disagrees, a signature that does not verify, or a provider payment whose order, amount or
    currency does not match what was authorized. None of them applies an outcome, consumes a
    reservation, moves inventory or marks a checkout paid.

    502 when Razorpay did not answer, refused, answered unreadably, or has no record of the
    payment being claimed.

    Another merchant's payment answers 404, and the first four refusals above reach Razorpay zero
    times.

    A rejected callback is recorded in the audit trail. A payload naming the wrong order or
    carrying a signature that does not verify is either a broken integration or somebody trying,
    and an operator should be able to find both afterwards.
    """
    verified = await RazorpayVerificationService(session, client, credentials, provider).verify(
        attempt_id,
        merchant_id=merchant.merchant_id,
        callback=request.to_callback(),
        credential_id=merchant.credential_id,
    )
    return RazorpayVerificationView.from_verification(verified)
