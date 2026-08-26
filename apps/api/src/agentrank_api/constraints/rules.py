"""The vocabulary an authoritative buyer constraint is written in.

Pure domain code. No SQLAlchemy, no FastAPI, no clock and no model, so the same inputs
always produce the same answer. Everything here is shared by three consumers: the buyer
intent types that state a constraint, the table that stores it, and the evaluator that
checks a checkout against it. One vocabulary rather than three that drift.

Two rules shape the whole module:

- the operator set is small and closed. There is no regular expression, no expression
  language and no user supplied code. This is authorization code, not a rules engine
- types never coerce. `"100"` is not `100` and `true` is not `"true"`. A comparison
  between two different kinds of value is not a stricter check, it is a meaningless one,
  and it is reported rather than guessed at

The comparison rule for text is exact after normalization, and normalization is only
case folding and trimming. `black` matches `Black`, and nothing decides that `charcoal`
is close enough. See docs/architecture.md.
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Self

MAX_ATTRIBUTE_KEY_LENGTH = 200
MAX_TEXT_VALUE_LENGTH = 200

# A membership list is a list, not a query. Without a ceiling one constraint could carry
# an arbitrarily large document into an authorization row.
MAX_VALUE_MEMBERS = 50


class ConstraintOperator(StrEnum):
    """How a constraint value is compared against what a checkout actually carries.

    Five, and deliberately not six. Every one of these can be evaluated type safely
    against a JSON scalar. `MATCHES`, `CONTAINS` and anything expression shaped are
    absent because they invite a pattern language into an authorization decision.
    """

    EQ = "EQ"
    NE = "NE"
    GTE = "GTE"
    LTE = "LTE"
    IN = "IN"


class ConstraintValueType(StrEnum):
    """The kinds of value this vocabulary can compare.

    Booleans are their own kind rather than numbers. Python would happily agree that
    `True == 1`, which is exactly the silent coercion an authorization decision must not
    contain.
    """

    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"


class PersistedConstraintKind(StrEnum):
    """The constraint kinds that are stored as authorization data.

    Two, and both are semantic. `MAX_TOTAL_AMOUNT` and `MAX_QUANTITY` exist on a
    `BuyerIntent` and are deliberately not here: financial limits are authorized by the
    `SpendingMandate` and nowhere else. Two authoritative copies of one ceiling is a way
    for them to disagree. See docs/architecture.md.
    """

    ALLOWED_CATEGORY = "allowed_category"
    REQUIRED_ATTRIBUTE = "required_attribute"


ScalarValue = str | int | float | bool
ConstraintValue = ScalarValue | tuple[ScalarValue, ...]


def value_type(value: object) -> ConstraintValueType | None:
    """Which kind of value this is, or None when it is not comparable at all.

    Booleans are tested first, because `bool` is a subclass of `int` and testing for a
    number first would classify `True` as one.
    """
    if isinstance(value, bool):
        return ConstraintValueType.BOOLEAN
    if isinstance(value, int | float):
        return ConstraintValueType.NUMBER
    if isinstance(value, str):
        return ConstraintValueType.STRING
    return None


def normalize_text(value: str) -> str:
    """The one normalization applied before any text comparison.

    Trimming and case folding, and nothing else. It is applied identically to both sides
    of every comparison, so it cannot make a constraint looser for one merchant than for
    another. Nothing here decides that two different words mean the same thing.
    """
    return value.strip().casefold()


def lookup_attribute(attributes: Mapping[str, Any], key: str) -> tuple[bool, Any]:
    """Find an attribute by name, allowing for a merchant's capitalisation.

    An exact match wins. Failing that, a single match after normalization is accepted, so a
    buyer asking for `color` is answered by a merchant who wrote `Color`. Two keys that
    normalize to the same name are ambiguous, and an ambiguous lookup is reported as missing
    rather than resolved by picking one, because picking one is a guess.

    Here rather than in either of its callers, because both the semantic authorization gate and
    the benchmark evaluator have to answer "did the merchant state this" the same way. Two
    copies of this rule would eventually be two rules, and the benchmark would then measure
    something the authorization layer does not enforce.
    """
    if key in attributes:
        return True, attributes[key]

    wanted = normalize_text(key)
    matches = [value for name, value in attributes.items() if normalize_text(name) == wanted]
    if len(matches) == 1:
        return True, matches[0]
    return False, None


def compare(operator: ConstraintOperator, expected: ConstraintValue, actual: object) -> bool | None:
    """Compare a checkout value against a constraint value.

    Returns True when the constraint is satisfied, False when it is not, and None when
    the two values are not of the same kind and therefore cannot be compared at all. The
    caller turns None into a type mismatch violation, which denies: an unanswerable
    question is never a pass.

    `expected` has already been validated against `operator`, so a numeric comparison
    cannot reach here with a string on the constraint side.
    """
    actual_type = value_type(actual)
    if actual_type is None:
        return None

    if operator is ConstraintOperator.IN:
        if not isinstance(expected, tuple) or not expected:
            return None
        # Validation guarantees a non empty homogeneous tuple, so the first member's kind
        # is the kind of the whole list.
        if value_type(expected[0]) is not actual_type:
            return None
        return any(_equal(member, actual) for member in expected)

    if isinstance(expected, tuple) or value_type(expected) is not actual_type:
        return None

    if operator is ConstraintOperator.EQ:
        return _equal(expected, actual)
    if operator is ConstraintOperator.NE:
        return not _equal(expected, actual)

    # GTE and LTE are numbers only, and a boolean is not a number.
    if not isinstance(expected, int | float) or not isinstance(actual, int | float):
        return None
    if isinstance(expected, bool) or isinstance(actual, bool):
        return None
    if operator is ConstraintOperator.GTE:
        return actual >= expected
    return actual <= expected


def _equal(expected: ScalarValue, actual: object) -> bool:
    """Equality within one kind of value, with text normalized on both sides."""
    if isinstance(expected, str) and isinstance(actual, str):
        return normalize_text(expected) == normalize_text(actual)
    return bool(expected == actual)


@dataclass(frozen=True, slots=True)
class IntentConstraintSpec:
    """One authoritative constraint, validated, before it is written or after it is read.

    This is the shape the table stores and the shape the evaluator reads. Building one is
    what proves a constraint is well formed, so an unvalidated constraint cannot reach
    either side.
    """

    kind: PersistedConstraintKind
    attribute_key: str | None
    operator: ConstraintOperator
    value: ConstraintValue

    def __post_init__(self) -> None:
        validate_constraint_value(self.operator, self.value)

        if self.kind is PersistedConstraintKind.ALLOWED_CATEGORY:
            if self.attribute_key is not None:
                raise ValueError("an allowed category constraint has no attribute key")
            if self.operator is not ConstraintOperator.IN:
                raise ValueError("an allowed category constraint compares with IN")
            return

        if self.attribute_key is None or not self.attribute_key.strip():
            raise ValueError("a required attribute constraint must name an attribute")
        if len(self.attribute_key) > MAX_ATTRIBUTE_KEY_LENGTH:
            raise ValueError(f"attribute key must be at most {MAX_ATTRIBUTE_KEY_LENGTH} characters")

    @classmethod
    def allowed_categories(cls, categories: tuple[str, ...]) -> Self:
        """Every category the purchase may come from, as one constraint.

        Several allowed categories mean any one of them, not all of them, which is a
        single membership test rather than several constraints that would each have to
        pass. Folding them into one row is what gives that rule one identifier to report
        a violation against.
        """
        return cls(
            kind=PersistedConstraintKind.ALLOWED_CATEGORY,
            attribute_key=None,
            operator=ConstraintOperator.IN,
            value=categories,
        )

    @classmethod
    def required_attribute(
        cls, attribute_key: str, operator: ConstraintOperator, value: ConstraintValue
    ) -> Self:
        return cls(
            kind=PersistedConstraintKind.REQUIRED_ATTRIBUTE,
            attribute_key=attribute_key,
            operator=operator,
            value=value,
        )

    def to_stored_value(self) -> Any:
        """The JSON value this constraint is stored as.

        A tuple becomes a JSON array and a scalar stays a scalar, so the stored document
        says which shape the operator expects without a second column to explain it.
        """
        return list(self.value) if isinstance(self.value, tuple) else self.value

    def to_summary(self) -> dict[str, Any]:
        """A compact description for an audit payload.

        The constraint itself and nothing around it. There is no prose here and no
        identifier of anything the reader would have to resolve separately.
        """
        return {
            "kind": self.kind.value,
            "attribute": self.attribute_key,
            "operator": self.operator.value,
            "value": self.to_stored_value(),
        }


def validate_constraint_value(operator: ConstraintOperator, value: ConstraintValue) -> None:
    """Refuse a value the operator cannot compare.

    This is where fail closed starts. A `GTE` whose constraint value is the string
    `"100"` would otherwise reach the evaluator and either raise or, worse, compare
    lexically. The database restates the same rules as check constraints, so a row
    written around this function is refused as well.
    """
    if operator is ConstraintOperator.IN:
        if not isinstance(value, tuple):
            raise ValueError("an IN constraint takes a list of values")
        if not value:
            raise ValueError("an IN constraint must list at least one value")
        if len(value) > MAX_VALUE_MEMBERS:
            raise ValueError(f"an IN constraint may list at most {MAX_VALUE_MEMBERS} values")
        kinds = {_validated_scalar(member) for member in value}
        if len(kinds) > 1:
            raise ValueError("an IN constraint must list values of one type")
        return

    if isinstance(value, tuple):
        raise ValueError(f"the {operator.value} operator takes a single value, not a list")

    kind = _validated_scalar(value)
    ordering = operator in (ConstraintOperator.GTE, ConstraintOperator.LTE)
    if ordering and kind is not ConstraintValueType.NUMBER:
        raise ValueError(f"the {operator.value} operator compares numbers, got {value!r}")


def _validated_scalar(value: ScalarValue) -> ConstraintValueType:
    kind = value_type(value)
    if kind is None:
        raise ValueError(f"a constraint value must be text, a number or a boolean, got {value!r}")
    if isinstance(value, str):
        if not value.strip():
            raise ValueError("a constraint value must not be blank")
        if len(value) > MAX_TEXT_VALUE_LENGTH:
            raise ValueError(
                f"a constraint value must be at most {MAX_TEXT_VALUE_LENGTH} characters"
            )
    # Neither infinity nor NaN is representable in JSON, so a row holding one could not
    # round trip through the column it is stored in.
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"a numeric constraint value must be finite, got {value!r}")
    return kind
