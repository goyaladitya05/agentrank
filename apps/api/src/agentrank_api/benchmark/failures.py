"""Why a benchmark mission did not produce the outcome its ground truth called for.

The vocabulary, not the logic. What decides which of these applies is the evaluator; this
module says what each one means and in what order they take precedence, and it lives on its own
because the mission run table stores one of these values and a check constraint has to list
them.

Two rules shaped the set.

Every code here is reachable against the commerce foundation that actually exists. Codes for
things this system cannot yet represent are absent rather than defined and never raised: there
is no `POLICY_UNRESOLVED`, because no authoritative policy representation exists; no
`DELIVERY_UNKNOWN`, because no authoritative delivery representation exists; and no
`PRICE_UNKNOWN` or `INVENTORY_UNKNOWN`, because a variant's price and stock are both `NOT NULL`
columns and cannot be unknown. A taxonomy that names failures the benchmark cannot observe would
report zero for them forever and read as evidence that merchants do not have those problems.

And there is no generic catch all. `AGENT_REASONING_ERROR` is close to one and is deliberately
not: it means the observed result contradicts itself or stops without an outcome, which is a
specific, checkable condition rather than a bucket for anything unclassified.

Failure precedence is explicit rather than incidental. `FAILURE_PRECEDENCE` is a written out
tuple, so the primary reason for a mission that got several things wrong never depends on the
order a set happened to iterate in. It follows the order a purchase is actually made: what could
not be reached at all, then what was chosen, then whether the choice was allowed, then whether it
could be quoted, authorized and paid for.
"""

from collections.abc import Set as AbstractSet
from enum import StrEnum


class FailureReason(StrEnum):
    """One machine readable reason a mission did not reach its expected outcome.

    A reason is not a status. `FAILED` says the mission did not go as its ground truth called
    for; the reason says what went wrong, and the two are separate columns because a scoring
    layer and a diagnostics layer want different halves of that. See docs/benchmark.md.
    """

    MERCHANT_API_ERROR = "MERCHANT_API_ERROR"
    """A merchant surface returned an error rather than an answer or a business refusal.

    A fact about the merchant, not about the harness, which is why it is a failure reason and
    not the `ERRORED` status. A 500 from a catalog endpoint is a commerce readiness finding.
    """

    WRONG_MERCHANT = "WRONG_MERCHANT"
    """The executor transacted with a merchant other than the one under benchmark."""

    AGENT_REASONING_ERROR = "AGENT_REASONING_ERROR"
    """The observed result contradicts itself, or stops with no outcome and no reason.

    Claiming a payment with no selection behind it, or abstaining and purchasing at once, or
    selecting a variant and then neither buying nor declining nor failing. Specific and
    checkable rather than a bucket for anything that could not be classified.
    """

    DISCOVERY_FAILURE = "DISCOVERY_FAILURE"
    """No purchasable candidate was identified, on a mission where one exists.

    Covers both shapes of the same finding: the executor found nothing at all, and the executor
    found things and could not tell that any of them satisfied the buyer. They are one reason
    because distinguishing them would mean trusting the executor's account of its own reasoning,
    and this benchmark marks what happened rather than what an agent said about it.
    """

    INVALID_VARIANT = "INVALID_VARIANT"
    """The selection is not something this merchant sells.

    A variant the catalog does not contain, or one it no longer offers. Different from having no
    stock: this says the merchant does not sell the thing, not that it has run out of it.
    """

    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    """The selection is priced in a currency the buyer's budget does not authorize.

    When this holds, the amount comparison is not made at all. Comparing 8999 EUR against a
    ceiling of 500000 INR is not a stricter check, it is a meaningless one, and reporting
    `BUDGET_EXCEEDED` from it would be reporting a fact nobody established.
    """

    CATEGORY_MISSING = "CATEGORY_MISSING"
    """The mission required a category and the merchant does not state one for the product.

    Machine unreadable merchant data, which is the thing this benchmark exists to measure. It is
    not the same as being in the wrong category, and folding the two together would hide the
    difference between a merchant with bad data and a merchant with the wrong product.
    """

    ATTRIBUTE_MISSING = "ATTRIBUTE_MISSING"
    """The mission required an attribute and the merchant's data does not carry it.

    Absence, never falsehood and never zero. Nothing infers a missing attribute from a product
    description, because inferring it would be inventing the merchant's answer.
    """

    ATTRIBUTE_UNREADABLE = "ATTRIBUTE_UNREADABLE"
    """The attribute is present in a form that cannot be compared with what was required.

    The merchant published `"100W"` where a number was needed, or `"yes"` where a boolean was.
    Its own reason rather than a mismatch, because the merchant did answer and the answer cannot
    be read, which is a different repair from publishing the wrong value.
    """

    CONSTRAINT_VIOLATION = "CONSTRAINT_VIOLATION"
    """A stated hard constraint was violated by a value the merchant did publish.

    The buyer required black and the selection is blue; the buyer allowed chargers and the
    selection is a cable; the buyer permitted at most two units and the purchase covers three.
    """

    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    """The purchase costs more than the buyer's stated ceiling, in the buyer's own currency."""

    QUANTITY_MISMATCH = "QUANTITY_MISMATCH"
    """The purchase covers a different number of units than the mission asked for.

    Distinct from `CONSTRAINT_VIOLATION`, which is about exceeding a stated ceiling. Buying one
    unit when two were wanted breaches no authorization and is still not the task.
    """

    INVENTORY_UNAVAILABLE = "INVENTORY_UNAVAILABLE"
    """The merchant sells the thing and could not hold enough of it.

    Out of stock, or not enough units left once other holds are counted. A fact about now.
    """

    CHECKOUT_CREATION_FAILED = "CHECKOUT_CREATION_FAILED"
    """The merchant refused to produce a quote, for a reason that is not stock."""

    MANDATE_DENIED = "MANDATE_DENIED"
    """The authorization layer refused the purchase.

    Deliberately not described as a system failure. When the attempt it refused was outside what
    the buyer authorized, this is the safety layer doing its job, and the metrics count it that
    way. It is a failure of the mission either way, because no purchase happened.
    """

    PAYMENT_FAILED = "PAYMENT_FAILED"
    """The provider definitively declined, and no money moved."""

    PAYMENT_UNRESOLVED = "PAYMENT_UNRESOLVED"
    """The payment reached no definitive outcome.

    Admitted and never dispatched, dispatched and unanswered, or answered ambiguously. Not
    `PAYMENT_FAILED`, because the payment kernel is built on never calling an unresolved payment
    a failed one, and a benchmark that flattened the two would be reporting a decline the
    provider never gave.
    """

    UNEXPECTED_PURCHASE = "UNEXPECTED_PURCHASE"
    """A purchase completed on a mission where nothing acceptable was for sale.

    Last in precedence on purpose. When the purchase also broke a stated requirement, that
    specific breach is the more useful primary reason and this is reported beside it. This
    stands alone only when a compliant purchase completed on a mission whose ground truth said
    none was possible, which means the ground truth was wrong and is worth seeing as such.
    """


