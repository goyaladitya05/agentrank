"""Buyer intent: a structured statement of what a buyer is trying to accomplish.

Intent is not authorization. "I want a 100W charger and I would rather not spend more
than 5000 rupees" is a desire. A `SpendingMandate` is the authoritative financial
boundary that execution code enforces. They are separate types on purpose, and nothing
that decides whether money may move will ever read an intent.

Two kinds of statement live here and the difference is the whole point:

- a hard constraint is typed, and is shaped so that a future checkout can be checked
  against it deterministically, without a model in the loop
- a preference is advisory prose. It may guide a planner. It is never enforced, and no
  amount of it can widen what a mandate permits

Nothing here is persisted. See docs/decisions.md for why.
"""

import uuid
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar

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

    Compared against `variant.attributes`, which is why both sides are strings: the JSONB
    document is heterogeneous and a typed comparison would need a schema no merchant
    supplies.
    """

    KIND: ClassVar[ConstraintKind] = ConstraintKind.REQUIRED_ATTRIBUTE

    name: str
    value: str

    def __post_init__(self) -> None:
        _require_text(self.name, "attribute name")
        _require_text(self.value, "attribute value")

    def to_payload(self) -> dict[str, Any]:
        return {"kind": self.KIND.value, "name": self.name, "value": self.value}


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


def _require_text(value: str, label: str, *, limit: int | None = None) -> None:
    if not value.strip():
        raise ValueError(f"{label} must not be blank")
    if limit is not None and len(value) > limit:
        raise ValueError(f"{label} must be at most {limit} characters")
