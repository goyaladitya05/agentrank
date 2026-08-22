"""The one place a Razorpay payment state becomes something AgentRank may act on.

There is exactly one `== "captured"` in this repository and it is in this file. That is the
whole point of the module: a comparison against a vendor's status string scattered through
services is a decision made in several places, and the several places drift. Everything above
this asks for a `PaymentObservation` and branches on `ObservedState`.

## The mapping, from Razorpay's documented payment states

```text
captured     SUCCEEDED     the funds have been claimed. This is the only success
authorized   PENDING       the funds are held and have not been claimed
created      PENDING       the payment exists and nothing has happened to it
failed       FAILED        this payment definitively collected nothing
refunded     REVERSED      the funds were claimed and then returned
anything     UNRECOGNIZED  a state this integration has never seen
```

## Why captured and not authorized

Razorpay's default is auto capture: a completed payment moves to `captured` without this
application doing anything. So `captured` is the ordinary outcome of a successful Test Mode
checkout, and treating it as the success is not a narrow reading, it is the documented one.

`authorized` means the customer's funds are held and the merchant has not taken them. Razorpay
documents that an authorized payment must be captured within three days or it is refunded
automatically. Fulfilling against one would mean shipping goods for money that may never be
collected, and the failure mode is silent: nothing tells this application when that window
closes.

So authorized is PENDING, deliberately. Nothing is consumed, no checkout becomes paid, and the
attempt is left where it is for an operator to look at. Capturing it is not implemented in this
phase, because capture is a decision about a merchant's money and it needs its own operation,
its own authorization and its own audit trail rather than being a branch inside a callback
handler.

## Why refunded is not a failure

A refunded payment moved money and then moved it back. Calling it FAILED would be recording that
no money moved, which is false and is the one direction the payment kernel treats as expensive:
a failure releases stock. Calling it SUCCEEDED would consume stock for a payment the customer no
longer has. It is neither, so it is its own state and it produces no outcome.

## Why an unknown status is not pending

A status this integration has never seen might mean anything, including something that should
have been a success. PENDING would say "wait and it may resolve", which is a claim. UNRECOGNIZED
says nothing and produces no outcome, which is the only honest answer, and it is visible in the
response and the audit trail so somebody can look.

## The captured flag

Razorpay reports `status` and a separate `captured` boolean, and in every documented case they
agree. When they do not, this application has been handed two incompatible facts, and picking
one silently would be choosing which of them to believe without saying so. A disagreement is
UNRECOGNIZED and produces no outcome.
"""

from dataclasses import dataclass
from enum import StrEnum

from agentrank_api.payments.provider import ProviderOutcome, ProviderResult
from agentrank_api.razorpay.entities import RazorpayPayment

# Razorpay's documented payment states, as strings rather than as an enum this application
# owns. They are a vendor's vocabulary and they are only ever read here.
CAPTURED = "captured"
AUTHORIZED = "authorized"
CREATED = "created"
FAILED = "failed"
REFUNDED = "refunded"


class ObservedState(StrEnum):
    """What AgentRank may conclude from one Razorpay payment.

    Five values, and the split is by what this application is allowed to do rather than by what
    the vendor called it. Only the first one may consume a merchant's stock.

    SUCCEEDED
        The funds have been claimed. `captured`, with the vendor's own boolean agreeing.

    PENDING
        Something exists and it has not collected anything yet. `created` and `authorized`. Not
        a failure, so nothing is released, and not a success, so nothing is consumed.

    FAILED
        This payment definitively collected nothing. Note the scope: this payment, not this
        order. A Razorpay order survives a failed payment and can be paid again, so a failed
        payment is not a failed AgentRank attempt.

    REVERSED
        Money moved and came back. Neither of the two definitive answers the payment kernel
        knows, so it produces neither.

    UNRECOGNIZED
        A status this integration has never seen, or a status and a captured flag that
        contradict each other. Produces nothing and says so loudly.
    """

    SUCCEEDED = "SUCCEEDED"
    PENDING = "PENDING"
    FAILED = "FAILED"
    REVERSED = "REVERSED"
    UNRECOGNIZED = "UNRECOGNIZED"


_STATES: dict[str, ObservedState] = {
    CAPTURED: ObservedState.SUCCEEDED,
    AUTHORIZED: ObservedState.PENDING,
    CREATED: ObservedState.PENDING,
    FAILED: ObservedState.FAILED,
    REFUNDED: ObservedState.REVERSED,
}


@dataclass(frozen=True, slots=True)
class PaymentObservation:
    """What one Razorpay payment says, in this application's own words.

    `payment_id` travels with the state because the two are always used together: the outcome
    machinery records the provider's identifier for a success, and the audit trail records it
    whatever the state was.
    """

    state: ObservedState
    payment_id: str

    @property
    def is_success(self) -> bool:
        """Whether this observation permits an AgentRank success.

        A property rather than a comparison written at each call site, so that adding a state
        cannot accidentally widen what counts as paid.
        """
        return self.state is ObservedState.SUCCEEDED


def observe(payment: RazorpayPayment) -> PaymentObservation:
    """Translate one Razorpay payment into what this application may conclude from it.

    Pure. No clock, no database, no network. It reads two fields and a table.

    Unknown statuses and self contradictory ones both land on UNRECOGNIZED rather than raising.
    Raising would lose the payment identifier and the rest of the entity at exactly the moment
    somebody needs to look at them, and there is nothing here that a caller could fix by
    retrying.
    """
    state = _STATES.get(payment.status, ObservedState.UNRECOGNIZED)
    if state is ObservedState.SUCCEEDED and not payment.captured:
        # The vendor said `captured` and also said it was not captured. Two incompatible facts,
        # and choosing one quietly is how a merchant's stock leaves the shelf for a payment
        # nobody claimed.
        state = ObservedState.UNRECOGNIZED
    return PaymentObservation(state=state, payment_id=payment.id)


def as_provider_result(observation: PaymentObservation) -> ProviderResult:
    """The success this observation authorizes, in the payment kernel's own vocabulary.

    Only called for a success, and it refuses anything else rather than mapping it. That refusal
    is the guard rail: the kernel's `FAILED` releases a merchant's stock, and there is no state
    in this integration that this application is currently willing to release stock on. A
    Razorpay order outlives a failed payment and can be paid again, so terminalizing an attempt
    on one failed payment would release stock a customer could still buy through, and the
    attempt is terminal and could never be corrected.

    The reference is the Razorpay payment identifier, which is what a merchant looks a charge up
    by in the dashboard.
    """
    if not observation.is_success:
        raise ValueError(
            f"a {observation.state.value} razorpay payment authorizes no agentrank outcome"
        )
    return ProviderResult(outcome=ProviderOutcome.SUCCEEDED, reference=observation.payment_id)
