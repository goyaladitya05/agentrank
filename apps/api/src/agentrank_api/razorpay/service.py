"""Preparing one Razorpay Standard Checkout, without ever holding a lock across the wire.

Three steps, three transaction boundaries, and the shape is deliberately the same one
`PaymentExecutionService.dispatch` uses, because it is a reaction to the same fact: a database
transaction and an external side effect cannot be committed together.

```text
TRANSACTION 1   establish ownership, require eligibility, lock the attempt,
                write the binding with its derived receipt, commit
                |
                v
NETWORK         create the Razorpay order, or recover the one that exists,
                with no transaction open
                |
                v
TRANSACTION 2   check the order against the attempt, bind it, audit, commit
```

The receipt is committed before the network call, which is what makes a lost create response
recoverable rather than merely regrettable. A crash anywhere after transaction 1 leaves a
PREPARING binding, and preparing again resolves it by asking Razorpay what exists under that
receipt instead of creating a second order.

Cross merchant denial happens in the first statement of transaction 1, before an idempotency
key is looked up, before a lock is taken and long before Razorpay could be involved. That
ordering is the security property and it is what the provider call count tests assert: a
request for another merchant's payment costs the gateway exactly nothing.

Creating an order is not a payment and nothing here pretends otherwise. No attempt becomes
SUCCEEDED, no checkout becomes PAID, no reservation is consumed and no inventory moves. All of
that belongs to `agentrank_api.razorpay.verification`, and only after a signature verified
callback and a confirmed provider payment.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.audit.models import ActorType
from agentrank_api.audit.repository import AuditRepository
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.config import RazorpayCredentials
from agentrank_api.errors import ConflictError, NotFoundError, UpstreamError
from agentrank_api.payments.admission import PaymentAdmission, PaymentAdmissionService
from agentrank_api.payments.models import PaymentAttempt, PaymentAttemptStatus
from agentrank_api.payments.references import provider_operation_reference
from agentrank_api.payments.repository import PaymentAttemptRepository
from agentrank_api.razorpay.client import RazorpayClient
from agentrank_api.razorpay.entities import NewOrder
from agentrank_api.razorpay.errors import (
    RazorpayOrderMismatchError,
    RazorpayRefusedError,
    RazorpayUnavailableError,
    RazorpayUnreadableError,
)
from agentrank_api.razorpay.models import RazorpayCheckout
from agentrank_api.razorpay.orders import obtain_order, require_matching_order
from agentrank_api.razorpay.repository import RazorpayCheckoutRepository

RAZORPAY_RESOURCE = "razorpay_checkout"
RAZORPAY_ORDER_CREATED = "razorpay.order_created"

# A merchant integration asked for a checkout to be prepared. The provider has not decided
# anything yet, so attributing this to it would attribute a decision to somebody who has not
# made one. Same reasoning, and the same actor, as `payment.admitted`.
PREPARATION_ACTOR = ActorType.BUYER

# The stable reasons this operation refuses with. Codes rather than prose, for the same reason
# every other refusal here is one: a buyer agent has to tell "configure the integration" from
# "this payment is finished" from "the gateway did not answer" without reading English.
NOT_CONFIGURED = "razorpay_not_configured"
ORDER_MISMATCH = "razorpay_order_mismatch"
GATEWAY_UNAVAILABLE = "razorpay_unavailable"
GATEWAY_REFUSED = "razorpay_refused"
GATEWAY_UNREADABLE = "razorpay_unreadable"

# What goes in the Razorpay order's notes. Identifiers this application already holds, so that a
# human reading the provider dashboard can get from an order back to a row. The receipt is an
# opaque digest by necessity, and this is what makes it navigable. Nothing secret, nothing a
# caller supplied, nothing about a buyer.
ATTEMPT_NOTE = "agentrank_payment_attempt_id"
CHECKOUT_NOTE = "agentrank_checkout_id"


@dataclass(frozen=True, slots=True)
class PreparedCheckout:
    """A Razorpay order that Standard Checkout may now be opened against.

    `created` says whether this call is what produced the order at Razorpay, and `recovered`
    says whether it was found rather than created. Both are false for a preparation that met a
    binding already carrying an order, which is the ordinary idempotent repeat.

    The key id is here because the browser cannot open Standard Checkout without it. The key
    secret is not here, has no field to be in, and never leaves the process.
    """

    binding: RazorpayCheckout
    attempt: PaymentAttempt
    key_id: str
    merchant_name: str
    checkout_id: uuid.UUID
    created: bool = False
    recovered: bool = False


class RazorpayCheckoutService:
    """Prepare an interactive Razorpay checkout for one admitted payment attempt."""

    def __init__(
        self,
        session: AsyncSession,
        client: RazorpayClient | None,
        credentials: RazorpayCredentials | None,
    ) -> None:
        self._session = session
        # Both may be absent together, which is what an unconfigured integration looks like.
        # They are separate parameters because the transport is injectable for tests and the
        # credentials carry the public key id a response has to include.
        self._client = client
        self._credentials = credentials
        self._attempts = PaymentAttemptRepository(session)
        self._bindings = RazorpayCheckoutRepository(session)
        self._merchants = MerchantRepository(session)
        self._audit = AuditRepository(session)

    async def prepare(
        self,
        attempt_id: uuid.UUID,
        *,
        merchant_id: uuid.UUID,
        credential_id: uuid.UUID | None = None,
    ) -> PreparedCheckout:
        """Give this payment attempt a Razorpay order, or hand back the one it already has.

        Idempotent, and idempotent at the gateway rather than only locally. A repeat that finds
        a bound order returns it without calling Razorpay at all. A repeat that finds a
        PREPARING binding, which is what a lost create response leaves behind, asks Razorpay for
        the order under the deterministic receipt and binds that one. Neither path creates a
        second order.

        Only an ADMITTED attempt may be prepared. Every other state either has an answer already
        or has a provider payment outstanding, and giving one of those a checkout page would be
        inviting a customer to pay for something that is finished or already being paid for.
        Each is refused by name, reusing the vocabulary the dispatch path already refuses with,
        because a caller meeting them needs the same distinctions.

        A cross merchant request raises before anything is locked and before Razorpay is asked
        anything. `merchant_id` is the authenticated principal and it is a condition in the
        first query rather than a comparison after it.
        """
        credentials, client = self._configured()
        attempt = await self._eligible(attempt_id, merchant_id=merchant_id)
        merchant = await self._merchants.get_by_id(merchant_id)
        if merchant is None:
            # Not reachable: the attempt was found under this merchant, and the composite
            # foreign keys tie it to a merchant row.
            raise NotFoundError("merchant", str(merchant_id))
        merchant_name = merchant.name
        checkout_id = attempt.checkout_id

        binding = await self._reserved(attempt, merchant_id=merchant_id)
        if binding.provider_order_id is not None:
            # Already bound. The order was checked against this attempt when it was bound, the
            # binding's amount and currency are the attempt's own by composite foreign key, and
            # the bound order identifier is immutable, so there is nothing left to verify and no
            # reason to ask the gateway again.
            return PreparedCheckout(
                binding=binding,
                attempt=attempt,
                key_id=credentials.key_id,
                merchant_name=merchant_name,
                checkout_id=checkout_id,
            )

        request = _order_for(attempt, binding.provider_receipt)
        # Deliberately outside every transaction. The commit in `_reserved` ended the last one
        # and nothing below emits SQL until the gateway has answered.
        with _translated_gateway_errors():
            obtained = await obtain_order(client, request)
            require_matching_order(obtained.order, request)

        bound = await self._bind(
            attempt_id,
            merchant_id=merchant_id,
            provider_order_id=obtained.order.id,
            recovered=obtained.recovered,
            credential_id=credential_id,
        )
        return PreparedCheckout(
            binding=bound,
            attempt=attempt,
            key_id=credentials.key_id,
            merchant_name=merchant_name,
            checkout_id=checkout_id,
            created=not obtained.recovered,
            recovered=obtained.recovered,
        )

    async def prepare_for_checkout(
        self,
        checkout_id: uuid.UUID,
        *,
        merchant_id: uuid.UUID,
        idempotency_key: str,
        credential_id: uuid.UUID | None = None,
    ) -> tuple[PaymentAdmission, PreparedCheckout | None]:
        """Admit a payment for this quote and give it a Razorpay checkout, or say why not.

        The composition a merchant integration actually wants, and the reason it exists here
        rather than in a route is the same reason `PaymentService.pay` exists: the order of two
        operations and the handling of a refusal are decisions, not glue.

        It admits and it deliberately does not dispatch. `pay` admits and then sends the attempt
        to the wired autonomous provider, which would settle it against the deterministic fake
        before a customer could ever see a checkout page. An interactive payment has to reach
        the ADMITTED state and stop there, and this is the operation that does that.

        Admission is reused unchanged, so every rule it enforces still holds: both authorization
        gates, an effective hold, an unconsumed mandate, no competing payment, and the amount
        and the currency frozen from the quote, all decided in one locked transaction. Nothing
        about interactive checkout relaxes any of it.

        Idempotent through the application idempotency key exactly as `pay` is. Two requests
        carrying one key against one quote are one payment, and because preparation is itself
        idempotent they are also one Razorpay order.

        A refusal returns the admission and no checkout. Nothing was written and Razorpay was
        not called, so a caller that is told the mandate is spent has cost the gateway nothing.
        """
        # Before admission rather than after. A deployment that cannot prepare a checkout should
        # not admit a payment it will then be unable to do anything with.
        self._configured()
        admission = await PaymentAdmissionService(self._session).admit_payment(
            checkout_id,
            merchant_id=merchant_id,
            idempotency_key=idempotency_key,
            credential_id=credential_id,
        )
        attempt = admission.attempt
        if attempt is None:
            return admission, None
        prepared = await self.prepare(
            attempt.id, merchant_id=merchant_id, credential_id=credential_id
        )
        return admission, prepared

    def _configured(self) -> tuple[RazorpayCredentials, RazorpayClient]:
        """The credentials and the transport, or a refusal that names what is missing.

        Checked before anything is read, so an unconfigured deployment answers the same way for
        every request rather than after a database round trip. Both halves are present together
        or absent together: the settings validator refuses a partial pair at startup and the
        wiring builds a transport only when it has one.
        """
        if self._credentials is None or self._client is None:
            raise ConflictError(
                NOT_CONFIGURED,
                "razorpay test mode credentials are not configured for this deployment",
                resource=RAZORPAY_RESOURCE,
            )
        return self._credentials, self._client

    async def _eligible(self, attempt_id: uuid.UUID, *, merchant_id: uuid.UUID) -> PaymentAttempt:
        """The attempt this checkout is for, if it belongs here and may still be paid.

        Two refusals and they are different answers. An attempt that does not belong to this
        merchant, or does not exist, is a 404 carrying no information about which of the two it
        was. An attempt that exists and is in the wrong state is a 409 naming the state.
        """
        attempt = await self._attempts.get_for_merchant(attempt_id, merchant_id=merchant_id)
        if attempt is None:
            raise NotFoundError("payment_attempt", str(attempt_id))
        if attempt.status is not PaymentAttemptStatus.ADMITTED:
            refusal = _not_preparable(attempt)
            await self._session.rollback()
            raise refusal
        return attempt

    async def _reserved(
        self, attempt: PaymentAttempt, *, merchant_id: uuid.UUID
    ) -> RazorpayCheckout:
        """The binding for this attempt, created if it does not exist yet, and committed.

        The attempt is locked before the binding is read, in the documented order, which is what
        makes two simultaneous preparations resolve to one row rather than to one row and one
        unique violation. The unique constraint on `payment_attempt_id` is still there and is
        still what guarantees it; the lock is what stops a caller having to see it.

        Committed before returning, and that is the point of this method existing separately.
        The receipt has to be durable before the gateway is asked, because a create whose
        response never arrives is only recoverable if something remembers which receipt was
        used.
        """
        await self._attempts.get_for_update(attempt.id)
        existing = await self._bindings.get_for_update(attempt.id, merchant_id=merchant_id)
        if existing is not None:
            await self._session.commit()
            return existing

        created = await self._bindings.create(
            merchant_id=merchant_id,
            payment_attempt_id=attempt.id,
            # Derived, never accepted. The caller's idempotency key is not the provider
            # namespace and cannot influence this.
            provider_receipt=provider_operation_reference(merchant_id, attempt.id),
            # Read off the attempt, and equal to it by composite foreign key rather than by
            # this line being correct.
            amount_minor=attempt.amount_minor,
            currency=attempt.currency,
        )
        await self._session.commit()
        return created

    async def _bind(
        self,
        attempt_id: uuid.UUID,
        *,
        merchant_id: uuid.UUID,
        provider_order_id: str,
        recovered: bool,
        credential_id: uuid.UUID | None,
    ) -> RazorpayCheckout:
        """Record which Razorpay order this attempt is settled through, atomically with its trail.

        A fresh transaction, opened after the gateway has answered. The binding is reloaded
        under a lock rather than reused, because the object from the previous transaction was
        read before a network call and anything could have happened to the row since.

        A concurrent preparation that got there first is not an error. Both requests derive the
        same receipt, so Razorpay gives both the same order: one created it and the other
        recovered it from the duplicate receipt refusal. The loser finds the order already bound
        and appends no second event.
        """
        binding = await self._bindings.get_for_update(attempt_id, merchant_id=merchant_id)
        if binding is None:
            # Not reachable: `_reserved` committed this row before the gateway was called.
            raise NotFoundError(RAZORPAY_RESOURCE, str(attempt_id))

        if not await self._bindings.bind_order(binding, provider_order_id=provider_order_id):
            await self._session.commit()
            return binding

        await self._audit.append(
            merchant_id=binding.merchant_id,
            actor_type=PREPARATION_ACTOR,
            credential_id=credential_id,
            event_type=RAZORPAY_ORDER_CREATED,
            resource_type=RAZORPAY_RESOURCE,
            resource_id=binding.id,
            payload={
                "payment_attempt_id": str(binding.payment_attempt_id),
                "provider_order_id": provider_order_id,
                "provider_receipt": binding.provider_receipt,
                # What the customer will be asked for, restated because it is the number this
                # event exists to be able to answer questions about later.
                "amount_minor": binding.amount_minor,
                "currency": binding.currency,
                # Whether the order was created by this request or found after a create whose
                # answer was lost. Two different stories that leave identical rows behind.
                "recovered": recovered,
                "status": binding.status.value,
            },
        )
        await self._session.commit()
        return binding


def _order_for(attempt: PaymentAttempt, receipt: str) -> NewOrder:
    """What Razorpay is asked to create, read off the attempt and off nothing else.

    Not off the checkout, not off a request body, not off anything a browser sent. The amount
    and the currency on this row are what was authorized, held there by a composite foreign key
    onto an immutable quote, and reading them from anywhere else would be reading a number that
    could have moved since.

    The same values are also checked back against this object after the order comes home, which
    is what makes a mismatched order impossible to present rather than merely unlikely.
    """
    return NewOrder(
        amount_minor=attempt.amount_minor,
        currency=attempt.currency,
        receipt=receipt,
        notes={
            ATTEMPT_NOTE: str(attempt.id),
            CHECKOUT_NOTE: str(attempt.checkout_id),
        },
    )


def _not_preparable(attempt: PaymentAttempt) -> ConflictError:
    """The refusal that names why this attempt may not have an interactive checkout.

    Four states and four different next moves, and the codes are the ones the dispatch path
    already uses. A caller that has learned what `payment_unresolved` means from one payment
    route should not have to learn a second vocabulary for another.
    """
    if attempt.status is PaymentAttemptStatus.SUCCEEDED:
        return ConflictError(
            "payment_already_succeeded",
            f"payment attempt {attempt.id} has already succeeded",
            resource="payment_attempt",
            identifier=str(attempt.id),
        )
    if attempt.status is PaymentAttemptStatus.FAILED:
        return ConflictError(
            "payment_already_failed",
            f"payment attempt {attempt.id} has already been declined",
            resource="payment_attempt",
            identifier=str(attempt.id),
        )
    if attempt.status is PaymentAttemptStatus.UNKNOWN:
        return ConflictError(
            "payment_unresolved",
            f"payment attempt {attempt.id} has an unresolved result and must be reconciled",
            resource="payment_attempt",
            identifier=str(attempt.id),
        )
    return ConflictError(
        "payment_in_progress",
        f"payment attempt {attempt.id} already has a payment outstanding",
        resource="payment_attempt",
        identifier=str(attempt.id),
    )


@contextmanager
def _translated_gateway_errors() -> Iterator[None]:
    """Turn a vendor shaped failure into this application's own vocabulary.

    Razorpay's error classes stop here. What reaches a caller is a stable code and a sentence
    this repository wrote, never a vendor's prose and never a status code it chose. An
    integration whose error bodies leak an upstream's wording is an integration whose callers
    end up parsing it.

    The mapping is by what the caller can do about it. A gateway that did not answer, refused
    this application's own request, or answered with something unreadable are all 502: the
    caller did nothing wrong and there is nothing in the request to fix. An order that does not
    match the attempt is 409, because it is this application refusing to proceed with state it
    will not accept.
    """
    try:
        yield
    except RazorpayOrderMismatchError as mismatch:
        raise ConflictError(
            ORDER_MISMATCH,
            "the razorpay order does not match the authorized payment and was not used",
            resource=RAZORPAY_RESOURCE,
            identifier=mismatch.entity_id,
        ) from mismatch
    except RazorpayUnavailableError as unavailable:
        raise UpstreamError(
            GATEWAY_UNAVAILABLE, "razorpay did not answer, so nothing may be concluded"
        ) from unavailable
    except RazorpayRefusedError as refused:
        raise UpstreamError(
            GATEWAY_REFUSED, "razorpay refused the order request for this payment"
        ) from refused
    except RazorpayUnreadableError as unreadable:
        raise UpstreamError(
            GATEWAY_UNREADABLE, "razorpay answered with something this application cannot read"
        ) from unreadable
