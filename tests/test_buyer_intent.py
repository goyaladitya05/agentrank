"""Buyer intent is a typed structure, not prose.

What matters here is the distinction the type system is meant to hold: a hard constraint
is machine checkable, a preference is not, and a financial ceiling is never left as text.
"""

import json
import uuid

import pytest

from agentrank_api.constraints.rules import ConstraintOperator
from agentrank_api.mandates.intent import (
    AllowedCategory,
    BuyerIntent,
    MaxQuantity,
    MaxTotalAmount,
    Preference,
    RequiredAttribute,
)


def test_an_intent_serializes_to_a_json_object() -> None:
    merchant_id = uuid.uuid7()
    intent = BuyerIntent(
        merchant_id=merchant_id,
        description="One 100W USB-C charger for a laptop",
        hard_constraints=(
            MaxTotalAmount(amount_minor=500000, currency="INR"),
            MaxQuantity(quantity=1),
            RequiredAttribute(name="connector", value="usb-c"),
            RequiredAttribute(name="wattage", operator=ConstraintOperator.GTE, value=100),
            AllowedCategory(category="chargers"),
        ),
        preferences=(Preference(statement="prefer next day delivery"),),
    )

    payload = intent.to_payload()

    # Recorded verbatim in an audit event, so the shape is a contract and json.dumps
    # must not have to guess at any value.
    assert json.loads(json.dumps(payload)) == {
        "merchant_id": str(merchant_id),
        "description": "One 100W USB-C charger for a laptop",
        "hard_constraints": [
            {"kind": "max_total_amount", "amount_minor": 500000, "currency": "INR"},
            {"kind": "max_quantity", "quantity": 1},
            {
                "kind": "required_attribute",
                "name": "connector",
                "operator": "EQ",
                "value": "usb-c",
            },
            {
                "kind": "required_attribute",
                "name": "wattage",
                "operator": "GTE",
                "value": 100,
            },
            {"kind": "allowed_category", "category": "chargers"},
        ],
        "preferences": ["prefer next day delivery"],
    }


def test_an_intent_needs_a_description() -> None:
    with pytest.raises(ValueError, match="description"):
        BuyerIntent(merchant_id=uuid.uuid7(), description="   ")


def test_a_money_constraint_carries_a_valid_currency_and_a_non_negative_amount() -> None:
    with pytest.raises(ValueError, match="currency"):
        MaxTotalAmount(amount_minor=500000, currency="inr")

    with pytest.raises(ValueError, match="negative"):
        MaxTotalAmount(amount_minor=-1, currency="INR")


def test_a_quantity_constraint_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        MaxQuantity(quantity=0)


def test_two_maximum_amounts_are_a_contradiction_not_a_constraint_set() -> None:
    with pytest.raises(ValueError, match="conflicting"):
        BuyerIntent(
            merchant_id=uuid.uuid7(),
            description="One charger",
            hard_constraints=(
                MaxTotalAmount(amount_minor=500000, currency="INR"),
                MaxTotalAmount(amount_minor=400000, currency="INR"),
            ),
        )


def test_repeatable_constraints_may_appear_more_than_once() -> None:
    """Several allowed categories mean any one of them, which is not a conflict."""
    intent = BuyerIntent(
        merchant_id=uuid.uuid7(),
        description="A charger or a cable",
        hard_constraints=(AllowedCategory(category="chargers"), AllowedCategory(category="cables")),
    )

    assert [entry["category"] for entry in intent.to_payload()["hard_constraints"]] == [
        "chargers",
        "cables",
    ]


def test_a_required_attribute_states_how_it_is_compared() -> None:
    """The operator is a field, because "at least 100W" and "exactly black" differ."""
    assert RequiredAttribute(name="color", value="black").operator is ConstraintOperator.EQ

    with pytest.raises(ValueError, match="compares numbers"):
        RequiredAttribute(name="wattage", operator=ConstraintOperator.GTE, value="100")

    with pytest.raises(ValueError, match="single value"):
        RequiredAttribute(name="color", value=("black", "blue"))


def test_a_required_attribute_keeps_the_type_it_was_given() -> None:
    """`"100"` is not `100`, and the payload must not collapse the two."""
    numeric = RequiredAttribute(name="wattage", operator=ConstraintOperator.GTE, value=100)
    listed = RequiredAttribute(
        name="color", operator=ConstraintOperator.IN, value=("black", "graphite")
    )

    assert numeric.to_payload()["value"] == 100
    assert listed.to_payload()["value"] == ["black", "graphite"]
