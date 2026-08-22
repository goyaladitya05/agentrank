"""The Razorpay entities this phase reads, parsed out of untrusted JSON into frozen records.

Two entities and deliberately not more. An Order is what a backend creates and what Standard
Checkout is opened against. A Payment is what a customer produced against it. Refunds,
settlements, invoices, subscriptions and payment links are all absent, because nothing calls
them and a parser for an entity with no reader is a guess about a vendor's API.

Every field is parsed rather than trusted. A response body is untrusted input in the ordinary
sense, and it is also untrusted in a sharper one: this application is about to decide whether a
merchant's stock leaves the shelf on the strength of what is in it. A missing amount, an amount
that arrives as a string, or a currency of the wrong shape all raise
`RazorpayUnreadableError` here rather than becoming a `None` that some later comparison quietly
treats as equal.

Statuses are kept as the vendor's own strings. That is deliberate: what a Razorpay payment state
means for AgentRank is a mapping, the mapping is a decision, and a decision belongs in one place
with a name. See `agentrank_api.razorpay.translation`. Parsing a status into an enum here would
put half that decision in a parser and would fail on the day Razorpay adds a state, which is
exactly when this application most wants to keep reading the rest of the entity.

Amounts are integers in the smallest currency subunit, which is what Razorpay documents and what
this application already uses everywhere. There is no conversion anywhere in this package, and
that is the point: a conversion is a place a rounding error can live.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Self

from agentrank_api.razorpay.errors import RazorpayUnreadableError

# Razorpay documents an order receipt as at most 40 characters and unique on the account, and it
# rejects characters outside its supported encoding. Restated here because this is the module
# that builds the request carrying one.
MAX_RECEIPT_LENGTH = 40

# Razorpay documents notes as at most 15 pairs of at most 256 characters each.
MAX_NOTES = 15
MAX_NOTE_LENGTH = 256


@dataclass(frozen=True, slots=True)
class NewOrder:
    """What this application asks Razorpay to create, and nothing else.

    Frozen, and every value comes off a committed `PaymentAttempt`. There is no field a caller
    could set: no amount from a request body, no currency from a query string, no receipt a
    browser chose. That is the whole integrity property of the preparation path expressed as a
    type.

    `receipt` is the derived provider operation reference. Razorpay treats it as unique on the
    account, which makes it both the namespace that keeps two merchants apart and the handle a
    lost create response is recovered by.

    `notes` exist so that a human reading the Razorpay dashboard can get from an order back to
    the AgentRank row, which an opaque digest receipt alone does not allow. Identifiers only.
    Nothing secret, nothing a caller supplied and nothing about a buyer goes in here.
    """

    amount_minor: int
    currency: str
    receipt: str
    notes: Mapping[str, str]

    def __post_init__(self) -> None:
        if self.amount_minor <= 0:
            raise ValueError(f"a razorpay order amount must be positive, got {self.amount_minor}")
        if len(self.currency) != 3 or not self.currency.isupper():
            raise ValueError(f"a razorpay order currency must be ISO 4217, got {self.currency!r}")
        if not self.receipt or len(self.receipt) > MAX_RECEIPT_LENGTH:
            raise ValueError(
                f"a razorpay receipt is 1 to {MAX_RECEIPT_LENGTH} characters,"
                f" got {len(self.receipt)}"
            )
        if len(self.notes) > MAX_NOTES:
            raise ValueError(f"razorpay accepts at most {MAX_NOTES} notes")
        for key, value in self.notes.items():
            if len(key) > MAX_NOTE_LENGTH or len(value) > MAX_NOTE_LENGTH:
                raise ValueError(f"a razorpay note is at most {MAX_NOTE_LENGTH} characters")

    def to_payload(self) -> dict[str, Any]:
        """The request body, in Razorpay's documented parameter names."""
        return {
            "amount": self.amount_minor,
            "currency": self.currency,
            "receipt": self.receipt,
            "notes": dict(self.notes),
        }


