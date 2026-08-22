"""What crosses the boundary between a benchmark runner and an executor in another process.

Two documents and a version, and nothing else is ever sent. Pure functions over plain JSON with
no SQLAlchemy, no service and no model, so this module can be imported on either side without
either side gaining a route to anything the other has.

What the runner sends is a `MissionRequest`. It is the smallest thing a buyer needs to shop:

```text
brief         the mission, as a buyer is allowed to see it
merchant_id   which shop
base_url      where that shop's commerce API is
token         one merchant credential, scoped to that shop, revoked when the run ends
strategy      which buyer to be
```

What is deliberately not in it is the whole point of the boundary. No database URL, no session,
no suite, no run identifier, no other mission, no expected outcome, no simulated value, no
evaluator, no payment provider secret and nothing about what any earlier mission did. There is no
field here that could carry one, so an executor process cannot be handed the oracle by a caller
that meant well and got the arguments wrong.

What comes back is a `MissionReport`: an `ObservedResult` and nothing else. There is no field for
a status, no field for a failure reason and no field for an error origin, so a worker cannot
mark its own mission and cannot say whose fault an interruption was. Attribution is decided on
the trusted side from the process exit, the transport and the tool boundary.

The token travels inside the request document on stdin rather than in the environment or on the
command line. Both of those are readable by any process the same user runs, and a benchmark that
put a credential in an argument vector would have published it to `ps`.

`PROTOCOL_VERSION` is checked on both sides. A worker from a different build answering a runner
from this one is a category of confusion worth refusing outright rather than diagnosing from a
missing key.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Self

from agentrank_api.benchmark.definitions import AgentMissionBrief
from agentrank_api.benchmark.observation import (
    AbstentionCode,
    CheckoutRefusal,
    ObservedAbstention,
    ObservedAuthorization,
    ObservedCheckout,
    ObservedError,
    ObservedPayment,
    ObservedResult,
    ObservedSelection,
)
from agentrank_api.payments.models import PaymentAttemptStatus

PROTOCOL_VERSION = 1

# The one buyer that exists. A second becomes another entry here and a branch in the worker, and
# an unknown name is refused rather than defaulted, because defaulting would silently run a
# different buyer than the run's executor identity claims.
REFERENCE_STRATEGY = "reference"

# What stands in for a credential anywhere a request might be written down.
REDACTED = "redacted"
STRATEGIES = frozenset({REFERENCE_STRATEGY})


class ProtocolError(ValueError):
    """A document that is not the one this version of the protocol describes.

    Its own class so that the runner can tell a worker which spoke nonsense from a worker which
    reported a mission. The first is a fault on the harness side and the second is a result.
    """


@dataclass(frozen=True, slots=True)
class MissionRequest:
    """One mission, and the capability to carry it out. Nothing else."""

    brief: AgentMissionBrief
    merchant_id: uuid.UUID
    base_url: str
    token: str
    strategy: str = REFERENCE_STRATEGY

    def __post_init__(self) -> None:
        if self.strategy not in STRATEGIES:
            raise ValueError(f"unknown buyer strategy {self.strategy!r}")
        if not self.base_url.strip():
            raise ValueError("a mission request names where the merchant's API is")
        if not self.token.strip():
            raise ValueError("a mission request carries the credential it will present")

    def to_payload(self) -> dict[str, Any]:
        return {
            "protocol": PROTOCOL_VERSION,
            "strategy": self.strategy,
            "merchant_id": str(self.merchant_id),
            "base_url": self.base_url,
            "token": self.token,
            "brief": self.brief.to_payload(),
        }

    @classmethod
    def from_payload(cls, payload: Any) -> Self:
        document = _document(payload)
        return cls(
            brief=AgentMissionBrief.from_payload(_object(document, "brief")),
            merchant_id=_identifier(document, "merchant_id"),
            base_url=_text(document, "base_url"),
            token=_text(document, "token"),
            strategy=_text(document, "strategy"),
        )

    def redacted(self) -> dict[str, Any]:
        """The request with the credential removed, for anything that might be written down.

        Not used by the protocol. It exists so that a diagnostic which wants to say what was
        asked for has an obvious thing to reach for that is not the document with the token in
        it.
        """
        payload = self.to_payload()
        payload["token"] = REDACTED
        return payload


def report_payload(observed: ObservedResult) -> dict[str, Any]:
    """One executor's report, as the only thing a worker is allowed to say."""
    return {"protocol": PROTOCOL_VERSION, "observed": observed_payload(observed)}


def report_from_payload(payload: Any) -> ObservedResult:
    """Read a worker's report, refusing anything that is not one."""
    document = _document(payload)
    return observed_from_payload(_object(document, "observed"))


def observed_payload(observed: ObservedResult) -> dict[str, Any]:
    """An `ObservedResult` as plain JSON.

    Written out field by field rather than reflected over, so that a field added to the
    observation model has to be placed here before it can cross the boundary. A serializer that
    walked the dataclass would carry a new field silently, and the fields on that model are
    exactly the ones a benchmark result is built from.
    """
    return {
        "merchant_id": str(observed.merchant_id),
        "selection": None if observed.selection is None else _selection(observed.selection),
        "checkout": None if observed.checkout is None else _checkout(observed.checkout),
        "authorization": (
            None
            if observed.authorization is None
            else {
                "allowed": observed.authorization.allowed,
                "violations": list(observed.authorization.violations),
            }
        ),
        "payment": (
            None
            if observed.payment is None
            else {
                "status": observed.payment.status.value,
                "attempt_id": _optional_identifier(observed.payment.attempt_id),
            }
        ),
        "abstention": (
            None
            if observed.abstention is None
            else {
                "code": observed.abstention.code.value,
                "detail": observed.abstention.detail,
            }
        ),
        "error": None if observed.error is None else {"detail": observed.error.detail},
    }


