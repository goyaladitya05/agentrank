"""The second authorization question, at the boundaries that matter.

Pure domain tests. The objects are built in memory and never persisted, because the
decision is a function of their fields and nothing else. There is no clock here at all,
unlike the financial gate: whether a charger is black does not depend on what time it is.

The cases that carry the most weight are the ones where the answer has to be a denial for a
reason other than "wrong value": a missing attribute, and two values of different kinds.
Both of those are places where a permissive implementation would quietly approve a purchase
nobody authorized.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from agentrank_api.checkout.authorization import (
    CheckoutAuthorizationViolation,
    authorize_checkout,
)
from agentrank_api.checkout.intent_authorization import (
    IntentViolationCode,
    evaluate_intent_constraints,
)
from agentrank_api.checkout.models import CheckoutLine, CheckoutSession, CheckoutStatus
from agentrank_api.constraints.models import IntentConstraint, IntentConstraintSet
from agentrank_api.constraints.rules import ConstraintOperator, IntentConstraintSpec
from agentrank_api.mandates.models import MandateStatus, SpendingMandate

MERCHANT = uuid.uuid7()
MANDATE = uuid.uuid7()
NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)

C = IntentViolationCode
Op = ConstraintOperator

BLACK = IntentConstraintSpec.required_attribute("color", Op.EQ, "black")
HUNDRED_WATTS = IntentConstraintSpec.required_attribute("wattage", Op.GTE, 100)
CHARGERS = IntentConstraintSpec.allowed_categories(("chargers",))


def a_set(*specs: IntentConstraintSpec, **overrides: object) -> IntentConstraintSet:
    fields: dict[str, object] = {
        "id": uuid.uuid7(),
        "merchant_id": MERCHANT,
        "mandate_id": MANDATE,
    }
    constraint_set = IntentConstraintSet(**(fields | overrides))
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


def a_line(category: str | None = "chargers", **attributes: Any) -> CheckoutLine:
    return CheckoutLine(
        id=uuid.uuid7(),
        variant_id=uuid.uuid7(),
        merchant_id=MERCHANT,
        quantity=1,
        unit_price_amount_minor=499900,
        currency="INR",
        product_category=category,
        variant_attributes=dict(attributes),
    )


def a_checkout(*lines: CheckoutLine, **overrides: object) -> CheckoutSession:
    fields: dict[str, object] = {
        "id": uuid.uuid7(),
        "merchant_id": MERCHANT,
        "mandate_id": MANDATE,
        "currency": "INR",
        "subtotal_amount_minor": 499900,
        "shipping_amount_minor": 0,
        "discount_amount_minor": 0,
        "total_amount_minor": 499900,
        "status": CheckoutStatus.OPEN,
        "expires_at": NOW + HOUR,
    }
    checkout = CheckoutSession(**(fields | overrides))
    checkout.lines = list(lines) or [a_line(color="black")]
    return checkout


def codes(*lines: CheckoutLine, specs: tuple[IntentConstraintSpec, ...]) -> list[C]:
    decision = evaluate_intent_constraints(a_checkout(*lines), a_set(*specs))
    return [violation.code for violation in decision.violations]


def test_an_allowed_category_passes() -> None:
    decision = evaluate_intent_constraints(a_checkout(a_line("chargers")), a_set(CHARGERS))

    assert decision.satisfied
    assert decision.violations == ()


def test_a_category_outside_the_allowed_set_fails() -> None:
    decision = evaluate_intent_constraints(a_checkout(a_line("headphones")), a_set(CHARGERS))

    assert not decision.satisfied
    violation = decision.violations[0]
    assert violation.code is C.CATEGORY_NOT_ALLOWED
    assert violation.expected == ["chargers"]
    assert violation.actual == "headphones"


def test_a_product_with_no_category_cannot_be_allowed() -> None:
    """Machine unreadable merchant data is a failure, not a reason to guess."""
    decision = evaluate_intent_constraints(a_checkout(a_line(None)), a_set(CHARGERS))

    assert [violation.code for violation in decision.violations] == [C.CATEGORY_NOT_ALLOWED]
    assert decision.violations[0].actual is None


def test_several_allowed_categories_mean_any_one_of_them() -> None:
    both = IntentConstraintSpec.allowed_categories(("chargers", "cables"))
    decision = evaluate_intent_constraints(
        a_checkout(a_line("chargers"), a_line("cables")), a_set(both)
    )

    assert decision.satisfied


def test_a_required_attribute_that_matches_exactly_passes() -> None:
    decision = evaluate_intent_constraints(a_checkout(a_line(color="black")), a_set(BLACK))

    assert decision.satisfied


def test_a_required_attribute_with_the_wrong_value_fails() -> None:
    """The whole point of the phase, in one assertion."""
    decision = evaluate_intent_constraints(a_checkout(a_line(color="blue")), a_set(BLACK))

    violation = decision.violations[0]
    assert violation.code is C.REQUIRED_ATTRIBUTE_MISMATCH
    assert violation.attribute == "color"
    assert violation.operator is Op.EQ
    assert violation.expected == "black"
    assert violation.actual == "blue"


def test_a_missing_required_attribute_fails() -> None:
    """Absence is not falsehood, and nothing reads the description to fill it in."""
    decision = evaluate_intent_constraints(a_checkout(a_line(color="black")), a_set(HUNDRED_WATTS))

    violation = decision.violations[0]
    assert violation.code is C.REQUIRED_ATTRIBUTE_MISSING
    assert violation.attribute == "wattage"
    assert violation.expected == 100
    assert violation.actual is None


def test_capitalisation_is_normalized_on_both_sides() -> None:
    decision = evaluate_intent_constraints(a_checkout(a_line(Color=" Black ")), a_set(BLACK))

    assert decision.satisfied


def test_two_keys_that_differ_only_in_case_are_ambiguous_and_fail() -> None:
    """Picking one of them would be a guess, and a guess is not an authorization."""
    # Two distinct keys, neither matching exactly, both normalizing to the same name.
    line = a_line(COLOR="black", Color="blue")

    decision = evaluate_intent_constraints(a_checkout(line), a_set(BLACK))

    assert [violation.code for violation in decision.violations] == [C.REQUIRED_ATTRIBUTE_MISSING]


def test_a_numeric_floor_passes_at_the_boundary_and_above() -> None:
    assert codes(a_line(wattage=100), specs=(HUNDRED_WATTS,)) == []
    assert codes(a_line(wattage=140), specs=(HUNDRED_WATTS,)) == []


def test_a_numeric_floor_fails_below_it() -> None:
    decision = evaluate_intent_constraints(a_checkout(a_line(wattage=99)), a_set(HUNDRED_WATTS))

    assert [violation.code for violation in decision.violations] == [C.REQUIRED_ATTRIBUTE_MISMATCH]
    assert decision.violations[0].actual == 99
    # The 65 watt travel charger against a 100 watt requirement, which is the case a
    # buyer agent would otherwise be talked into by a helpful product description.
    assert codes(a_line(wattage=65), specs=(HUNDRED_WATTS,)) == [C.REQUIRED_ATTRIBUTE_MISMATCH]


@pytest.mark.parametrize(
    ("spec", "attributes"),
    [
        pytest.param(HUNDRED_WATTS, {"wattage": "100"}, id="text where a number is required"),
        pytest.param(HUNDRED_WATTS, {"wattage": True}, id="a boolean is not a number"),
        pytest.param(BLACK, {"color": 3}, id="a number where text is required"),
        pytest.param(
            IntentConstraintSpec.required_attribute("refurbished", Op.EQ, True),
            {"refurbished": "true"},
            id="text where a boolean is required",
        ),
        pytest.param(
            IntentConstraintSpec.required_attribute("refurbished", Op.EQ, False),
            {"refurbished": 0},
            id="zero is not false",
        ),
        pytest.param(BLACK, {"color": ["black"]}, id="a list is not a value"),
    ],
)
def test_values_of_different_kinds_are_never_compared(
    spec: IntentConstraintSpec, attributes: dict[str, Any]
) -> None:
    """`"100" >= 65` and `true == "true"` are the two comparisons this must never make."""
    decision = evaluate_intent_constraints(a_checkout(a_line(**attributes)), a_set(spec))

    assert [violation.code for violation in decision.violations] == [C.ATTRIBUTE_TYPE_MISMATCH]


def test_a_boolean_constraint_compares_booleans() -> None:
    refurbished = IntentConstraintSpec.required_attribute("refurbished", Op.NE, True)

    assert codes(a_line(refurbished=False), specs=(refurbished,)) == []
    assert codes(a_line(refurbished=True), specs=(refurbished,)) == [C.REQUIRED_ATTRIBUTE_MISMATCH]


def test_membership_accepts_any_listed_value() -> None:
    either = IntentConstraintSpec.required_attribute("color", Op.IN, ("black", "graphite"))

    assert codes(a_line(color="graphite"), specs=(either,)) == []
    assert codes(a_line(color="silver"), specs=(either,)) == [C.REQUIRED_ATTRIBUTE_MISMATCH]


def test_a_range_is_two_constraints_and_both_must_hold() -> None:
    ceiling = IntentConstraintSpec.required_attribute("wattage", Op.LTE, 140)

    assert codes(a_line(wattage=100), specs=(HUNDRED_WATTS, ceiling)) == []
    assert codes(a_line(wattage=240), specs=(HUNDRED_WATTS, ceiling)) == [
        C.REQUIRED_ATTRIBUTE_MISMATCH
    ]


def test_every_constraint_must_pass() -> None:
    decision = evaluate_intent_constraints(
        a_checkout(a_line("headphones", color="blue")), a_set(CHARGERS, BLACK)
    )

    assert [violation.code for violation in decision.violations] == [
        C.CATEGORY_NOT_ALLOWED,
        C.REQUIRED_ATTRIBUTE_MISMATCH,
    ]


def test_every_line_is_checked_not_only_the_first() -> None:
    """A charger plus a pair of headphones is not a purchase of chargers."""
    charger = a_line("chargers", color="black")
    headphones = a_line("headphones", color="black")

    decision = evaluate_intent_constraints(a_checkout(charger, headphones), a_set(CHARGERS))

    assert [violation.code for violation in decision.violations] == [C.CATEGORY_NOT_ALLOWED]
    assert decision.violations[0].line_id == headphones.id
    assert decision.violations[0].variant_id == headphones.variant_id


def test_one_bad_line_out_of_three_denies_the_whole_checkout() -> None:
    decision = evaluate_intent_constraints(
        a_checkout(a_line(color="black"), a_line(color="blue"), a_line(color="black")),
        a_set(BLACK),
    )

    assert not decision.satisfied
    assert len(decision.violations) == 1


def test_violations_are_ordered_by_constraint_then_by_line() -> None:
    first = a_line("headphones", color="blue")
    second = a_line("headphones", color="blue")

    decision = evaluate_intent_constraints(a_checkout(first, second), a_set(CHARGERS, BLACK))

    assert [(v.code, v.line_id) for v in decision.violations] == [
        (C.CATEGORY_NOT_ALLOWED, first.id),
        (C.CATEGORY_NOT_ALLOWED, second.id),
        (C.REQUIRED_ATTRIBUTE_MISMATCH, first.id),
        (C.REQUIRED_ATTRIBUTE_MISMATCH, second.id),
    ]


def test_a_violation_names_the_constraint_that_was_broken() -> None:
    constraint_set = a_set(CHARGERS, BLACK)
    decision = evaluate_intent_constraints(a_checkout(a_line(color="blue")), constraint_set)

    assert decision.violations[0].constraint_id == constraint_set.constraints[1].id


def test_constraints_authorized_for_another_mandate_are_not_evaluated() -> None:
    """A rule that was not authorized for this checkout is not a rule it failed."""
    decision = evaluate_intent_constraints(
        a_checkout(a_line(color="blue")), a_set(BLACK, mandate_id=uuid.uuid7())
    )

    assert [violation.code for violation in decision.violations] == [C.CONSTRAINTS_MANDATE_MISMATCH]


def test_constraints_belonging_to_another_merchant_are_not_evaluated() -> None:
    other = uuid.uuid7()
    decision = evaluate_intent_constraints(
        a_checkout(a_line(color="black")),
        a_set(BLACK, merchant_id=other, mandate_id=uuid.uuid7()),
    )

    assert [violation.code for violation in decision.violations] == [
        C.CONSTRAINTS_MERCHANT_MISMATCH,
        C.CONSTRAINTS_MANDATE_MISMATCH,
    ]


def test_a_checkout_with_no_lines_satisfies_nothing() -> None:
    checkout = a_checkout()
    checkout.lines = []

    decision = evaluate_intent_constraints(checkout, a_set(BLACK))

    assert [violation.code for violation in decision.violations] == [C.CHECKOUT_HAS_NO_LINES]


def test_an_empty_attribute_snapshot_fails_every_attribute_constraint() -> None:
    """The shape a checkout quoted before semantic snapshots existed arrives in."""
    line = a_line()
    line.variant_attributes = {}

    decision = evaluate_intent_constraints(a_checkout(line), a_set(BLACK))

    assert [violation.code for violation in decision.violations] == [C.REQUIRED_ATTRIBUTE_MISSING]


def a_mandate(**overrides: object) -> SpendingMandate:
    fields: dict[str, object] = {
        "id": MANDATE,
        "merchant_id": MERCHANT,
        "max_total_amount_minor": 500000,
        "currency": "INR",
        "max_quantity": 1,
        "valid_from": NOW,
        "valid_until": NOW + HOUR,
        "status": MandateStatus.ACTIVE,
    }
    return SpendingMandate(**(fields | overrides))


def test_a_checkout_can_be_affordable_and_still_be_the_wrong_thing() -> None:
    """The exact safety distinction this phase exists to make.

    A blue charger at 499900 INR sits under a 500000 INR ceiling. The money is authorized.
    The purchase is not what the buyer asked for, and only the second gate can say so.
    """
    checkout = a_checkout(a_line(color="blue"))

    financial = authorize_checkout(checkout, a_mandate(), at=NOW)
    intent = evaluate_intent_constraints(checkout, a_set(BLACK))

    assert financial.allowed
    assert not intent.satisfied
    violation = intent.violations[0]
    assert violation.code is C.REQUIRED_ATTRIBUTE_MISMATCH
    assert (violation.attribute, violation.expected, violation.actual) == (
        "color",
        "black",
        "blue",
    )


def test_a_checkout_can_be_exactly_right_and_still_be_unaffordable() -> None:
    """The other direction, which is what proves the two gates are genuinely independent."""
    checkout = a_checkout(a_line(color="black"), total_amount_minor=500001)

    financial = authorize_checkout(checkout, a_mandate(), at=NOW)
    intent = evaluate_intent_constraints(checkout, a_set(BLACK, CHARGERS))

    assert not financial.allowed
    assert financial.violations == (CheckoutAuthorizationViolation.MAX_TOTAL_EXCEEDED,)
    assert intent.satisfied


def test_a_black_charger_within_the_ceiling_passes_both_gates() -> None:
    checkout = a_checkout(a_line("chargers", color="black", wattage=100))

    financial = authorize_checkout(checkout, a_mandate(), at=NOW)
    intent = evaluate_intent_constraints(checkout, a_set(BLACK, CHARGERS, HUNDRED_WATTS))

    assert financial.allowed
    assert intent.satisfied
