"""The order execution sensitive rows are locked in, stated once and in one place.

Three classes of row carry mutable state that an execution preparation depends on, and
more than one operation needs more than one of them at a time. An operation that took them
in an order of its own would eventually meet an operation that took them in another, and
PostgreSQL would resolve that by aborting one of them as a deadlock.

The order is:

```text
SpendingMandate          the authorization
        |
        v
CheckoutSession          the quote written against it
        |
        v
Variant rows             ascending identifier, the stock the quote names
```

Every operation that needs more than one class takes them in this order and never in
another. An operation that needs one class takes only that one: revoking a mandate locks
the mandate and nothing below it, cancelling a checkout locks the checkout and nothing
above it, and neither reverses the order.

The order is read downwards, from the authorization to the thing it authorizes to the
stock that thing names, which is also the order the facts depend on each other in. A quote
is meaningless without its mandate, and stock is held for a quote.

Preparation is the only operation that needs all three, so it is the one the order exists
for and the one a test asserts the actual sequence of.
"""

MANDATE = "spending_mandate"
CHECKOUT = "checkout_session"
VARIANT = "variant"

# The table names in lock order. Here rather than restated in a test, so the rule and the
# thing asserting it cannot drift apart.
LOCK_ORDER: tuple[str, str, str] = (MANDATE, CHECKOUT, VARIANT)
