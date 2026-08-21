"""Whether a mandate is usable right now.

Time is an argument, never a clock reading, so these tests state the instant they are
asking about instead of sleeping or freezing anything. The mandates are constructed in
memory: validation is a pure function of the field values, and persisting them would test
the database again rather than the rule.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from agentrank_api.mandates.models import MandateStatus, SpendingMandate
from agentrank_api.mandates.validation import MandateViolation, validate_mandate

NOON = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


def mandate(
    *,
    status: MandateStatus = MandateStatus.ACTIVE,
    valid_from: datetime = NOON,
    valid_until: datetime = NOON + HOUR,
) -> SpendingMandate:
    return SpendingMandate(
        id=uuid.uuid7(),
        merchant_id=uuid.uuid7(),
        max_total_amount_minor=500000,
        currency="INR",
        valid_from=valid_from,
        valid_until=valid_until,
        status=status,
        revoked_at=NOON if status is MandateStatus.REVOKED else None,
    )


def test_an_active_mandate_inside_its_window_is_usable() -> None:
    result = validate_mandate(mandate(), at=NOON + HOUR / 2)

    assert result.valid
    assert result.violations == ()

    # The window is half open, so the first instant counts and the last one does not.
    assert validate_mandate(mandate(), at=NOON).valid


def test_a_mandate_whose_window_has_not_opened_is_not_yet_valid() -> None:
    result = validate_mandate(mandate(), at=NOON - HOUR)

    assert not result.valid
    assert result.violations == (MandateViolation.MANDATE_NOT_YET_VALID,)


def test_a_mandate_is_expired_from_valid_until_onwards() -> None:
    assert validate_mandate(mandate(), at=NOON + HOUR).violations == (
        MandateViolation.MANDATE_EXPIRED,
    )
    assert validate_mandate(mandate(), at=NOON + 2 * HOUR).violations == (
        MandateViolation.MANDATE_EXPIRED,
    )


def test_a_revoked_mandate_is_never_usable() -> None:
    """Revocation beats the window. The mandate is inside its validity period here."""
    result = validate_mandate(mandate(status=MandateStatus.REVOKED), at=NOON + HOUR / 2)

    assert result.violations == (MandateViolation.MANDATE_NOT_ACTIVE,)


def test_every_reason_is_reported_in_a_fixed_order() -> None:
    """A caller should see all of it at once, and see it the same way every time."""
    revoked_and_expired = mandate(status=MandateStatus.REVOKED)

    result = validate_mandate(revoked_and_expired, at=NOON + 2 * HOUR)

    assert result.violations == (
        MandateViolation.MANDATE_NOT_ACTIVE,
        MandateViolation.MANDATE_EXPIRED,
    )


def test_a_naive_evaluation_time_is_refused() -> None:
    """Comparing a naive instant against a stored timestamptz is a silent wrong answer."""
    with pytest.raises(ValueError, match="timezone aware"):
        validate_mandate(mandate(), at=datetime(2026, 8, 21, 12, 30))
