"""Is this checkout financially authorized by this spending mandate right now.

That is the whole question, and the name of the answer says exactly which question was
asked. `CheckoutAuthorizationDecision` is not a general policy verdict: it compares a quote
against one mandate's merchant, currency, amount ceiling, quantity ceiling and validity
window, and against the quote's own status and expiry. Nothing else.

What it deliberately does not answer is whether the checkout satisfies the buyer's
`BuyerIntent`. "Black only" and "no refurbished units" are hard constraints that live on an
intent, are not persisted, and are not read here. A checkout can be financially authorized
and still be the wrong thing to buy. Semantic intent enforcement is a separate deterministic
gate that has to exist before any payment does. See docs/security.md.

Everything here is pure. It reads no clock, touches no database, calls no external service
and consults no model, so the same inputs always produce the same decision. The evaluation
instant is an argument, because a function that reads the clock cannot be tested without
controlling the clock, and an authorization decision should name the instant it was made
against.

Nothing here writes an audit event either. Evaluating a policy is a read, and an event per
read would turn the trail into a request log. The event worth recording is a refusal at the
moment an action is actually attempted, which belongs with payment execution.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from agentrank_api.checkout.models import CheckoutSession, CheckoutStatus
from agentrank_api.mandates.models import SpendingMandate
from agentrank_api.mandates.validation import MandateViolation, validate_mandate


class CheckoutAuthorizationViolation(StrEnum):
    """Why a checkout is not authorized.

    Machine readable identifiers, not prose. A buyer agent has to be able to tell "this
    costs more than you may spend" from "this authorization has lapsed" without reading
    English, because the two call for completely different next moves.

    The first three carry the same values as `MandateViolation`, and are translated from
    it rather than re-derived. One rule decides whether a mandate is usable, and this
    reports its answer instead of drifting from it.
    """

    MANDATE_NOT_ACTIVE = "MANDATE_NOT_ACTIVE"
    MANDATE_NOT_YET_VALID = "MANDATE_NOT_YET_VALID"
    MANDATE_EXPIRED = "MANDATE_EXPIRED"
    CHECKOUT_MERCHANT_MISMATCH = "CHECKOUT_MERCHANT_MISMATCH"
    CHECKOUT_MANDATE_MISMATCH = "CHECKOUT_MANDATE_MISMATCH"
    CHECKOUT_NOT_OPEN = "CHECKOUT_NOT_OPEN"
    CHECKOUT_EXPIRED = "CHECKOUT_EXPIRED"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    MAX_TOTAL_EXCEEDED = "MAX_TOTAL_EXCEEDED"
    MAX_QUANTITY_EXCEEDED = "MAX_QUANTITY_EXCEEDED"


# Written out rather than converted by value, so that adding a mandate violation is a
# compile time question here rather than a KeyError in production. A test asserts the map
# covers every member.
FROM_MANDATE_VIOLATION = {
    MandateViolation.MANDATE_NOT_ACTIVE: CheckoutAuthorizationViolation.MANDATE_NOT_ACTIVE,
    MandateViolation.MANDATE_NOT_YET_VALID: CheckoutAuthorizationViolation.MANDATE_NOT_YET_VALID,
    MandateViolation.MANDATE_EXPIRED: CheckoutAuthorizationViolation.MANDATE_EXPIRED,
}


@dataclass(frozen=True, slots=True)
class CheckoutAuthorizationDecision:
    """The outcome of checking one checkout against one mandate at one instant.

    `allowed` is derived rather than stored, so a decision carrying violations cannot also
    claim to permit anything. Violations are ordered, and the order is fixed, so two runs
    of the same check produce the same decision.
    """

    violations: tuple[CheckoutAuthorizationViolation, ...] = ()

    @property
    def allowed(self) -> bool:
        return not self.violations


def authorize_checkout(
    checkout: CheckoutSession, mandate: SpendingMandate, *, at: datetime
) -> CheckoutAuthorizationDecision:
    """Answer whether this mandate authorizes this checkout at `at`.

    Every relevant reason is reported rather than only the first one found, because a
    denied checkout is an ordinary outcome and a caller usually wants the whole picture at
    once. The order is fixed: what the mandate is, then how the two relate, then what the
    checkout is, then the comparisons.

    The checkout is read as the snapshot it is. No catalog row is consulted, so a price
    change after the quote was written cannot alter what this decision was made against.

    The amount comparison is skipped when the currencies disagree. Comparing 4999 EUR
    against a ceiling of 500000 INR is not a stricter check, it is a meaningless one, and
    reporting MAX_TOTAL_EXCEEDED from it would be reporting a fact nobody established. The
    quantity comparison is unaffected, because a count has no currency.
    """
    if at.tzinfo is None:
        raise ValueError("evaluation time must be timezone aware")

    violations: list[CheckoutAuthorizationViolation] = [
        FROM_MANDATE_VIOLATION[violation]
        for violation in validate_mandate(mandate, at=at).violations
    ]

    if checkout.merchant_id != mandate.merchant_id:
        violations.append(CheckoutAuthorizationViolation.CHECKOUT_MERCHANT_MISMATCH)
    if checkout.mandate_id != mandate.id:
        # Reachable only by pairing a checkout with a mandate it was not written against.
        # The database prevents the cross merchant case; this catches the rest.
        violations.append(CheckoutAuthorizationViolation.CHECKOUT_MANDATE_MISMATCH)

    if checkout.status is not CheckoutStatus.OPEN:
        violations.append(CheckoutAuthorizationViolation.CHECKOUT_NOT_OPEN)
    # Half open, in the same direction as a mandate's window: usable before expires_at,
    # not usable at it.
    if at >= checkout.expires_at:
        violations.append(CheckoutAuthorizationViolation.CHECKOUT_EXPIRED)

    if checkout.currency != mandate.currency:
        violations.append(CheckoutAuthorizationViolation.CURRENCY_MISMATCH)
    elif checkout.total_amount_minor > mandate.max_total_amount_minor:
        violations.append(CheckoutAuthorizationViolation.MAX_TOTAL_EXCEEDED)

    # Null max_quantity means this mandate places no limit on quantity. It does not mean
    # zero and it does not mean one.
    if mandate.max_quantity is not None and checkout_quantity(checkout) > mandate.max_quantity:
        violations.append(CheckoutAuthorizationViolation.MAX_QUANTITY_EXCEEDED)

    return CheckoutAuthorizationDecision(violations=tuple(violations))


def checkout_quantity(checkout: CheckoutSession) -> int:
    """How many units the quote covers.

    The sum of the line quantities, never the number of lines. Reading `lines` raises
    unless they were loaded, which is deliberate: a quantity computed from a collection
    that was never fetched would be a confident zero.
    """
    return sum(line.quantity for line in checkout.lines)
