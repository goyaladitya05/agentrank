"""API request and response models for the Razorpay bridge.

The response side is where the important rule lives, and it is a rule about absence. Standard
Checkout runs in the browser, so the browser has to be told the public key id, the order
identifier, the amount and the currency. It is told those four and a little display metadata,
and it is told nothing else. There is no field for the key secret, no field that could be
widened into one, and no serializer that reads a `SecretStr`.

The request side is shorter still. Preparing a checkout takes no body at all, because there is
nothing a caller could legitimately state: the amount and the currency come from the admitted
payment attempt, the receipt is derived, and the merchant comes from the authenticated
principal. A request that could carry an amount is a request that could carry a different one.

`test_mode` is on the wire rather than assumed by the frontend. The integration refuses a live
key at startup, so the value is always true today, and it comes from the server anyway because
a banner saying no real money is a claim about the backend and should be answered by the
backend.
"""

import uuid
from datetime import datetime
from typing import Self

from pydantic import BaseModel, Field

from agentrank_api.payments.admission import AdmissionRefusal, PaymentAdmission
from agentrank_api.payments.schemas import PaymentAttemptView
from agentrank_api.razorpay.models import RazorpayCheckoutStatus
from agentrank_api.razorpay.service import PreparedCheckout
from agentrank_api.razorpay.translation import ObservedState
from agentrank_api.razorpay.verification import CheckoutCallback, VerifiedPayment

# Razorpay identifiers are short prefixed strings. Bounded and restricted to characters that
# survive a URL, a header and a log without escaping, because a value that has to be encoded
# differently in two places is two values. Deliberately not a pattern asserting the vendor's
# prefixes: this is a safety bound on untrusted input, not a guess about their identifier
# scheme, and rejecting a valid identifier because they changed a prefix would break payments.
PROVIDER_IDENTIFIER = r"^[A-Za-z0-9_-]{1,64}$"

# A hex encoded SHA-256 digest, which is what Razorpay documents the payment signature as.
SIGNATURE = r"^[0-9a-f]{64}$"


class RazorpayCheckoutView(BaseModel):
    """Everything the browser legitimately needs to open Standard Checkout.

    `key_id` is public by design. Razorpay's own documentation puts it in the page, and
    Standard Checkout cannot be opened without it. `key_secret` has no field here and never
    will: it authenticates this application to Razorpay and it is what verifies a callback
    signature, so a browser holding one could forge a payment confirmation.

    `amount_minor` and `currency` are echoed for display only. The browser passes the order
    identifier to Razorpay and Razorpay reads the amount off the order it holds, so a tampered
    value here changes what a page renders and cannot change what anybody is charged.

    `created` and `recovered` describe how this call arrived at the order. Both false means it
    already existed and the gateway was not called at all, which is what a repeated preparation
    looks like and what makes the idempotency observable from outside.
    """

    payment_attempt_id: uuid.UUID
    checkout_id: uuid.UUID
    merchant_id: uuid.UUID
    merchant_name: str
    key_id: str
    provider_order_id: str
    provider_receipt: str
    amount_minor: int
    currency: str
    status: RazorpayCheckoutStatus
    test_mode: bool
    created: bool
    recovered: bool
    order_created_at: datetime | None

    @classmethod
    def from_preparation(cls, prepared: PreparedCheckout) -> Self:
        binding = prepared.binding
        if binding.provider_order_id is None:
            # Not reachable: a preparation returns only after an order is bound, and the check
            # constraint keeps the identifier and the status in agreement.
            raise ValueError(f"razorpay checkout {binding.id} has no order to present")
        return cls(
            payment_attempt_id=binding.payment_attempt_id,
            checkout_id=prepared.checkout_id,
            merchant_id=binding.merchant_id,
            merchant_name=prepared.merchant_name,
            key_id=prepared.key_id,
            provider_order_id=binding.provider_order_id,
            provider_receipt=binding.provider_receipt,
            amount_minor=binding.amount_minor,
            currency=binding.currency,
            status=binding.status,
            # Always true while a live key cannot be configured. Served from here rather than
            # hardcoded in the console, because "no real money" is a claim about this process.
            test_mode=True,
            created=prepared.created,
            recovered=prepared.recovered,
            order_created_at=binding.order_created_at,
        )


