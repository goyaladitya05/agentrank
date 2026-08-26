"""Buyer intent: a structured statement of what a buyer is trying to accomplish.

Intent is not authorization. "I want a 100W charger and I would rather not spend more
than 5000 rupees" is a desire. A `SpendingMandate` is the authoritative financial
boundary that execution code enforces. They are separate types on purpose, and nothing
that decides whether money may move will ever read an intent.

Two kinds of statement live here and the difference is the whole point:

- a hard constraint is typed, and is shaped so that a checkout can be checked against it
  deterministically, without a model in the loop
- a preference is advisory prose. It may guide a planner. It is never enforced, and no
  amount of it can widen what a mandate permits

Nothing here is persisted. An intent is a request, not authorization data. Its enforceable
half is validated into an `IntentConstraintSet`, which is authoritative and immutable, and
that is what execution will read. The description and the preferences survive only in the
`mandate.created` audit payload, and nothing reads them back. See docs/architecture.md.

The financial kinds here are the exception to that. `MaxTotalAmount` and `MaxQuantity` are
never persisted as constraints, because a `SpendingMandate` is the only authority on money
and a ceiling stored in two places is a ceiling that can disagree with itself. They are
validated against the mandate instead, so a buyer's stated limit cannot be quietly widened
by the authorization that replaces it.
"""

import uuid
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar

from agentrank_api.constraints.rules import (
    MAX_ATTRIBUTE_KEY_LENGTH,
    ConstraintOperator,
    ConstraintValue,
    ScalarValue,
    validate_constraint_value,
)
from agentrank_api.money import validate_amount_minor, validate_currency

MAX_DESCRIPTION_LENGTH = 1000
MAX_STATEMENT_LENGTH = 200
MAX_HARD_CONSTRAINTS = 20
MAX_PREFERENCES = 20


class ConstraintKind(StrEnum):
    """The hard constraints that exist today.

    Small on purpose. Commerce can express far more than this, and inventing the rest now
    would mean guessing at rules nothing can enforce yet. A new kind is a new frozen
    dataclass plus one member of `HardConstraint`.
    """

    MAX_TOTAL_AMOUNT = "max_total_amount"
    MAX_QUANTITY = "max_quantity"
    REQUIRED_ATTRIBUTE = "required_attribute"
    ALLOWED_CATEGORY = "allowed_category"


# A kind in this set answers a question that has exactly one answer. Two maximum amounts
# are not a tighter constraint set, they are a contradiction, so they are refused.
SINGLE_VALUED_KINDS = frozenset({ConstraintKind.MAX_TOTAL_AMOUNT, ConstraintKind.MAX_QUANTITY})


@dataclass(frozen=True, slots=True)
class MaxTotalAmount:
    """A ceiling on what the whole purchase may cost.

    Structured rather than written into the description, because an amount buried in
    prose cannot be checked. Note that this is still only intent: the mandate carries the
    amount that actually authorizes anything.
    """

    KIND: ClassVar[ConstraintKind] = ConstraintKind.MAX_TOTAL_AMOUNT

    amount_minor: int
    currency: str

    def __post_init__(self) -> None:
        validate_amount_minor(self.amount_minor)
        validate_currency(self.currency)

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.KIND.value,
            "amount_minor": self.amount_minor,
            "currency": self.currency,
        }


@dataclass(frozen=True, slots=True)
class MaxQuantity:
    """A ceiling on how many units may be bought."""

    KIND: ClassVar[ConstraintKind] = ConstraintKind.MAX_QUANTITY

    quantity: int

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(f"max quantity must be positive, got {self.quantity}")

    def to_payload(self) -> dict[str, Any]:
        return {"kind": self.KIND.value, "quantity": self.quantity}


@dataclass(frozen=True, slots=True)
class RequiredAttribute:
    """A variant attribute the purchased item must carry.

    Compared against `variant.attributes` as it was snapshotted onto a checkout line. The
    comparison is stated rather than assumed: "wattage at least 100" and "colour exactly
    black" are both hard constraints and they are not the same test, so the operator is a
    field and the value keeps its type.

    `operator` and the value rules come from `agentrank_api.constraints.rules`, which is
    also what the authoritative constraint table stores and what the evaluator compares
    with. One vocabulary rather than a buyer facing one and a storage one that drift.
    """

    KIND: ClassVar[ConstraintKind] = ConstraintKind.REQUIRED_ATTRIBUTE

    name: str
    value: ConstraintValue
    operator: ConstraintOperator = ConstraintOperator.EQ

    def __post_init__(self) -> None:
        _require_text(self.name, "attribute name", limit=MAX_ATTRIBUTE_KEY_LENGTH)
        validate_constraint_value(self.operator, self.value)

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.KIND.value,
            "name": self.name,
            "operator": self.operator.value,
            "value": list(self.value) if isinstance(self.value, tuple) else self.value,
        }


@dataclass(frozen=True, slots=True)
class AllowedCategory:
    """A product category the purchase may come from.

    Repeatable. Several of these mean any one of them, not all of them.
    """

    KIND: ClassVar[ConstraintKind] = ConstraintKind.ALLOWED_CATEGORY

    category: str

    def __post_init__(self) -> None:
        _require_text(self.category, "category")

    def to_payload(self) -> dict[str, Any]:
        return {"kind": self.KIND.value, "category": self.category}


HardConstraint = MaxTotalAmount | MaxQuantity | RequiredAttribute | AllowedCategory