def observed_from_payload(payload: Any) -> ObservedResult:
    """Rebuild an `ObservedResult`, through the same constructors the executor used.

    Every part validates itself on the way in, exactly as it does in process, so a worker cannot
    put a report across this boundary that it could not have constructed on its own side. A
    payment claiming success with no attempt identifier is refused here for the same reason it is
    refused there: that is the most consequential claim an executor makes.
    """
    document = _document(payload)
    return ObservedResult(
        merchant_id=_identifier(document, "merchant_id"),
        selection=_read(document, "selection", _selection_from),
        checkout=_read(document, "checkout", _checkout_from),
        authorization=_read(document, "authorization", _authorization_from),
        payment=_read(document, "payment", _payment_from),
        abstention=_read(document, "abstention", _abstention_from),
        error=_read(document, "error", _error_from),
    )


def _selection(selection: ObservedSelection) -> dict[str, Any]:
    return {
        "variant_id": str(selection.variant_id),
        "quantity": selection.quantity,
        "unit_price_amount_minor": selection.unit_price_amount_minor,
        "currency": selection.currency,
        "product_category": selection.product_category,
        "variant_attributes": dict(selection.variant_attributes),
    }


def _checkout(checkout: ObservedCheckout) -> dict[str, Any]:
    return {
        "created": checkout.created,
        "checkout_id": _optional_identifier(checkout.checkout_id),
        "total_amount_minor": checkout.total_amount_minor,
        "currency": checkout.currency,
        "refusal": None if checkout.refusal is None else checkout.refusal.value,
    }


def _selection_from(document: dict[str, Any]) -> ObservedSelection:
    attributes = document.get("variant_attributes") or {}
    if not isinstance(attributes, dict):
        raise ProtocolError("variant_attributes must be an object")
    return ObservedSelection(
        variant_id=_identifier(document, "variant_id"),
        quantity=_integer(document, "quantity"),
        unit_price_amount_minor=_integer(document, "unit_price_amount_minor"),
        currency=_text(document, "currency"),
        product_category=_optional_text(document, "product_category"),
        variant_attributes=attributes,
    )


def _checkout_from(document: dict[str, Any]) -> ObservedCheckout:
    refusal = _optional_text(document, "refusal")
    return ObservedCheckout(
        created=_boolean(document, "created"),
        checkout_id=_optional_id(document, "checkout_id"),
        total_amount_minor=_optional_integer(document, "total_amount_minor"),
        currency=_optional_text(document, "currency"),
        refusal=None if refusal is None else _member(CheckoutRefusal, refusal),
    )


def _authorization_from(document: dict[str, Any]) -> ObservedAuthorization:
    violations = document.get("violations") or []
    if not isinstance(violations, list):
        raise ProtocolError("violations must be an array")
    return ObservedAuthorization(
        allowed=_boolean(document, "allowed"),
        violations=tuple(str(violation) for violation in violations),
    )


def _payment_from(document: dict[str, Any]) -> ObservedPayment:
    return ObservedPayment(
        status=_member(PaymentAttemptStatus, _text(document, "status")),
        attempt_id=_optional_id(document, "attempt_id"),
    )


def _abstention_from(document: dict[str, Any]) -> ObservedAbstention:
    return ObservedAbstention(
        code=_member(AbstentionCode, _text(document, "code")),
        detail=_optional_text(document, "detail"),
    )


def _error_from(document: dict[str, Any]) -> ObservedError:
    return ObservedError(detail=_text(document, "detail"))


def _document(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ProtocolError("a protocol document is a JSON object")
    version = payload.get("protocol")
    if version is not None and version != PROTOCOL_VERSION:
        raise ProtocolError(f"protocol version {version!r} is not {PROTOCOL_VERSION}")
    return payload


def _object(document: dict[str, Any], name: str) -> dict[str, Any]:
    value = document.get(name)
    if not isinstance(value, dict):
        raise ProtocolError(f"{name} must be an object")
    return value


def _read[T](document: dict[str, Any], name: str, build: Callable[[dict[str, Any]], T]) -> T | None:
    value = document.get(name)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ProtocolError(f"{name} must be an object or null")
    return build(value)


def _text(document: dict[str, Any], name: str) -> str:
    value = document.get(name)
    if not isinstance(value, str):
        raise ProtocolError(f"{name} must be a string")
    return value


def _optional_text(document: dict[str, Any], name: str) -> str | None:
    value = document.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProtocolError(f"{name} must be a string or null")
    return value


def _integer(document: dict[str, Any], name: str) -> int:
    value = document.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProtocolError(f"{name} must be an integer")
    return value


def _optional_integer(document: dict[str, Any], name: str) -> int | None:
    if document.get(name) is None:
        return None
    return _integer(document, name)


def _boolean(document: dict[str, Any], name: str) -> bool:
    value = document.get(name)
    if not isinstance(value, bool):
        raise ProtocolError(f"{name} must be a boolean")
    return value


def _identifier(document: dict[str, Any], name: str) -> uuid.UUID:
    try:
        return uuid.UUID(_text(document, name))
    except ValueError as malformed:
        raise ProtocolError(f"{name} must be a UUID") from malformed


def _optional_id(document: dict[str, Any], name: str) -> uuid.UUID | None:
    if document.get(name) is None:
        return None
    return _identifier(document, name)


def _optional_identifier(value: uuid.UUID | None) -> str | None:
    return None if value is None else str(value)


def _member[T](enumeration: type[T], value: str) -> T:
    try:
        return enumeration(value)  # type: ignore[call-arg]
    except ValueError as unknown:
        raise ProtocolError(f"{value!r} is not a {enumeration.__name__}") from unknown