class RazorpayPreparationView(BaseModel):
    """The answer to preparing a checkout from a quote, whether or not one was prepared.

    The same shape of answer `PaymentView` gives, and for the same reason: a caller that cannot
    tell "you may not buy this" from "somebody is already paying for it" will retry the same
    request forever, and the two call for opposite next moves. So an admission refusal is an
    ordinary 200 carrying a reason rather than an error.

    `razorpay` is null exactly when `admitted` is false. There is no state in which a checkout
    was prepared for a payment that was not admitted.
    """

    admitted: bool
    created: bool
    checkout_id: uuid.UUID
    refusal: AdmissionRefusal | None
    attempt: PaymentAttemptView | None
    razorpay: RazorpayCheckoutView | None

    @classmethod
    def refused(cls, admission: PaymentAdmission) -> Self:
        return cls(
            admitted=False,
            created=False,
            checkout_id=admission.checkout_id,
            refusal=admission.refusal,
            attempt=None
            if admission.attempt is None
            else PaymentAttemptView.from_model(admission.attempt),
            razorpay=None,
        )

    @classmethod
    def prepared(cls, admission: PaymentAdmission, checkout: RazorpayCheckoutView) -> Self:
        attempt = admission.attempt
        if attempt is None:
            # Not reachable: an admitted result always carries its attempt.
            raise ValueError("an admitted payment has no attempt to report")
        return cls(
            admitted=True,
            created=admission.created,
            checkout_id=admission.checkout_id,
            refusal=None,
            attempt=PaymentAttemptView.from_model(attempt),
            razorpay=checkout,
        )


class RazorpayCallbackRequest(BaseModel):
    """The three fields Standard Checkout hands back, treated as hostile until proven otherwise.

    Every one of them arrives from a customer's browser. None is trusted, none selects anything,
    and none decides an amount. `razorpay_order_id` in particular is not what the signature is
    verified against: the stored order identifier is, and this field is compared to it and
    refused on a mismatch. A payload verified against itself proves nothing.

    Bounded here so that a value which cannot possibly be a Razorpay identifier is refused as a
    422 before it reaches a comparison, a log line or an audit payload. The signature bound is
    the documented digest shape rather than a guess.

    There is no amount here, no currency, no order status and no merchant. Every one of them
    comes from authoritative state, and a field a browser could set is a field a browser could
    set wrongly.
    """

    razorpay_payment_id: str = Field(pattern=PROVIDER_IDENTIFIER)
    razorpay_order_id: str = Field(pattern=PROVIDER_IDENTIFIER)
    razorpay_signature: str = Field(pattern=SIGNATURE)

    def to_callback(self) -> CheckoutCallback:
        return CheckoutCallback(
            payment_id=self.razorpay_payment_id,
            order_id=self.razorpay_order_id,
            signature=self.razorpay_signature,
        )


class RazorpayVerificationView(BaseModel):
    """What one verified callback produced, in this application's own words.

    `confirmed` is the only field that says money moved, and it is true only when Razorpay
    reported the payment captured and the AgentRank outcome was applied. `changed` says whether
    this call is what moved it, which is what makes a repeated callback observably idempotent.

    `provider_state` is this application's reading of the provider payment rather than the
    vendor's status string. A caller sees PENDING or REVERSED and never `authorized` or
    `refunded`, because a vendor's vocabulary in these responses is a vendor's vocabulary in a
    buyer agent's parser, and it changes without notice.

    `attempt` is the authoritative record and the only thing a caller should act on. Confirmed
    and `attempt.status == "SUCCEEDED"` say the same thing, and the attempt is the one that says
    it about the payment rather than about this request.
    """

    confirmed: bool
    changed: bool
    conflicted: bool
    provider_state: ObservedState
    checkout_id: uuid.UUID
    razorpay_status: RazorpayCheckoutStatus
    attempt: PaymentAttemptView

    @classmethod
    def from_verification(cls, verified: VerifiedPayment) -> Self:
        return cls(
            confirmed=verified.confirmed,
            changed=verified.changed,
            conflicted=verified.conflicted,
            provider_state=verified.state,
            checkout_id=verified.attempt.checkout_id,
            razorpay_status=verified.binding.status,
            attempt=PaymentAttemptView.from_model(verified.attempt),
        )
