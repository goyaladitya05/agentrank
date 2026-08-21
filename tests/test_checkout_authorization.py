"""The one question this phase exists to answer, at exact boundaries.

Pure domain tests. The objects are built in memory and never persisted, because
authorization is a function of their fields and nothing else. The evaluation instant is
passed in rather than slept towards, so every boundary is exact rather than approximate.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from agentrank_api.checkout.authorization import (
    FROM_MANDATE_VIOLATION,
    CheckoutAuthorizationViolation,
    authorize_checkout,
)
from agentrank_api.checkout.models import CheckoutLine, CheckoutSession, CheckoutStatus
from agentrank_api.mandates.models import MandateStatus, SpendingMandate
from agentrank_api.mandates.validation import MandateViolation

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
SECOND = timedelta(seconds=1)
MERCHANT = uuid.uuid7()
TOTAL = 500000

V = CheckoutAuthorizationViolation


def a_mandate(**overrides: object) -> SpendingMandate:
    fields: dict[str, object] = {
        "id": uuid.uuid7(),
        "merchant_id": MERCHANT,
        "max_total_amount_minor": TOTAL,
        "currency": "INR",
        "max_quantity": None,
        "valid_from": NOW,
        "valid_until": NOW + HOUR,
        "status": MandateStatus.ACTIVE,
    }
    return SpendingMandate(**(fields | overrides))


def a_checkout(mandate: SpendingMandate, *quantities: int, **overrides: object) -> CheckoutSession:
    fields: dict[str, object] = {
        "id": uuid.uuid7(),
        "merchant_id": mandate.merchant_id,
        "mandate_id": mandate.id,
        "currency": "INR",
        "subtotal_amount_minor": TOTAL,
        "shipping_amount_minor": 0,
        "discount_amount_minor": 0,
        "total_amount_minor": TOTAL,
        "status": CheckoutStatus.OPEN,
        "expires_at": NOW + HOUR,
    }
    checkout = CheckoutSession(**(fields | overrides))
    checkout.lines = [
        CheckoutLine(
            checkout_id=checkout.id,
            merchant_id=checkout.merchant_id,
            variant_id=uuid.uuid7(),
            quantity=quantity,
            unit_price_amount_minor=1,
            currency=checkout.currency,
        )
        for quantity in (quantities or (1,))
    ]
    return checkout


def test_a_checkout_inside_every_limit_is_allowed() -> None:
    mandate = a_mandate()
    decision = authorize_checkout(a_checkout(mandate), mandate, at=NOW)

    assert decision.allowed
    assert decision.violations == ()


def test_a_total_equal_to_the_ceiling_is_allowed_and_one_unit_over_is_not() -> None:
    """The equality boundary, which is the one an off by one gets wrong."""
    mandate = a_mandate()

    exact = a_checkout(mandate, total_amount_minor=TOTAL)
    assert authorize_checkout(exact, mandate, at=NOW).allowed

    over = a_checkout(mandate, total_amount_minor=TOTAL + 1)
    assert authorize_checkout(over, mandate, at=NOW).violations == (V.MAX_TOTAL_EXCEEDED,)


def test_a_quantity_equal_to_the_ceiling_is_allowed_and_one_more_is_not() -> None:
    mandate = a_mandate(max_quantity=3)

    # Three units across two lines. Quantity is the sum, not the number of lines.
    exact = a_checkout(mandate, 2, 1)
    assert authorize_checkout(exact, mandate, at=NOW).allowed

    over = a_checkout(mandate, 2, 2)
    assert authorize_checkout(over, mandate, at=NOW).violations == (V.MAX_QUANTITY_EXCEEDED,)


def test_a_mandate_without_a_quantity_ceiling_places_no_limit() -> None:
    """Null means no limit. It does not mean zero and it does not mean one."""
    mandate = a_mandate(max_quantity=None)

    assert authorize_checkout(a_checkout(mandate, 99), mandate, at=NOW).allowed


def test_a_currency_mismatch_is_denied_and_the_amounts_are_not_compared() -> None:
    """Comparing an amount against a ceiling in another currency is meaningless, so no
    MAX_TOTAL_EXCEEDED is reported alongside it."""
    mandate = a_mandate(currency="INR", max_total_amount_minor=1)
    checkout = a_checkout(mandate, currency="EUR", total_amount_minor=999999)

    assert authorize_checkout(checkout, mandate, at=NOW).violations == (V.CURRENCY_MISMATCH,)


def test_the_mandate_window_boundaries_are_half_open() -> None:
    """Usable at valid_from, not usable at valid_until. Same rule as mandate validation,
    because it is the same rule."""
    mandate = a_mandate()
    # Expiry well past the mandate window, so only the mandate boundary is under test.
    checkout = a_checkout(mandate, expires_at=NOW + 10 * HOUR)

    assert authorize_checkout(checkout, mandate, at=NOW - SECOND).violations == (
        V.MANDATE_NOT_YET_VALID,
    )
    assert authorize_checkout(checkout, mandate, at=NOW).allowed
    assert authorize_checkout(checkout, mandate, at=NOW + HOUR - SECOND).allowed
    assert authorize_checkout(checkout, mandate, at=NOW + HOUR).violations == (V.MANDATE_EXPIRED,)


def test_the_checkout_expiry_boundary_is_half_open_too() -> None:
    mandate = a_mandate(valid_until=NOW + 10 * HOUR)
    checkout = a_checkout(mandate, expires_at=NOW + HOUR)

    assert authorize_checkout(checkout, mandate, at=NOW + HOUR - SECOND).allowed
    assert authorize_checkout(checkout, mandate, at=NOW + HOUR).violations == (V.CHECKOUT_EXPIRED,)


def test_a_revoked_mandate_is_denied() -> None:
    mandate = a_mandate(status=MandateStatus.REVOKED, revoked_at=NOW)

    assert authorize_checkout(a_checkout(mandate), mandate, at=NOW).violations == (
        V.MANDATE_NOT_ACTIVE,
    )


def test_a_cancelled_checkout_is_denied() -> None:
    mandate = a_mandate()
    checkout = a_checkout(mandate, status=CheckoutStatus.CANCELLED, cancelled_at=NOW)

    assert authorize_checkout(checkout, mandate, at=NOW).violations == (V.CHECKOUT_NOT_OPEN,)


def test_a_checkout_paired_with_the_wrong_mandate_is_denied() -> None:
    """The database prevents the cross merchant pairing. This catches a checkout handed a
    different mandate belonging to the same merchant."""
    mandate = a_mandate()
    other_merchant = a_mandate(merchant_id=uuid.uuid7())

    same_merchant = a_mandate()
    checkout = a_checkout(mandate)
    assert authorize_checkout(checkout, same_merchant, at=NOW).violations == (
        V.CHECKOUT_MANDATE_MISMATCH,
    )
    assert authorize_checkout(checkout, other_merchant, at=NOW).violations == (
        V.CHECKOUT_MERCHANT_MISMATCH,
        V.CHECKOUT_MANDATE_MISMATCH,
    )


def test_every_relevant_reason_is_reported_at_once_in_a_fixed_order() -> None:
    """A denied checkout is an ordinary outcome, and a caller wants the whole picture."""
    mandate = a_mandate(status=MandateStatus.REVOKED, revoked_at=NOW, max_quantity=1)
    checkout = a_checkout(
        mandate,
        4,
        status=CheckoutStatus.CANCELLED,
        cancelled_at=NOW,
        expires_at=NOW + HOUR,
        total_amount_minor=TOTAL + 1,
    )

    assert authorize_checkout(checkout, mandate, at=NOW + 2 * HOUR).violations == (
        V.MANDATE_NOT_ACTIVE,
        V.MANDATE_EXPIRED,
        V.CHECKOUT_NOT_OPEN,
        V.CHECKOUT_EXPIRED,
        V.MAX_TOTAL_EXCEEDED,
        V.MAX_QUANTITY_EXCEEDED,
    )


def test_a_naive_evaluation_time_is_refused_rather_than_compared() -> None:
    mandate = a_mandate()

    with pytest.raises(ValueError, match="timezone aware"):
        authorize_checkout(a_checkout(mandate), mandate, at=NOW.replace(tzinfo=None))


def test_every_mandate_violation_has_a_checkout_violation() -> None:
    """The translation table is written out, so a new mandate violation must be added here
    rather than becoming a KeyError the first time it is reported."""
    assert set(FROM_MANDATE_VIOLATION) == set(MandateViolation)
