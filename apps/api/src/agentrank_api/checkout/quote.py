"""Quote arithmetic and the rules about a checkout's shape.

Pure domain code. No SQLAlchemy, no FastAPI, no clock reading, so the same inputs always
produce the same quote.

The arithmetic is deliberately trivial and deliberately in one place. Money is an integer
count of minor units, the currency travels with it, and there is no rounding anywhere
because there is nothing to round: a total is a sum of integers.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from agentrank_api.money import validate_amount_minor

# A quote is a bounded document. Without a ceiling a single request could ask for an
# arbitrarily large statement and an arbitrarily large audit payload.
MAX_CHECKOUT_LINES = 50

# How long a quote is good for when the caller does not say. Fifteen minutes is short
# enough that a stale price is unlikely and long enough for a buyer agent to decide.
DEFAULT_CHECKOUT_TTL = timedelta(minutes=15)

# The furthest ahead a caller may push expiry. A quote that lasts a year is a promise to
# honour a price nobody rechecked, which is exactly what expiry exists to prevent.
MAX_CHECKOUT_TTL = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class QuotedLine:
    """One variant, a quantity, and what the catalog said about it when the quote was made.

    Everything here except the identifiers is a snapshot. The price is read from the
    catalog once and never again, and so are the two semantic fields beside it: a checkout
    has to stay readable as the offer it was even after the catalog moves on.

    `product_category` and `variant_attributes` are here because semantic authorization
    asks what was actually being bought, and asking the live catalog would mean a merchant
    edit could change the answer for a quote made yesterday. They carry structured commerce
    values only. No title, no description, no merchant prose: the evaluator compares
    machine readable fields and nothing else can be compared anyway.

    `product_category` is nullable because `product.category` is. A catalog that does not
    say what something is fails an allowed category constraint, which is the right answer
    rather than a reason to guess.
    """

    variant_id: UUID
    quantity: int
    unit_price_amount_minor: int
    product_category: str | None = None
    variant_attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(f"line quantity must be positive, got {self.quantity}")
        validate_amount_minor(self.unit_price_amount_minor)

    @property
    def line_amount_minor(self) -> int:
        return self.quantity * self.unit_price_amount_minor


@dataclass(frozen=True, slots=True)
class CheckoutTotals:
    """What the quote adds up to.

    Derived rather than supplied, so a total that disagrees with its lines is not
    representable. The database enforces the same identity row locally; this is where the
    cross row sum that the database cannot see is computed.
    """

    subtotal_amount_minor: int
    shipping_amount_minor: int
    discount_amount_minor: int
    total_amount_minor: int


def checkout_totals(
    lines: Sequence[QuotedLine],
    *,
    shipping_amount_minor: int = 0,
    discount_amount_minor: int = 0,
) -> CheckoutTotals:
    """Add a quote up, refusing anything that would not be a quote.

    `total = subtotal + shipping - discount`, and the result must not be negative. A
    discount larger than what is being bought is a contradiction rather than a refund.
    """
    if not lines:
        raise ValueError("a checkout must contain at least one line")
    if len(lines) > MAX_CHECKOUT_LINES:
        raise ValueError(f"a checkout may contain at most {MAX_CHECKOUT_LINES} lines")
    validate_amount_minor(shipping_amount_minor)
    validate_amount_minor(discount_amount_minor)

    variants = {line.variant_id for line in lines}
    if len(variants) != len(lines):
        # Two lines for one variant are one line with a larger quantity. Allowing both
        # shapes would make the total quantity depend on how a caller split the request.
        raise ValueError("a checkout must not contain the same variant twice")

    subtotal = sum(line.line_amount_minor for line in lines)
    total = subtotal + shipping_amount_minor - discount_amount_minor
    if total < 0:
        raise ValueError("discount must not exceed the subtotal plus shipping")

    return CheckoutTotals(
        subtotal_amount_minor=subtotal,
        shipping_amount_minor=shipping_amount_minor,
        discount_amount_minor=discount_amount_minor,
        total_amount_minor=total,
    )


def total_quantity(lines: Sequence[QuotedLine]) -> int:
    """How many units the quote covers.

    The sum of the line quantities, never the number of lines. The distinction is what a
    mandate's quantity ceiling is about: three of one variant is three units, not one.
    """
    return sum(line.quantity for line in lines)


def validate_checkout_expiry(expires_at: datetime, *, now: datetime) -> None:
    """Reject an expiry that has already passed or is unreasonably far away.

    Also refused by a database check constraint, which compares against the row's own
    creation time. Stating it here as well is what turns an integrity error into a typed
    refusal naming the field, for HTTP callers and service callers alike.
    """
    if expires_at.tzinfo is None or now.tzinfo is None:
        raise ValueError("checkout expiry must be timezone aware")
    if expires_at <= now:
        raise ValueError("expires_at must be in the future")
    if expires_at > now + MAX_CHECKOUT_TTL:
        raise ValueError(f"expires_at must be within {MAX_CHECKOUT_TTL} of now")
