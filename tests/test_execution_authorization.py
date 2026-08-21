"""Both gates over one checkout, and what happens when half the authorization is absent.

Pure domain tests. The objects are built in memory and never persisted, because the
decision is a function of their fields and the instant it is asked about.

The case that carries the most weight is the missing constraint set. A mandate can exist
without one, and reading that as "nothing was required, so everything passes" is the single
most dangerous default this system could have. It has to deny, and it has to say why.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from agentrank_api.checkout.authorization import CheckoutAuthorizationViolation
from agentrank_api.checkout.execution_authorization import (
    ExecutionAuthorizationViolation,
    authorize_checkout_execution,
)
from agentrank_api.checkout.intent_authorization import IntentViolationCode
from agentrank_api.checkout.models import CheckoutLine, CheckoutSession, CheckoutStatus
from agentrank_api.constraints.models import IntentConstraint, IntentConstraintSet
from agentrank_api.constraints.rules import ConstraintOperator, IntentConstraintSpec
from agentrank_api.mandates.models import MandateStatus, SpendingMandate

MERCHANT = uuid.uuid7()
MANDATE = uuid.uuid7()
NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
PRICE = 499900

BLACK = IntentConstraintSpec.required_attribute("color", ConstraintOperator.EQ, "black")


def a_mandate(**overrides: object) -> SpendingMandate:
    fields: dict[str, object] = {
        "id": MANDATE,
        "merchant_id": MERCHANT,
        "max_total_amount_minor": PRICE,
        "currency": "INR",
        "max_quantity": None,
        "valid_from": NOW - HOUR,
        "valid_until": NOW + HOUR,
        "status": MandateStatus.ACTIVE,
        "revoked_at": None,
    }
    return SpendingMandate(**(fields | overrides))


def a_checkout(**overrides: object) -> CheckoutSession:
    fields: dict[str, object] = {
        "id": uuid.uuid7(),
        "merchant_id": MERCHANT,
        "mandate_id": MANDATE,
        "currency": "INR",
        "subtotal_amount_minor": PRICE,
        "shipping_amount_minor": 0,
        "discount_amount_minor": 0,
        "total_amount_minor": PRICE,
        "status": CheckoutStatus.OPEN,
        "expires_at": NOW + HOUR,
    }
    attributes: dict[str, Any] = {"color": "black"}
    checkout = CheckoutSession(**(fields | overrides))
    checkout.lines = [
        CheckoutLine(
            id=uuid.uuid7(),
            merchant_id=MERCHANT,
            variant_id=uuid.uuid7(),
            quantity=1,
            unit_price_amount_minor=checkout.subtotal_amount_minor,
            currency="INR",
            product_category="chargers",
            variant_attributes=attributes,
        )
    ]
    return checkout


def a_set(*specs: IntentConstraintSpec) -> IntentConstraintSet:
    constraint_set = IntentConstraintSet(id=uuid.uuid7(), merchant_id=MERCHANT, mandate_id=MANDATE)
    constraint_set.constraints = [
        IntentConstraint(
            id=uuid.uuid7(),
            constraint_set_id=constraint_set.id,
            merchant_id=MERCHANT,
            kind=spec.kind,
            attribute_key=spec.attribute_key,
            operator=spec.operator,
            value=spec.to_stored_value(),
        )
        for spec in specs
    ]
    return constraint_set


def test_both_gates_allowing_is_the_only_authorized_case() -> None:
    decision = authorize_checkout_execution(a_checkout(), a_mandate(), a_set(BLACK), at=NOW)

    assert decision.authorized
    assert decision.financial.allowed
    assert decision.intent is not None
    assert decision.intent.satisfied
    assert decision.violations == ()


def test_financially_allowed_and_semantically_denied_is_not_authorized() -> None:
    """The independence Phase 1D proved, now required to compose into a denial."""
    blue = a_checkout()
    blue.lines[0].variant_attributes = {"color": "blue"}

    decision = authorize_checkout_execution(blue, a_mandate(), a_set(BLACK), at=NOW)

    assert not decision.authorized
    assert decision.financial.allowed
    assert decision.intent is not None
    assert [violation.code for violation in decision.intent.violations] == [
        IntentViolationCode.REQUIRED_ATTRIBUTE_MISMATCH
    ]


def test_financially_denied_and_semantically_satisfied_is_not_authorized() -> None:
    decision = authorize_checkout_execution(
        a_checkout(), a_mandate(max_total_amount_minor=PRICE - 1), a_set(BLACK), at=NOW
    )

    assert not decision.authorized
    assert decision.financial.violations == (CheckoutAuthorizationViolation.MAX_TOTAL_EXCEEDED,)
    assert decision.intent is not None
    assert decision.intent.satisfied


def test_both_denying_reports_both_rather_than_the_first() -> None:
    """A caller fixing one problem has to be able to see the other."""
    blue = a_checkout()
    blue.lines[0].variant_attributes = {"color": "blue"}

    decision = authorize_checkout_execution(
        blue, a_mandate(max_total_amount_minor=PRICE - 1), a_set(BLACK), at=NOW
    )

    assert not decision.authorized
    assert decision.financial.violations == (CheckoutAuthorizationViolation.MAX_TOTAL_EXCEEDED,)
    assert decision.intent is not None
    assert not decision.intent.satisfied


def test_a_missing_constraint_set_denies_and_says_so() -> None:
    """Absence of a semantic authorization is not a passed one."""
    decision = authorize_checkout_execution(a_checkout(), a_mandate(), None, at=NOW)

    assert not decision.authorized
    assert decision.intent is None
    assert decision.violations == (ExecutionAuthorizationViolation.INTENT_CONSTRAINTS_MISSING,)
    # The financial answer is still made and still reported.
    assert decision.financial.allowed


def test_a_missing_constraint_set_denies_even_when_the_money_is_wrong_too() -> None:
    decision = authorize_checkout_execution(
        a_checkout(), a_mandate(max_total_amount_minor=PRICE - 1), None, at=NOW
    )

    assert not decision.authorized
    assert decision.violations == (ExecutionAuthorizationViolation.INTENT_CONSTRAINTS_MISSING,)
    assert decision.financial.violations == (CheckoutAuthorizationViolation.MAX_TOTAL_EXCEEDED,)


def test_a_constraint_set_holding_no_constraints_is_not_a_missing_one() -> None:
    """The distinction Phase 1D drew, preserved here.

    `IntentConstraintRepository.create` refuses to write an empty set, so this shape is not
    reachable through the application. If one ever existed it would mean a semantic
    authorization that requires nothing, which is satisfied, and that is a different fact
    from there being no authorization at all.
    """
    decision = authorize_checkout_execution(a_checkout(), a_mandate(), a_set(), at=NOW)

    assert decision.authorized
    assert decision.intent is not None
    assert decision.intent.satisfied
    assert decision.violations == ()


def test_a_cancelled_or_expired_checkout_is_not_authorized() -> None:
    cancelled = authorize_checkout_execution(
        a_checkout(status=CheckoutStatus.CANCELLED, cancelled_at=NOW),
        a_mandate(),
        a_set(BLACK),
        at=NOW,
    )
    assert not cancelled.authorized
    assert CheckoutAuthorizationViolation.CHECKOUT_NOT_OPEN in cancelled.financial.violations

    expired = authorize_checkout_execution(
        a_checkout(), a_mandate(), a_set(BLACK), at=NOW + 2 * HOUR
    )
    assert not expired.authorized
    assert CheckoutAuthorizationViolation.CHECKOUT_EXPIRED in expired.financial.violations


def test_a_revoked_or_expired_mandate_is_not_authorized() -> None:
    revoked = authorize_checkout_execution(
        a_checkout(),
        a_mandate(status=MandateStatus.REVOKED, revoked_at=NOW),
        a_set(BLACK),
        at=NOW,
    )
    assert not revoked.authorized
    assert CheckoutAuthorizationViolation.MANDATE_NOT_ACTIVE in revoked.financial.violations

    lapsed = authorize_checkout_execution(
        a_checkout(expires_at=NOW + 3 * HOUR), a_mandate(), a_set(BLACK), at=NOW + 2 * HOUR
    )
    assert not lapsed.authorized
    assert CheckoutAuthorizationViolation.MANDATE_EXPIRED in lapsed.financial.violations


def test_the_decision_is_a_function_of_the_instant_it_is_asked_about() -> None:
    """The same three rows answer differently as the clock moves, and nothing is cached."""
    checkout, mandate, constraints = a_checkout(), a_mandate(), a_set(BLACK)

    assert authorize_checkout_execution(checkout, mandate, constraints, at=NOW).authorized
    assert not authorize_checkout_execution(
        checkout, mandate, constraints, at=NOW + 2 * HOUR
    ).authorized