@dataclass(frozen=True, slots=True)
class RazorpayOrder:
    """One order as Razorpay reports it.

    `status` is the vendor string: `created` until a payment is attempted, `attempted` while one
    or more attempts exist without a captured one, `paid` once a payment is captured. Nothing in
    this application branches on it directly.

    `amount_paid_minor` is carried because it is the order level statement of how much has
    actually been captured, which is a useful second opinion beside the payment entity and is
    worth having in the audit trail.
    """

    id: str
    amount_minor: int
    amount_paid_minor: int
    currency: str
    receipt: str | None
    status: str
    attempts: int

    @classmethod
    def parse(cls, body: object) -> Self:
        fields = _object(body, "order")
        return cls(
            id=_text(fields, "id"),
            amount_minor=_integer(fields, "amount"),
            amount_paid_minor=_integer(fields, "amount_paid", default=0),
            currency=_text(fields, "currency"),
            receipt=_optional_text(fields, "receipt"),
            status=_text(fields, "status"),
            attempts=_integer(fields, "attempts", default=0),
        )


@dataclass(frozen=True, slots=True)
class RazorpayPayment:
    """One payment as Razorpay reports it.

    `status` is the vendor string: `created`, `authorized`, `captured`, `refunded` or `failed`.
    The mapping onto what AgentRank may conclude lives in one place and is not this one.

    `captured` is carried beside the status even though the two agree in every documented case,
    because they are separately stated by the vendor and an integration that silently preferred
    one would be choosing which of two disagreeing facts to believe without saying so.

    `order_id` is what ties a payment back to the order this application created. It is checked
    against the stored order identifier rather than against anything a browser sent.
    """

    id: str
    order_id: str | None
    amount_minor: int
    currency: str
    status: str
    captured: bool
    method: str | None
    error_code: str | None
    error_description: str | None

    @classmethod
    def parse(cls, body: object) -> Self:
        fields = _object(body, "payment")
        return cls(
            id=_text(fields, "id"),
            order_id=_optional_text(fields, "order_id"),
            amount_minor=_integer(fields, "amount"),
            currency=_text(fields, "currency"),
            status=_text(fields, "status"),
            captured=_flag(fields, "captured"),
            method=_optional_text(fields, "method"),
            error_code=_optional_text(fields, "error_code"),
            error_description=_optional_text(fields, "error_description"),
        )


def parse_collection(body: object, entity: str) -> list[dict[str, Any]]:
    """The `items` of a Razorpay collection envelope, or a refusal to guess.

    Razorpay wraps every list in `{entity: "collection", count, items}`. An envelope without a
    list under `items` is not an empty result, it is a response this application does not
    understand, and treating the two the same is how "no payments on this order" comes to mean
    "the shape changed".
    """
    fields = _object(body, f"{entity} collection")
    items = fields.get("items")
    if not isinstance(items, list):
        raise RazorpayUnreadableError(f"a razorpay {entity} collection has no items list")
    return [_object(item, entity) for item in items]


def _object(value: object, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RazorpayUnreadableError(f"a razorpay {what} must be a JSON object, got {type(value)}")
    return value


def _text(fields: Mapping[str, Any], key: str) -> str:
    value = fields.get(key)
    if not isinstance(value, str) or not value:
        raise RazorpayUnreadableError(f"razorpay field {key!r} must be a non empty string")
    return value


def _optional_text(fields: Mapping[str, Any], key: str) -> str | None:
    value = fields.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RazorpayUnreadableError(f"razorpay field {key!r} must be a string or null")
    return value


def _integer(fields: Mapping[str, Any], key: str, *, default: int | None = None) -> int:
    value = fields.get(key)
    if value is None and default is not None:
        return default
    # bool is a subclass of int in Python, and a boolean amount is not an amount.
    if not isinstance(value, int) or isinstance(value, bool):
        raise RazorpayUnreadableError(f"razorpay field {key!r} must be an integer")
    return value


def _flag(fields: Mapping[str, Any], key: str) -> bool:
    value = fields.get(key)
    if not isinstance(value, bool):
        raise RazorpayUnreadableError(f"razorpay field {key!r} must be a boolean")
    return value
