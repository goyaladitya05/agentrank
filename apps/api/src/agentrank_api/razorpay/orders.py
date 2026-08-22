"""Obtaining exactly one Razorpay Order for one payment attempt, and proving it is the right one.

Two problems live here and they are both consequences of the same fact: creating an order is an
external side effect, and an external side effect cannot be committed atomically with a database
transaction. No amount of careful ordering removes that, so both halves are designed for.

## The lost response

```text
request reaches Razorpay   ->   Razorpay creates the order   ->   the answer never arrives
```

From this side that is indistinguishable from the request never arriving. Creating again under a
fresh identity would produce a second order for one payment, which is how a customer ends up
looking at a checkout for an order the merchant is not tracking.

The receipt is what makes it recoverable. It is derived from the merchant and the attempt, so it
can be recomputed from scratch at any time by anybody holding the attempt, and Razorpay treats
it as unique on the account. So the recovery is a question rather than a guess: is there already
an order carrying this receipt.

Recovery runs for every failure class, not only for a duplicate receipt refusal. An ambiguous
gateway may have created the order. A refusal may be a duplicate receipt from a create that did
get through. Even an unreadable response may be a 200 describing an order that exists. All three
are answered by asking. The original error is re-raised when the answer is no, so a genuine
validation failure is never masked by the recovery attempt.

Deliberately not answered by parsing the refusal text. Razorpay's duplicate receipt error is
prose inside a `BAD_REQUEST_ERROR`, and building recovery on a substring match would make it
break the day the wording changes. The question this asks is true or false regardless of how the
refusal was worded.

## The order that is not ours

An order recovered by receipt, or created and read back, is still just a document a remote
system handed over. Before Standard Checkout is opened against it, three things have to be true:
the amount is what was admitted, the currency is what was admitted, and the receipt is the one
this attempt derives. Any mismatch fails closed, because the alternative is presenting a
customer with a checkout that collects the wrong amount and then honouring it.

Fail closed here means raise. There is no branch that continues with a mismatched order, no
tolerance, and no comparison that treats a missing field as equal.
"""

from dataclasses import dataclass

from agentrank_api.razorpay.client import RazorpayClient
from agentrank_api.razorpay.entities import NewOrder, RazorpayOrder
from agentrank_api.razorpay.errors import RazorpayError, RazorpayOrderMismatchError


@dataclass(frozen=True, slots=True)
class ObtainedOrder:
    """The order this attempt is settled through, and how it was arrived at.

    `recovered` is separate from the order because the two answer different questions and only
    one of them is visible afterwards. An order that was created and an order that was found
    look identical in the database, and "the create response was lost and we recovered" is worth
    recording in the audit trail and worth asserting in a test.
    """

    order: RazorpayOrder
    recovered: bool


async def obtain_order(client: RazorpayClient, request: NewOrder) -> ObtainedOrder:
    """Create the order for this receipt, or find the one that already exists.

    Never creates twice. There is exactly one `create_order` call in this function and no loop
    around it, which is the property worth being able to see at a glance.

    A failure of any class is followed by one lookup by receipt. If Razorpay has an order under
    it, that order is the answer and the failure is discarded, because the failure was about a
    request and the order is a fact. If Razorpay has none, the original failure is re-raised
    unchanged, so an amount Razorpay rejected still reaches the caller as a refusal rather than
    as a mysterious absence.

    A lookup that itself fails propagates. Nothing may be concluded when the gateway will not
    answer, and the original failure remains attached as the cause.
    """
    try:
        created = await client.create_order(request)
    except RazorpayError:
        recovered = await client.find_order_by_receipt(request.receipt)
        if recovered is None:
            raise
        return ObtainedOrder(order=recovered, recovered=True)
    return ObtainedOrder(order=created, recovered=False)


def require_matching_order(order: RazorpayOrder, expected: NewOrder) -> None:
    """Refuse an order that is not the one this payment attempt authorized.

    Three comparisons and no tolerance on any of them. The amount and the currency are what a
    customer will be charged, and the receipt is what says this order belongs to this attempt
    rather than to a different one that happened to come back from a listing.

    Raises rather than returning a decision, because there is no caller that would do anything
    other than stop, and a boolean would eventually be checked in one place and not another.
    """
    if order.receipt != expected.receipt:
        raise RazorpayOrderMismatchError(
            order.id, "receipt", str(expected.receipt), str(order.receipt)
        )
    if order.amount_minor != expected.amount_minor:
        raise RazorpayOrderMismatchError(
            order.id, "amount_minor", str(expected.amount_minor), str(order.amount_minor)
        )
    if order.currency != expected.currency:
        raise RazorpayOrderMismatchError(order.id, "currency", expected.currency, order.currency)