# Written out rather than derived from declaration order, so that reordering the enum cannot
# silently change how a mission with several problems is classified. A test asserts this is a
# permutation of `FailureReason`, so a new reason cannot be added without placing it.
#
# The order is the order a purchase is actually made. What stopped the executor before it could
# choose anything, then what it chose, then whether that choice was allowed, then what happened
# when it tried to buy it.
FAILURE_PRECEDENCE: tuple[FailureReason, ...] = (
    FailureReason.MERCHANT_API_ERROR,
    FailureReason.WRONG_MERCHANT,
    FailureReason.AGENT_REASONING_ERROR,
    FailureReason.DISCOVERY_FAILURE,
    FailureReason.INVALID_VARIANT,
    FailureReason.CURRENCY_MISMATCH,
    FailureReason.CATEGORY_MISSING,
    FailureReason.ATTRIBUTE_MISSING,
    FailureReason.ATTRIBUTE_UNREADABLE,
    FailureReason.CONSTRAINT_VIOLATION,
    FailureReason.BUDGET_EXCEEDED,
    FailureReason.QUANTITY_MISMATCH,
    FailureReason.INVENTORY_UNAVAILABLE,
    FailureReason.CHECKOUT_CREATION_FAILED,
    FailureReason.MANDATE_DENIED,
    FailureReason.PAYMENT_FAILED,
    FailureReason.PAYMENT_UNRESOLVED,
    FailureReason.UNEXPECTED_PURCHASE,
)

_RANK = {reason: rank for rank, reason in enumerate(FAILURE_PRECEDENCE)}

# The reasons that describe the purchase itself rather than the process of trying to make one.
# An executor that carried a selection matching none of these past selection was trying to buy
# something the buyer did not authorize, which is what makes an attempt unsafe. See
# docs/benchmark.md.
UNSAFE_SELECTION_REASONS = frozenset(
    {
        FailureReason.WRONG_MERCHANT,
        FailureReason.INVALID_VARIANT,
        FailureReason.CURRENCY_MISMATCH,
        FailureReason.CATEGORY_MISSING,
        FailureReason.ATTRIBUTE_MISSING,
        FailureReason.ATTRIBUTE_UNREADABLE,
        FailureReason.CONSTRAINT_VIOLATION,
        FailureReason.BUDGET_EXCEEDED,
    }
)


def in_precedence(reasons: AbstractSet[FailureReason]) -> tuple[FailureReason, ...]:
    """The reasons found, ordered so that the primary one is first.

    A set goes in and a tuple comes out, which is the point. The evaluator collects reasons
    however it finds them, and the order they are reported in is decided here and nowhere else.
    """
    return tuple(sorted(reasons, key=lambda reason: _RANK[reason]))
