"""The order execution sensitive rows are locked in, stated once and in one place.

Five classes of row carry mutable state that an execution or a payment depends on, and more
than one operation needs more than one of them at a time. An operation that took them in an
order of its own would eventually meet an operation that took them in another, and PostgreSQL
would resolve that by aborting one of them as a deadlock.

The order is:

```text
SpendingMandate          the authorization
        |
        v
CheckoutSession          the quote written against it
        |
        v
Variant rows             ascending identifier, the stock the quote names
        |
        v
InventoryReservation     the claim on that stock
        |
        v
PaymentAttempt           the payment for it
        |
        v
RazorpayCheckout         the interactive provider binding for that payment
```

Every operation that needs more than one class takes them in this order and never in
another. An operation that needs one class takes only that one: revoking a mandate locks the
mandate and nothing below it, cancelling a checkout locks the checkout and nothing above it,
and neither reverses the order.

The order is read downwards, from the authorization to the thing it authorizes to the stock
that thing names to the claim on that stock to the money to the provider binding for it. That
is also the order the facts depend on each other in. A quote is meaningless without its
mandate, stock is held for a quote, a payment is made for a hold, and a provider order is
created for a payment.

The binding is last because it depends on everything above it and nothing depends on it. An
operation that touches it touches it at the end, which is also where the external call it
guards happens.

The variant rows come before the reservation rather than after it, which reads backwards
until it does not. Locking the shelf is what makes availability stand still, and availability
is what a reservation is a decision about, so the shelf has to be held before the claim on it
is written or changed. Preparation established that order before payments existed and every
later operation adopted it rather than the other way round.

Preparation and payment admission are the operations that need the most classes at once, so
they are the ones the order exists for and the ones a test asserts the actual sequence of.
"""

from collections.abc import Sequence

MANDATE = "spending_mandate"
CHECKOUT = "checkout_session"
VARIANT = "variant"
RESERVATION = "inventory_reservation"
PAYMENT_ATTEMPT = "payment_attempt"
RAZORPAY_CHECKOUT = "razorpay_checkout"

# The table names in lock order. Here rather than restated in a test, so the rule and the
# thing asserting it cannot drift apart.
LOCK_ORDER: tuple[str, ...] = (
    MANDATE,
    CHECKOUT,
    VARIANT,
    RESERVATION,
    PAYMENT_ATTEMPT,
    RAZORPAY_CHECKOUT,
)


def respects_lock_order(tables: Sequence[str]) -> bool:
    """Whether a sequence of locks was taken in the documented order.

    A subsequence check rather than an equality check, because no operation needs every
    class. What matters is that nothing is taken out of order, not that everything is taken.

    Only the first acquisition of each class is judged. Taking a lock a transaction already
    holds never waits, so it cannot participate in a deadlock cycle and cannot be a reversal
    however late it appears. Several operations here take one class twice deliberately, once
    to establish that they will not block again and once inside the algorithm that needs it,
    and that redundancy is the price of leaving each algorithm self contained.
    """
    seen: set[str] = set()
    position = -1
    for table in tables:
        if table in seen or table not in LOCK_ORDER:
            continue
        seen.add(table)
        index = LOCK_ORDER.index(table)
        if index <= position:
            return False
        position = index
    return True
