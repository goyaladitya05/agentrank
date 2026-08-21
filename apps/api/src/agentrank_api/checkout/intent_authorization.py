"""Does this checkout satisfy the buyer's authoritative hard constraints.

The second of two independent authorization questions, and the one Phase 1C deliberately
left unanswered. `authorize_checkout` asks whether the money is within what was
authorized. This asks whether the thing being bought is what was asked for. A checkout can
pass either and fail the other, and both have to pass before any payment can be considered
safe. Neither substitutes for the other, and this function never looks at an amount.

Everything here is pure. It reads no clock, touches no database, calls no external service
and consults no model, so the same two rows always produce the same decision. There is no
evaluation instant either, unlike the financial gate: whether a charger is black does not
depend on what time it is.

It reads the checkout's own snapshot rather than the catalog. A merchant editing a variant
from black to blue after a quote was written must not change whether that quote satisfied
what the buyer asked for.

Three rules decide everything, and all three fail closed:

- every constraint applies to every line. "Category chargers" is not satisfied by a
  checkout that also contains headphones
- a missing attribute is a denial, never a default. Merchant data an agent cannot read is
  a measurable commerce failure, and inferring it from a product description would be
  inventing the answer
- values of different kinds are never compared. `"100"` is not `100`, and reporting either
  a pass or an ordinary mismatch from that comparison would be reporting a fact nobody
  established

Nothing here writes, including no audit event. Evaluating an authorization is a read, and
an event per evaluation would turn the trail into a request log.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from agentrank_api.checkout.models import CheckoutLine, CheckoutSession
from agentrank_api.constraints.models import IntentConstraintSet
from agentrank_api.constraints.rules import (
    ConstraintOperator,
    IntentConstraintSpec,
    PersistedConstraintKind,
    compare,
    normalize_text,
)


class IntentViolationCode(StrEnum):
    """Why a checkout does not satisfy what the buyer required.

    Machine readable identifiers, not prose, for the same reason every other code in this
    project is one. `REQUIRED_ATTRIBUTE_MISSING` and `REQUIRED_ATTRIBUTE_MISMATCH` are
    deliberately different: the first says the merchant never published the fact, and the
    second says the merchant published it and it is the wrong value. A benchmark that
    cannot tell those apart cannot say anything useful about a catalog.
    """

    CONSTRAINTS_MERCHANT_MISMATCH = "CONSTRAINTS_MERCHANT_MISMATCH"
    CONSTRAINTS_MANDATE_MISMATCH = "CONSTRAINTS_MANDATE_MISMATCH"
    CHECKOUT_HAS_NO_LINES = "CHECKOUT_HAS_NO_LINES"
    CATEGORY_NOT_ALLOWED = "CATEGORY_NOT_ALLOWED"
    REQUIRED_ATTRIBUTE_MISSING = "REQUIRED_ATTRIBUTE_MISSING"
    REQUIRED_ATTRIBUTE_MISMATCH = "REQUIRED_ATTRIBUTE_MISMATCH"
    ATTRIBUTE_TYPE_MISMATCH = "ATTRIBUTE_TYPE_MISMATCH"


@dataclass(frozen=True, slots=True)
class IntentConstraintViolation:
    """One rule, one line, and what the two disagreed about.

    A bare code is not enough for a semantic denial. "The purchase was not what you asked
    for" is not actionable; "line X quotes a blue charger and you required black" is. Every
    field here exists so that an explanation can be assembled without a second query: the
    constraint that was violated, the line and variant that violated it, and the two values
    side by side.

    `operator` travels with `expected` because the value alone does not say what was
    required. `100` means one thing beside `GTE` and another beside `EQ`.

    The binding violations carry no constraint and no line, because they are about the two
    rows not belonging together rather than about any rule inside them.
    """

    code: IntentViolationCode
    constraint_id: uuid.UUID | None = None
    line_id: uuid.UUID | None = None
    variant_id: uuid.UUID | None = None
    attribute: str | None = None
    operator: ConstraintOperator | None = None
    expected: Any = None
    actual: Any = None


@dataclass(frozen=True, slots=True)
class IntentConstraintDecision:
    """The outcome of checking one checkout against one constraint set.

    `satisfied` is derived rather than stored, so a decision carrying violations cannot
    also claim the purchase was what the buyer asked for. Violations are ordered, and the
    order is fixed, so two runs of the same check produce the same decision.
    """

    violations: tuple[IntentConstraintViolation, ...] = ()

    @property
    def satisfied(self) -> bool:
        return not self.violations


def evaluate_intent_constraints(
    checkout: CheckoutSession, constraint_set: IntentConstraintSet
) -> IntentConstraintDecision:
    """Answer whether this checkout satisfies these authoritative constraints.

    Every violation is reported rather than only the first one found, because a denied
    checkout is an ordinary outcome and a caller usually wants the whole picture. The
    order is fixed: the constraints in the order they were authorized, and within each one
    the lines in the order they were quoted.

    The binding is checked first and short circuits. Constraints that were not authorized
    for this checkout are not rules this checkout failed, so reporting what it did against
    them would be reporting noise. The database makes this pairing hard to get wrong, and
    this catches a caller that assembled the two by hand anyway.

    Nothing about status, expiry or money is considered. Those belong to
    `authorize_checkout`, and keeping the two apart is what lets a caller tell "you cannot
    afford this" from "this is not what you asked for".
    """
    binding = _binding_violations(checkout, constraint_set)
    if binding:
        return IntentConstraintDecision(violations=tuple(binding))

    lines = list(checkout.lines)
    if not lines:
        # Not reachable through `CheckoutRepository.create`, which refuses an empty quote.
        # Stated anyway, because a checkout with no lines would otherwise satisfy every
        # constraint by having nothing to check, which is the loudest possible false pass.
        return IntentConstraintDecision(
            violations=(IntentConstraintViolation(code=IntentViolationCode.CHECKOUT_HAS_NO_LINES),)
        )

    violations: list[IntentConstraintViolation] = []
    for constraint in constraint_set.constraints:
        spec = constraint.to_spec()
        for line in lines:
            violation = _check_line(spec, line)
            if violation is not None:
                violations.append(
                    IntentConstraintViolation(
                        code=violation.code,
                        constraint_id=constraint.id,
                        line_id=line.id,
                        variant_id=line.variant_id,
                        attribute=spec.attribute_key,
                        operator=spec.operator,
                        expected=spec.to_stored_value(),
                        actual=violation.actual,
                    )
                )

    return IntentConstraintDecision(violations=tuple(violations))


@dataclass(frozen=True, slots=True)
class _LineFailure:
    """What one line got wrong about one constraint, before it is given its identifiers."""

    code: IntentViolationCode
    actual: Any


def _binding_violations(
    checkout: CheckoutSession, constraint_set: IntentConstraintSet
) -> list[IntentConstraintViolation]:
    violations: list[IntentConstraintViolation] = []
    if checkout.merchant_id != constraint_set.merchant_id:
        violations.append(
            IntentConstraintViolation(
                code=IntentViolationCode.CONSTRAINTS_MERCHANT_MISMATCH,
                expected=str(constraint_set.merchant_id),
                actual=str(checkout.merchant_id),
            )
        )
    if checkout.mandate_id != constraint_set.mandate_id:
        violations.append(
            IntentConstraintViolation(
                code=IntentViolationCode.CONSTRAINTS_MANDATE_MISMATCH,
                expected=str(constraint_set.mandate_id),
                actual=str(checkout.mandate_id),
            )
        )
    return violations


def _check_line(spec: IntentConstraintSpec, line: CheckoutLine) -> _LineFailure | None:
    """Check one constraint against one quoted line, or report why it does not hold."""
    if spec.kind is PersistedConstraintKind.ALLOWED_CATEGORY:
        return _check_category(spec, line.product_category)
    return _check_attribute(spec, line.variant_attributes or {})


def _check_category(spec: IntentConstraintSpec, category: str | None) -> _LineFailure | None:
    if category is None:
        # A catalog that does not say what something is cannot say it is allowed. This is
        # the machine unreadable merchant data case, and it denies rather than passing.
        return _LineFailure(code=IntentViolationCode.CATEGORY_NOT_ALLOWED, actual=None)

    result = compare(spec.operator, spec.value, category)
    if result is None:
        return _LineFailure(code=IntentViolationCode.ATTRIBUTE_TYPE_MISMATCH, actual=category)
    if not result:
        return _LineFailure(code=IntentViolationCode.CATEGORY_NOT_ALLOWED, actual=category)
    return None


def _check_attribute(
    spec: IntentConstraintSpec, attributes: Mapping[str, Any]
) -> _LineFailure | None:
    if spec.attribute_key is None:
        # Unreachable: a required attribute constraint always names one, in the domain and
        # at the database. Treated as missing rather than skipped, because a rule that
        # cannot be evaluated must not be a rule that passed.
        return _LineFailure(code=IntentViolationCode.REQUIRED_ATTRIBUTE_MISSING, actual=None)

    found, actual = _lookup(attributes, spec.attribute_key)
    if not found:
        # Absence is not falsehood and it is not zero. Nothing here reads a product
        # description to guess what the merchant meant.
        return _LineFailure(code=IntentViolationCode.REQUIRED_ATTRIBUTE_MISSING, actual=None)

    result = compare(spec.operator, spec.value, actual)
    if result is None:
        return _LineFailure(code=IntentViolationCode.ATTRIBUTE_TYPE_MISMATCH, actual=actual)
    if not result:
        return _LineFailure(code=IntentViolationCode.REQUIRED_ATTRIBUTE_MISMATCH, actual=actual)
    return None


def _lookup(attributes: Mapping[str, Any], key: str) -> tuple[bool, Any]:
    """Find an attribute by name, allowing for a merchant's capitalisation.

    An exact match wins. Failing that, a single match after normalization is accepted, so
    a buyer asking for `color` is answered by a merchant who wrote `Color`. Two keys that
    normalize to the same name are ambiguous, and an ambiguous lookup is reported as
    missing rather than resolved by picking one, because picking one is a guess.
    """
    if key in attributes:
        return True, attributes[key]

    wanted = normalize_text(key)
    matches = [value for name, value in attributes.items() if normalize_text(name) == wanted]
    if len(matches) == 1:
        return True, matches[0]
    return False, None
