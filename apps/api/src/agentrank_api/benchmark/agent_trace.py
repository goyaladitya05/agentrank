"""Bounded, redacted LLM execution evidence that may cross the worker boundary.

The worker records runtime observations, never benchmark outcomes.  This module is deliberately
free of database imports: the isolated process can construct an evidence document but only trusted
orchestration can bind and persist it to a mission run.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any

MAX_EVENTS = 128
MAX_PAYLOAD_BYTES = 16_384
MAX_TEXT_LENGTH = 2_000
_SECRET_PART = re.compile(r"(authorization|api.?key|token|secret|password)", re.IGNORECASE)
EVENT_TYPES = frozenset(
    {
        "MODEL_REQUEST",
        "MODEL_RESPONSE",
        "TOOL_CALL",
        "TOOL_RESULT",
        "TOOL_ERROR",
        "AGENT_FINAL",
        "AGENT_ABORT",
        "PROVIDER_ERROR",
    }
)


def safe_payload(value: Any) -> dict[str, Any]:
    """Return JSON-shaped evidence with credentials redacted and size bounded."""
    sanitized = _sanitize(value)
    if not isinstance(sanitized, dict):
        sanitized = {"value": sanitized}
    encoded = json.dumps(sanitized, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if len(encoded.encode()) <= MAX_PAYLOAD_BYTES:
        return sanitized
    return {"truncated": True, "sha256_length": len(encoded), "preview": encoded[:MAX_TEXT_LENGTH]}


def _sanitize(value: Any, *, key: str | None = None) -> Any:
    if key is not None and _SECRET_PART.search(key):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(name)[:128]: _sanitize(item, key=str(name)) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value[:100]]
    if isinstance(value, str):
        return value[:MAX_TEXT_LENGTH]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:MAX_TEXT_LENGTH]


@dataclass(frozen=True, slots=True)
class TraceEvent:
    event_type: str
    payload: dict[str, Any]

    def to_payload(self) -> dict[str, Any]:
        return {"event_type": self.event_type, "payload": safe_payload(self.payload)}


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    invocation_sequence: int
    trace_sequence: int
    provider: str
    requested_model: str
    actual_model: str | None
    provider_request_id: str | None
    provider_latency_ms: int | None
    usage: dict[str, Any] | None

    def to_payload(self) -> dict[str, Any]:
        return {
            "invocation_sequence": self.invocation_sequence,
            "trace_sequence": self.trace_sequence,
            "provider": self.provider,
            "requested_model": self.requested_model,
            "actual_model": self.actual_model,
            "provider_request_id": self.provider_request_id,
            "provider_latency_ms": self.provider_latency_ms,
            "usage": safe_payload(self.usage or {}),
        }


@dataclass(slots=True)
class AgentExecutionEvidence:
    events: list[TraceEvent] = field(default_factory=list)
    usages: list[ProviderUsage] = field(default_factory=list)

    def add(self, event_type: str, payload: dict[str, Any]) -> int:
        if len(self.events) >= MAX_EVENTS:
            return len(self.events)
        self.events.append(TraceEvent(event_type, safe_payload(payload)))
        return len(self.events)

    def to_payload(self) -> dict[str, Any]:
        return {
            "events": [event.to_payload() for event in self.events],
            "usages": [usage.to_payload() for usage in self.usages],
        }

    @classmethod
    def from_payload(cls, payload: Any) -> AgentExecutionEvidence:
        if not isinstance(payload, dict) or set(payload) != {"events", "usages"}:
            raise ValueError("agent evidence has invalid fields")
        raw_events, raw_usages = payload["events"], payload["usages"]
        if not isinstance(raw_events, list) or not isinstance(raw_usages, list):
            raise ValueError("agent evidence lists are invalid")
        if len(raw_events) > MAX_EVENTS or len(raw_usages) > MAX_EVENTS:
            raise ValueError("agent evidence exceeds its bound")
        events: list[TraceEvent] = []
        for raw in raw_events:
            if not isinstance(raw, dict) or set(raw) != {"event_type", "payload"}:
                raise ValueError("agent trace event is invalid")
            if raw["event_type"] not in EVENT_TYPES or not isinstance(raw["payload"], dict):
                raise ValueError("agent trace event types are invalid")
            events.append(TraceEvent(raw["event_type"], safe_payload(raw["payload"])))
        usages: list[ProviderUsage] = []
        fields = {
            "invocation_sequence",
            "trace_sequence",
            "provider",
            "requested_model",
            "actual_model",
            "provider_request_id",
            "provider_latency_ms",
            "usage",
        }
        for raw in raw_usages:
            if not isinstance(raw, dict) or set(raw) != fields:
                raise ValueError("agent provider usage is invalid")
            if (
                not isinstance(raw["invocation_sequence"], int)
                or not isinstance(raw["trace_sequence"], int)
                or raw["invocation_sequence"] < 1
                or raw["trace_sequence"] < 1
            ):
                raise ValueError("agent provider usage sequence is invalid")
            if (
                not isinstance(raw["provider"], str)
                or not isinstance(raw["requested_model"], str)
                or len(raw["provider"]) > 64
                or len(raw["requested_model"]) > 128
            ):
                raise ValueError("agent provider identity is invalid")
            if raw["usage"] is not None and not isinstance(raw["usage"], dict):
                raise ValueError("agent provider usage payload is invalid")
            if raw["provider_latency_ms"] is not None and (
                not isinstance(raw["provider_latency_ms"], int) or raw["provider_latency_ms"] < 0
            ):
                raise ValueError("agent provider latency is invalid")
            usages.append(ProviderUsage(**raw))
        if any(usage.trace_sequence > len(events) for usage in usages):
            raise ValueError("agent usage does not name an event")
        if any(events[usage.trace_sequence - 1].event_type != "MODEL_RESPONSE" for usage in usages):
            raise ValueError("agent usage must name a model response")
        return cls(events=events, usages=usages)
