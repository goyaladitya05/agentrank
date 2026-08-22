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

What comes back is an `ExecutorReport` and nothing else: which variant it selected, which quote
it created, which payment it dispatched, whether it declined, and what stopped it. There is no
field for a status, no field for a failure reason, no field for an error origin, and since Phase
2B-R2 no field for a price, a quoted total, an authorization decision or a payment status either.
A worker cannot mark its own mission, cannot say whose fault an interruption was, and cannot
state a commerce fact. What those identifiers came to is established on the trusted side from the
merchant's own rows, and attribution is decided from the process exit, the transport and the tool
boundary.

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
from agentrank_api.benchmark.report import (
    AbstentionCode,
    CheckoutRefusal,
    ExecutorReport,
    ReportedAbstention,
    ReportedCheckout,
    ReportedError,
    ReportedPayment,
    ReportedSelection,
)

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


def report_payload(report: ExecutorReport) -> dict[str, Any]:
    """One executor's report, as the only thing a worker is allowed to say."""
    return {"protocol": PROTOCOL_VERSION, "observed": executor_report_payload(report)}


def report_from_payload(payload: Any) -> ExecutorReport:
    """Read a worker's report, refusing anything that is not one."""
    document = _document(payload)
    return executor_report_from_payload(_object(document, "observed"))


def executor_report_payload(report: ExecutorReport) -> dict[str, Any]:
    """An `ExecutorReport` as plain JSON.

    Written out field by field rather than reflected over, so that a field added to the report
    model has to be placed here before it can cross the boundary. A serializer that walked the
    dataclass would carry a new field silently, and what a worker may say is exactly the question
    this boundary exists to answer.
    """
    return {
        "merchant_id": str(report.merchant_id),
        "selection": (
            None
            if report.selection is None
            else {
                "variant_id": str(report.selection.variant_id),
                "quantity": report.selection.quantity,
            }
        ),
        "checkout": (
            None
            if report.checkout is None
            else {
                "checkout_id": _optional_identifier(report.checkout.checkout_id),
                "refusal": (
                    None if report.checkout.refusal is None else report.checkout.refusal.value
                ),
            }
        ),
        "payment": (
            None if report.payment is None else {"attempt_id": str(report.payment.attempt_id)}
        ),
        "abstention": (
            None
            if report.abstention is None
            else {
                "code": report.abstention.code.value,
                "detail": report.abstention.detail,
            }
        ),
        "error": None if report.error is None else {"detail": report.error.detail},
    }


def executor_report_from_payload(payload: Any) -> ExecutorReport:
    """Rebuild an `ExecutorReport`, through the same constructors the executor used.

    Every part validates itself on the way in, exactly as it does in process, so a worker cannot
    put a report across this boundary that it could not have constructed on its own side.
    """
    document = _document(payload)
    return ExecutorReport(
        merchant_id=_identifier(document, "merchant_id"),
        selection=_read(document, "selection", _selection_from),
        checkout=_read(document, "checkout", _checkout_from),
        payment=_read(document, "payment", _payment_from),
        abstention=_read(document, "abstention", _abstention_from),
        error=_read(document, "error", _error_from),
    )


def _selection_from(document: dict[str, Any]) -> ReportedSelection:
    return ReportedSelection(
        variant_id=_identifier(document, "variant_id"),
        quantity=_integer(document, "quantity"),
    )


def _checkout_from(document: dict[str, Any]) -> ReportedCheckout:
    refusal = _optional_text(document, "refusal")
    return ReportedCheckout(
        checkout_id=_optional_id(document, "checkout_id"),
        refusal=None if refusal is None else _member(CheckoutRefusal, refusal),
    )


def _payment_from(document: dict[str, Any]) -> ReportedPayment:
    return ReportedPayment(attempt_id=_identifier(document, "attempt_id"))


def _abstention_from(document: dict[str, Any]) -> ReportedAbstention:
    return ReportedAbstention(
        code=_member(AbstentionCode, _text(document, "code")),
        detail=_optional_text(document, "detail"),
    )


def _error_from(document: dict[str, Any]) -> ReportedError:
    return ReportedError(detail=_text(document, "detail"))


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
