"""Turning a Standard Checkout callback into an AgentRank payment outcome, or refusing to.

Every field the browser sends is untrusted input. The customer's device is not this application
and is not Razorpay, and the only thing a callback proves on arrival is that somebody posted
some strings. Two independent checks turn that into a payment, and both are mandatory.

```text
SIGNATURE      HMAC-SHA256 over the STORED order id and the supplied payment id,
               compared in constant time. Proves the callback is authentic
CONFIRMATION   GET /v1/payments/{id} from Razorpay itself, checked against the
               attempt's amount, currency and order. Proves what actually happened
```

A valid signature alone is deliberately not enough. It proves authenticity and says nothing
about state: it is computed over two identifiers and would still verify for a payment that was
authorized and never captured, or one that was refunded a minute later. So the signature decides
whether to keep reading, and Razorpay's own record of the payment decides what happened. See
`agentrank_api.razorpay.translation` for the mapping, which lives in exactly one place.

The order identifier used for the signature is the column, never the `razorpay_order_id` the
browser sent. Verifying a payload against itself proves nothing about which order this
application was expecting. The supplied one is compared against the column separately and a
mismatch is refused by name, which is a cheaper and clearer answer than a signature failure for
what is usually a client bug.

The sequence, and its transaction boundaries:

```text
LOCAL           establish ownership, load the binding, check the supplied order id,
                verify the signature. No provider call, no state change
                |
                v
TRANSACTION 1   mark the attempt IN_FLIGHT and commit. A signature verified callback
                proves a provider payment exists, which is what IN_FLIGHT means
                |
                v
NETWORK         fetch the payment from Razorpay, with no transaction open
                |
                v
TRANSACTION 2   only for a confirmed capture: the existing outcome machinery marks
                the attempt SUCCEEDED, the checkout PAID, consumes the hold and
                decrements the stock, atomically
                |
                v
TRANSACTION 3   mark the binding CONFIRMED, which is evidence rather than state
```

The last two are separate because the outcome machinery owns its own transaction and commits
it, and that is the right way round: the authoritative record commits first and the evidence
follows. A crash between them leaves a SUCCEEDED payment and a binding still awaiting one, and
running this operation again heals it, because every step is idempotent.

What fails closed, meaning no outcome is applied, no reservation is consumed, no inventory moves
and no checkout becomes paid:

```text
another merchant's attempt          404, and Razorpay is called zero times
no binding, or no order on it       409, and Razorpay is called zero times
the supplied order id disagrees     409, and Razorpay is called zero times
the signature does not verify       409, and Razorpay is called zero times
Razorpay has no such payment        502
the payment is for another order    409
the payment amount disagrees        409
the payment currency disagrees      409
the payment is not captured         200, reported honestly, nothing applied
```

The first four cost the gateway nothing, which is the property the call counting tests assert.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.audit.models import ActorType
from agentrank_api.audit.repository import AuditRepository
from agentrank_api.config import RazorpayCredentials
from agentrank_api.errors import ConflictError, NotFoundError, UpstreamError
from agentrank_api.payments.execution import PaymentExecutionService, PaymentOutcome
from agentrank_api.payments.models import OutcomeSource, PaymentAttempt
from agentrank_api.payments.provider import PaymentProvider
from agentrank_api.payments.repository import PaymentAttemptRepository
from agentrank_api.razorpay.client import RazorpayClient
from agentrank_api.razorpay.entities import RazorpayPayment
from agentrank_api.razorpay.errors import (
    RazorpayRefusedError,
    RazorpayUnavailableError,
    RazorpayUnreadableError,
)
from agentrank_api.razorpay.models import RazorpayCheckout, RazorpayCheckoutStatus
from agentrank_api.razorpay.repository import RazorpayCheckoutRepository
from agentrank_api.razorpay.service import (
    GATEWAY_REFUSED,
    GATEWAY_UNAVAILABLE,
    GATEWAY_UNREADABLE,
    NOT_CONFIGURED,
    RAZORPAY_RESOURCE,
)
from agentrank_api.razorpay.signature import signature_matches
from agentrank_api.razorpay.translation import (
    ObservedState,
    PaymentObservation,
    as_provider_result,
    observe,
)

RAZORPAY_PAYMENT_VERIFIED = "razorpay.payment_verified"
RAZORPAY_PAYMENT_CONFIRMED = "razorpay.payment_confirmed"
RAZORPAY_CALLBACK_REJECTED = "razorpay.callback_rejected"

# A callback reports what a provider did, so the provider is the actor, exactly as it is for
# every other payment outcome event. The customer pressed the button and the provider decided
# whether the money moved.
CALLBACK_ACTOR = ActorType.PAYMENT_PROVIDER

# The stable reasons this operation refuses with.
NO_CHECKOUT = "razorpay_checkout_missing"
NO_ORDER = "razorpay_order_missing"
ORDER_DISAGREES = "razorpay_order_mismatch"
SIGNATURE_INVALID = "razorpay_signature_invalid"
PAYMENT_MISSING = "razorpay_payment_missing"
PAYMENT_MISMATCH = "razorpay_payment_mismatch"


@dataclass(frozen=True, slots=True)
class CheckoutCallback:
    """The three strings Standard Checkout hands back, and nothing is trusted about any of them.

    A frozen record rather than three loose parameters, because they are all opaque identifiers
    of similar shape and a positional call that swapped two would compile, run and reject every
    genuine payment.
    """

    payment_id: str
    order_id: str
    signature: str


@dataclass(frozen=True, slots=True)
class VerifiedPayment:
    """What one callback produced.

    `confirmed` is the only field that says money moved, and it is true only when Razorpay
    reported the payment captured and the AgentRank outcome was applied.

    `changed` says whether this call is what moved the payment, which is what makes a repeated
    callback observably idempotent rather than merely harmless.

    `state` is this application's own reading of the provider payment. A caller sees
    `PENDING` or `REVERSED` rather than a vendor status string, because a vendor's vocabulary in
    this application's responses is a vendor's vocabulary in a buyer agent's parser.

    `attempt` is the authoritative record and the only thing a caller should act on.
    """

    attempt: PaymentAttempt
    binding: RazorpayCheckout
    state: ObservedState
    confirmed: bool = False
    changed: bool = False
    conflicted: bool = False


class RazorpayVerificationService:
    """Verify one Standard Checkout callback and apply whatever it authorizes, which is at most
    one thing."""

    def __init__(
        self,
        session: AsyncSession,
        client: RazorpayClient | None,
        credentials: RazorpayCredentials | None,
        provider: PaymentProvider,
    ) -> None:
        self._session = session
        self._client = client
        self._credentials = credentials
        # The autonomous provider, needed only because `PaymentExecutionService` takes one at
        # construction. Nothing on this path calls it: an interactive payment is confirmed
        # through the Razorpay transport and converges on the outcome machinery, which is the
        # part of that service this uses.
        self._execution = PaymentExecutionService(session, provider)
        self._attempts = PaymentAttemptRepository(session)
        self._bindings = RazorpayCheckoutRepository(session)
        self._audit = AuditRepository(session)

    async def verify(
        self,
        attempt_id: uuid.UUID,
        *,
        merchant_id: uuid.UUID,
        callback: CheckoutCallback,
        credential_id: uuid.UUID | None = None,
    ) -> VerifiedPayment:
        """Accept a Standard Checkout success payload, or refuse it having changed nothing.

        Safe to call repeatedly, and repeatedly is the normal case: a browser can retry, a user
        can reload, and a network can duplicate. A callback for an already confirmed checkout
        still has its signature verified, and then answers with the settled state without asking
        Razorpay anything and without writing a second outcome.

        A cross merchant callback answers 404 and reaches Razorpay zero times. The merchant is
        the authenticated principal and it is a condition in the first two queries rather than a
        comparison after them, so submitting somebody else's attempt identifier reveals nothing
        and costs the gateway nothing.
        """
        credentials, client = self._configured()
        # Ownership first, and its result is deliberately discarded. What this establishes is
        # that the attempt exists under this merchant, and everything below re-reads whatever it
        # needs. A cross merchant callback stops here, before the binding is looked at and long
        # before Razorpay is asked anything.
        await self._owned(attempt_id, merchant_id=merchant_id)
        binding = await self._bound(attempt_id, merchant_id=merchant_id)
        order_id = binding.provider_order_id
        if order_id is None:
            # Not reachable while the check constraint keeps the status and the identifier in
            # agreement, and stated rather than assumed because everything below anchors on it.
            raise self._refuse(NO_ORDER, "this razorpay checkout has no order to verify against")

        if callback.order_id != order_id:
            await self._reject(
                binding, callback, reason=ORDER_DISAGREES, credential_id=credential_id
            )
            raise self._refuse(
                ORDER_DISAGREES,
                "the callback names a different razorpay order than this payment was prepared for",
                identifier=binding.provider_receipt,
            )

        if not signature_matches(
            # The stored identifier, never the one the browser sent. Razorpay's documentation
            # says so explicitly, and the reason is that verifying a payload against itself
            # proves nothing about which order this application was expecting.
            order_id=order_id,
            payment_id=callback.payment_id,
            presented=callback.signature,
            key_secret=credentials.key_secret,
        ):
            await self._reject(
                binding, callback, reason=SIGNATURE_INVALID, credential_id=credential_id
            )
            raise self._refuse(
                SIGNATURE_INVALID,
                "the callback signature is not authentic and nothing was applied",
                identifier=binding.provider_receipt,
            )

        if binding.status is RazorpayCheckoutStatus.CONFIRMED:
            # Already settled, and the signature above proves this repeat is genuine. Nothing to
            # learn from the gateway and nothing to write.
            settled = await self._owned(attempt_id, merchant_id=merchant_id)
            return VerifiedPayment(
                attempt=settled,
                binding=binding,
                state=ObservedState.SUCCEEDED,
                confirmed=True,
            )

        # A verified callback proves a provider payment exists, which is exactly what IN_FLIGHT
        # means. Committed before the confirmation query, so the doubt stays on the side that
        # cannot charge anybody twice.
        await self._execution.record_external_dispatch(attempt_id)

        with _translated_gateway_errors():
            payment = await client.fetch_payment(callback.payment_id)
        if payment is None:
            raise UpstreamError(
                PAYMENT_MISSING, "razorpay has no record of the payment this callback names"
            )
        self._require_matching_payment(payment, binding)

        observation = observe(payment)
        await self._append_verified(binding, observation, credential_id=credential_id)
        if not observation.is_success:
            # Reported honestly and applied to nothing. A pending, reversed or unrecognized
            # provider payment is not an AgentRank success, and it is deliberately not an
            # AgentRank failure either: a Razorpay order survives a payment that collected
            # nothing and can be paid again, so releasing the hold here would release stock a
            # customer could still buy through, on an attempt that would then be terminal and
            # uncorrectable.
            current = await self._owned(attempt_id, merchant_id=merchant_id)
            return VerifiedPayment(attempt=current, binding=binding, state=observation.state)

        outcome = await self._execution.apply_provider_observation(
            attempt_id, as_provider_result(observation), source=OutcomeSource.INTERACTIVE
        )
        confirmed = await self._confirm(
            attempt_id,
            merchant_id=merchant_id,
            outcome=outcome,
            observation=observation,
            credential_id=credential_id,
        )
        return VerifiedPayment(
            attempt=outcome.attempt,
            binding=confirmed,
            state=observation.state,
            confirmed=outcome.conflict is None,
            changed=outcome.changed,
            conflicted=outcome.conflict is not None,
        )

    def _configured(self) -> tuple[RazorpayCredentials, RazorpayClient]:
        """The credentials and the transport, or a refusal that names what is missing."""
        if self._credentials is None or self._client is None:
            raise ConflictError(
                NOT_CONFIGURED,
                "razorpay test mode credentials are not configured for this deployment",
                resource=RAZORPAY_RESOURCE,
            )
        return self._credentials, self._client

    async def _owned(self, attempt_id: uuid.UUID, *, merchant_id: uuid.UUID) -> PaymentAttempt:
        attempt = await self._attempts.get_for_merchant(attempt_id, merchant_id=merchant_id)
        if attempt is None:
            raise NotFoundError("payment_attempt", str(attempt_id))
        return attempt

    async def _bound(self, attempt_id: uuid.UUID, *, merchant_id: uuid.UUID) -> RazorpayCheckout:
        binding = await self._bindings.get_for_attempt(attempt_id, merchant_id=merchant_id)
        if binding is None:
            raise self._refuse(
                NO_CHECKOUT,
                "no razorpay checkout has been prepared for this payment",
                identifier=str(attempt_id),
            )
        if binding.provider_order_id is None:
            raise self._refuse(
                NO_ORDER,
                "this razorpay checkout has no order yet and cannot have been paid",
                identifier=str(attempt_id),
            )
        return binding

    def _require_matching_payment(
        self, payment: RazorpayPayment, binding: RazorpayCheckout
    ) -> None:
        """Refuse a provider payment that is not for this order, amount and currency.

        Three comparisons and no tolerance. The amount is checked against the binding, which
        carries the attempt's own amount through a composite foreign key, so this compares what
        Razorpay collected against what the mandate authorized rather than against a number some
        earlier step copied.

        A payment for a different order is the interesting one. A signature is computed over an
        order and a payment, so a valid signature already ties them together, and this is the
        second, independent statement of the same tie taken from Razorpay's own record rather
        than from a digest.
        """
        if payment.order_id != binding.provider_order_id:
            raise self._refuse(
                PAYMENT_MISMATCH,
                "the razorpay payment belongs to a different order and was not applied",
                identifier=payment.id,
            )
        if payment.amount_minor != binding.amount_minor:
            raise self._refuse(
                PAYMENT_MISMATCH,
                "the razorpay payment amount does not match the authorized amount",
                identifier=payment.id,
            )
        if payment.currency != binding.currency:
            raise self._refuse(
                PAYMENT_MISMATCH,
                "the razorpay payment currency does not match the authorized currency",
                identifier=payment.id,
            )

    async def _confirm(
        self,
        attempt_id: uuid.UUID,
        *,
        merchant_id: uuid.UUID,
        outcome: PaymentOutcome,
        observation: PaymentObservation,
        credential_id: uuid.UUID | None,
    ) -> RazorpayCheckout:
        """Record which provider payment settled this checkout, after the outcome has committed.

        Evidence rather than state, and deliberately committed second. The authoritative answer
        is `payment_attempt.status`, and it was written atomically with the checkout, the hold
        and the stock in the transaction above. This row says which Razorpay payment it was.

        A crash between the two leaves a SUCCEEDED payment and a binding still awaiting one.
        Running the verification again heals it: the signature verifies, the outcome machinery
        finds the attempt already SUCCEEDED and reports it unchanged, and this writes the
        confirmation. Nothing is applied twice, because every step reports whether it moved
        anything.

        A conflict leaves the binding alone. `outcome.conflict` means somebody else recorded a
        contradictory terminal state first, and that state stands: writing CONFIRMED beside a
        FAILED attempt would make the binding claim something the authoritative row denies.
        """
        binding = await self._bindings.get_for_update(attempt_id, merchant_id=merchant_id)
        if binding is None:
            # Not reachable: it was read at the start of this operation and never deleted.
            raise NotFoundError(RAZORPAY_RESOURCE, str(attempt_id))
        if outcome.conflict is not None:
            await self._session.commit()
            return binding

        if not await self._bindings.mark_confirmed(
            binding, provider_payment_id=observation.payment_id
        ):
            await self._session.commit()
            return binding

        await self._audit.append(
            merchant_id=binding.merchant_id,
            actor_type=CALLBACK_ACTOR,
            credential_id=credential_id,
            event_type=RAZORPAY_PAYMENT_CONFIRMED,
            resource_type=RAZORPAY_RESOURCE,
            resource_id=binding.id,
            payload={
                "payment_attempt_id": str(binding.payment_attempt_id),
                "provider_order_id": binding.provider_order_id,
                "provider_payment_id": observation.payment_id,
                "amount_minor": binding.amount_minor,
                "currency": binding.currency,
                "status": binding.status.value,
            },
        )
        await self._session.commit()
        return binding

    async def _append_verified(
        self,
        binding: RazorpayCheckout,
        observation: PaymentObservation,
        *,
        credential_id: uuid.UUID | None,
    ) -> None:
        """Record that a genuine callback arrived and what the provider said about it.

        Its own transaction, for the same reason `payment.reconciled` has one: it records that
        somebody looked, which is worth having separately from what they found, and an
        observation that authorizes no outcome has no state to be atomic with.

        The provider state recorded here is this application's reading rather than the vendor's
        string. A trail written in somebody else's vocabulary is a trail that changes meaning
        when they revise it.
        """
        await self._audit.append(
            merchant_id=binding.merchant_id,
            actor_type=CALLBACK_ACTOR,
            credential_id=credential_id,
            event_type=RAZORPAY_PAYMENT_VERIFIED,
            resource_type=RAZORPAY_RESOURCE,
            resource_id=binding.id,
            payload={
                "payment_attempt_id": str(binding.payment_attempt_id),
                "provider_order_id": binding.provider_order_id,
                "provider_payment_id": observation.payment_id,
                "provider_state": observation.state.value,
            },
        )
        await self._session.commit()

    async def _reject(
        self,
        binding: RazorpayCheckout,
        callback: CheckoutCallback,
        *,
        reason: str,
        credential_id: uuid.UUID | None,
    ) -> None:
        """Record a callback that did not survive verification, and commit before refusing.

        Worth a row of its own. A payload that names the wrong order or carries a signature that
        does not verify is either a broken integration or somebody trying, and both are things
        an operator should be able to find afterwards. The payment identifier is recorded because
        it is the only handle on what was claimed; the signature is not, because storing rejected
        digests would be storing an attacker's work for them.

        Committed here rather than left to the caller, because the caller's next statement raises
        and a raise rolls back.
        """
        await self._audit.append(
            merchant_id=binding.merchant_id,
            actor_type=CALLBACK_ACTOR,
            credential_id=credential_id,
            event_type=RAZORPAY_CALLBACK_REJECTED,
            resource_type=RAZORPAY_RESOURCE,
            resource_id=binding.id,
            payload={
                "payment_attempt_id": str(binding.payment_attempt_id),
                "provider_order_id": binding.provider_order_id,
                "claimed_payment_id": callback.payment_id,
                "reason": reason,
            },
        )
        await self._session.commit()

    def _refuse(self, reason: str, detail: str, *, identifier: str | None = None) -> ConflictError:
        return ConflictError(reason, detail, resource=RAZORPAY_RESOURCE, identifier=identifier)


@contextmanager
def _translated_gateway_errors() -> Iterator[None]:
    """Turn a vendor shaped failure into this application's own vocabulary.

    The same mapping the preparation path uses, restated here rather than shared through a
    third module, because there are two call sites and an indirection for two call sites costs
    more to read than it saves.
    """
    try:
        yield
    except RazorpayUnavailableError as unavailable:
        raise UpstreamError(
            GATEWAY_UNAVAILABLE, "razorpay did not answer, so nothing may be concluded"
        ) from unavailable
    except RazorpayRefusedError as refused:
        raise UpstreamError(
            GATEWAY_REFUSED, "razorpay refused the request to confirm this payment"
        ) from refused
    except RazorpayUnreadableError as unreadable:
        raise UpstreamError(
            GATEWAY_UNREADABLE, "razorpay answered with something this application cannot read"
        ) from unreadable
