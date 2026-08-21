"""Rules about a mandate.

Two questions live here, and they are not the same question:

- may this mandate be created at all, which is about the request
- may this mandate be used right now, which is about the clock and the status

Neither of them is "does this checkout satisfy this mandate". That is Phase 1C, and
keeping it out of here is what stops the two from blurring.

Everything here is deterministic and takes the current time as an argument. No clock is
read, no database is touched and no model is consulted, so the same inputs always produce
the same answer.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from agentrank_api.mandates.models import MandateStatus, SpendingMandate


def validate_validity_window(valid_from: datetime, valid_until: datetime) -> None:
    """Reject a window that ends before it starts.

    Also refused by a database check constraint. It is stated here as well so that the
    API can answer with a message naming the fields rather than surfacing an integrity
    error, and so that a service caller gets the same refusal as an HTTP caller.
    """
    if valid_from.tzinfo is None or valid_until.tzinfo is None:
        raise ValueError("validity window timestamps must be timezone aware")
    if valid_until <= valid_from:
        raise ValueError("valid_until must be after valid_from")


class MandateViolation(StrEnum):
    """Why a mandate cannot be used right now.

    Machine readable identifiers, not prose. A buyer agent has to be able to tell "this
    authorization has not started yet" from "this authorization was revoked" without
    reading English.
    """

    MANDATE_NOT_ACTIVE = "MANDATE_NOT_ACTIVE"
    MANDATE_NOT_YET_VALID = "MANDATE_NOT_YET_VALID"
    MANDATE_EXPIRED = "MANDATE_EXPIRED"


@dataclass(frozen=True, slots=True)
class MandateValidationResult:
    """The outcome of checking a mandate against a moment in time.

    `valid` is derived rather than stored, so a result carrying violations cannot also
    claim to be valid. Violations are ordered, and the order is fixed, so two runs of the
    same check produce the same result.
    """

    violations: tuple[MandateViolation, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.violations


def validate_mandate(mandate: SpendingMandate, *, at: datetime) -> MandateValidationResult:
    """Answer whether this mandate is usable at `at`.

    This is not "does this checkout satisfy this mandate". It asks only about the mandate
    itself: its status, and where the given instant falls in its validity window. What a
    checkout may do with a usable mandate is a separate question and a later phase.

    An unusable mandate is an expected outcome, not an exception. It is reported as a
    typed result so that a caller has to handle it, and so that every reason can be
    reported at once rather than only the first one found.

    The evaluation time is a required argument. A function that reads the clock itself
    cannot be tested without controlling the clock, and an authorization decision should
    name the instant it was made against.
    """
    if at.tzinfo is None:
        raise ValueError("evaluation time must be timezone aware")

    violations: list[MandateViolation] = []
    # Fixed order: what the mandate is, then where the clock is. A revoked mandate that
    # has also expired reports both, always in this sequence.
    if mandate.status is not MandateStatus.ACTIVE:
        violations.append(MandateViolation.MANDATE_NOT_ACTIVE)
    if at < mandate.valid_from:
        violations.append(MandateViolation.MANDATE_NOT_YET_VALID)
    # Half open: usable at valid_from, not usable at valid_until.
    elif at >= mandate.valid_until:
        violations.append(MandateViolation.MANDATE_EXPIRED)

    return MandateValidationResult(violations=tuple(violations))