@dataclass(frozen=True, slots=True)
class Preference:
    """Advisory. A planner may weigh it, nothing enforces it.

    Free text is acceptable here precisely because it is not enforced. The moment a
    statement has to be obeyed it belongs in a hard constraint or in the mandate, where
    it can be checked.
    """

    statement: str

    def __post_init__(self) -> None:
        _require_text(self.statement, "preference statement", limit=MAX_STATEMENT_LENGTH)


@dataclass(frozen=True, slots=True)
class BuyerIntent:
    """What a buyer wants from one merchant.

    Merchant scoped, because AgentRank benchmarks one merchant at a time and an intent
    that spans merchants has no meaning until a cross merchant buyer exists.

    Preference order is significant: earlier statements matter more. That is a convention
    rather than a weight, since a numeric weight nobody computes with would be a number
    invented to look precise.
    """

    merchant_id: uuid.UUID
    description: str
    hard_constraints: tuple[HardConstraint, ...] = ()
    preferences: tuple[Preference, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_text(self.description, "description", limit=MAX_DESCRIPTION_LENGTH)
        if len(self.hard_constraints) > MAX_HARD_CONSTRAINTS:
            raise ValueError(f"at most {MAX_HARD_CONSTRAINTS} hard constraints are allowed")
        if len(self.preferences) > MAX_PREFERENCES:
            raise ValueError(f"at most {MAX_PREFERENCES} preferences are allowed")

        counts = Counter(constraint.KIND for constraint in self.hard_constraints)
        repeated = sorted(kind.value for kind in SINGLE_VALUED_KINDS if counts[kind] > 1)
        if repeated:
            raise ValueError(f"conflicting hard constraints: {', '.join(repeated)}")

    def to_payload(self) -> dict[str, Any]:
        """A JSON object describing this intent.

        This is what gets recorded alongside a mandate in the audit trail, so it holds
        the buyer's own words and nothing else. It is a record of why an authorization
        was asked for, never an input to deciding whether one may be used.
        """
        return {
            "merchant_id": str(self.merchant_id),
            "description": self.description,
            "hard_constraints": [constraint.to_payload() for constraint in self.hard_constraints],
            "preferences": [preference.statement for preference in self.preferences],
        }


def hard_constraint_from_payload(payload: Mapping[str, Any]) -> HardConstraint:
    """Rebuild one hard constraint from the JSON object `to_payload` produced.

    The inverse exists because a benchmark mission definition is stored and read back, and a
    stored requirement has to become the same typed constraint a buyer would have stated.
    Without it the benchmark would need its own vocabulary for "black only" and there would
    be two languages for one idea, which is exactly what this module exists to prevent.

    Strict in both directions. An unknown kind, a missing field or a field of the wrong type
    raises rather than producing a constraint with a plausible default, because a
    requirement nobody stated is a requirement that silently passes. The constructed object
    revalidates itself, so a payload that was written around this function still cannot
    become a malformed constraint.

    A JSON array becomes a tuple, which is the shape `IN` expects. Nothing else is coerced:
    `"100"` stays a string and is refused by the value rules rather than read as a number.
    """
    kind = payload.get("kind")
    match kind:
        case ConstraintKind.MAX_TOTAL_AMOUNT.value:
            return MaxTotalAmount(
                amount_minor=_require_int(payload, "amount_minor"),
                currency=_require_str(payload, "currency"),
            )
        case ConstraintKind.MAX_QUANTITY.value:
            return MaxQuantity(quantity=_require_int(payload, "quantity"))
        case ConstraintKind.REQUIRED_ATTRIBUTE.value:
            return RequiredAttribute(
                name=_require_str(payload, "name"),
                operator=ConstraintOperator(_require_str(payload, "operator")),
                value=_require_value(payload),
            )
        case ConstraintKind.ALLOWED_CATEGORY.value:
            return AllowedCategory(category=_require_str(payload, "category"))
        case _:
            raise ValueError(f"unknown hard constraint kind: {kind!r}")


def _require_value(payload: Mapping[str, Any]) -> ConstraintValue:
    """The comparison value, in the vocabulary's own value type.

    A JSON array becomes a tuple, which is the shape `IN` expects. Every member is checked to
    be a scalar this vocabulary can compare, so a nested object or a null is refused here
    rather than reaching a comparison that cannot answer. Nothing else is coerced: `"100"`
    stays a string and is refused by the operator's own value rules.
    """
    raw = payload.get("value")
    if isinstance(raw, list):
        return tuple(_require_scalar(member) for member in raw)
    return _require_scalar(raw)


def _require_scalar(value: Any) -> ScalarValue:
    # Booleans are integers in Python, and the vocabulary keeps them as their own kind rather
    # than rejecting them here. `value_type` is what tells them apart at comparison time.
    if isinstance(value, str | int | float):
        return value
    raise ValueError(f"a constraint value must be text, a number or a boolean, got {value!r}")


def _require_str(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text, got {value!r}")
    return value


def _require_int(payload: Mapping[str, Any], field: str) -> int:
    value = payload.get(field)
    # Booleans are integers in Python and would pass an isinstance check, which is the same
    # silent coercion the constraint vocabulary refuses everywhere else.
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field} must be a whole number, got {value!r}")
    return value


def _require_text(value: str, label: str, *, limit: int | None = None) -> None:
    if not value.strip():
        raise ValueError(f"{label} must not be blank")
    if limit is not None and len(value) > limit:
        raise ValueError(f"{label} must be at most {limit} characters")
