"""Money rules shared by every subsystem that stores or reasons about an amount.

Money is an integer count of minor currency units and its currency always travels with
it. Rupees 4,999.00 is 499900 with currency INR. The rule predates any table and now has
more than one consumer, so the pattern and the two checks live in one place rather than
being restated wherever an amount appears.

The regular expression is also embedded in database check constraints, which is why it is
exported as a pattern string rather than only as a compiled object.
"""

import re

CURRENCY_PATTERN = r"^[A-Z]{3}$"

_CURRENCY = re.compile(CURRENCY_PATTERN)


def validate_currency(currency: str) -> None:
    """Reject anything that is not an uppercase three letter code.

    Deliberately a format check and not a list of known codes. AgentRank does not decide
    which currencies exist, and a stale allowlist would refuse a legitimate merchant.
    """
    if _CURRENCY.fullmatch(currency) is None:
        raise ValueError(f"currency must match {CURRENCY_PATTERN}, got {currency!r}")


def validate_amount_minor(amount_minor: int) -> None:
    """Reject a negative amount. Zero is allowed; it authorizes nothing rather than being
    invalid."""
    if amount_minor < 0:
        raise ValueError(f"amount in minor units must not be negative, got {amount_minor}")
