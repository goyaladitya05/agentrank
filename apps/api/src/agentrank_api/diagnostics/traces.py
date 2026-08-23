"""Structured facts extracted from an LLM mission's trusted trace evidence.

Trace events are trusted runtime records written by the worker boundary, never by the model.
This module reads them back into the small set of facts diagnostics need, and it exists so
that the difference between two things that look identical in a failure list stays visible:

a provider throttle that was retried and recovered is operational history inside a mission
whose outcome means whatever it means, while a provider failure that terminated the mission
is why the mission has no meaningful outcome at all. Both leave `PROVIDER_ERROR` rows. Only
one of them should ever reach a merchant as the reason a mission produced nothing, and only
one of them may never be read as merchant or buyer failure.

The extraction is deliberately narrow. It answers five questions and nothing else: did the
mission end on a provider failure, were there throttles that recovered, which models actually
answered, how much interaction did the mission cost, and how did the buyer stop. It reads no
model prose: payload text fields such as a response body or a refusal message are ignored
entirely, because a model's account of its own situation is exactly what this layer must not
classify from.

Pure domain code over plain records, so it can be tested without a database and used against
any source of persisted trace rows.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TraceEventRecord:
    """One persisted trace event as this module reads it.

    `identifier` carries the row identity when there is one, so a diagnosis can cite the
    concrete evidence rather than describing it from memory.
    """

    identifier: str | None
    event_type: str
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ProviderUsageRecord:
    """One persisted provider invocation as this module reads it."""

    identifier: str | None
    requested_model: str | None
    actual_model: str | None


# The event types this module distinguishes. They mirror `AgentTraceEventType`; they are
# restated as plain strings so this module stays importable by anything holding rows.
_ABORT_EVENT = "AGENT_ABORT"
_FINAL_EVENT = "AGENT_FINAL"
_PROVIDER_ERROR_EVENT = "PROVIDER_ERROR"
_TOOL_CALL_EVENT = "TOOL_CALL"
_TOOL_ERROR_EVENT = "TOOL_ERROR"
_MODEL_REQUEST_EVENT = "MODEL_REQUEST"

# The one abort reason that means the model provider failed the mission. Every other abort
# reason is buyer behavior or harness machinery and keeps its existing attribution.
_PROVIDER_UNAVAILABLE = "provider_unavailable"

# The error kinds the buyer runtime distinguishes. `ProviderThrottledError` is retryable and
# every other kind ends the mission on its first occurrence.
_THROTTLED_KIND = "ProviderThrottledError"


@dataclass(frozen=True, slots=True)
class ProviderFaultFacts:
    """What the trace establishes about provider failures inside one mission."""

    outage_terminated_mission: bool
    terminating_kind: str | None
    terminating_detail: str | None
    terminating_event_id: str | None
    throttles_recovered: int


@dataclass(frozen=True, slots=True)
class InteractionSummary:
    """How much interaction one mission cost, as observed at trusted boundaries.

    Model invocations count provider round trips including throttle retries, because a retry
    is real interaction cost even though the buyer loop does not charge it a turn. Token
    totals are deliberately absent here: unreported token counts stay null upstream and are
    never presented as zero, so they are summarized where nulls can survive, not here.
    """

    model_invocations: int
    tool_calls: int
    tool_errors: int


@dataclass(frozen=True, slots=True)
class TraceFacts:
    """Everything diagnostics conclude from one mission's trace, and no more."""

    interactions: InteractionSummary
    provider_faults: ProviderFaultFacts | None
    requested_model: str | None
    resolved_models: tuple[str, ...]
    final_reason: str | None
    abort_reason: str | None

    @property
    def resolved_model_matches_request(self) -> bool:
        if self.requested_model is None:
            return True
        return all(model == self.requested_model for model in self.resolved_models)


def trace_facts(
    events: Iterable[TraceEventRecord],
    usages: Iterable[ProviderUsageRecord] = (),
) -> TraceFacts:
    """Read one mission's persisted trace into diagnostic facts.

    Events must arrive in sequence order. Usages carry one entry per provider invocation.
    """
    ordered = list(events)
    usage_records = list(usages)
    provider_errors = [event for event in ordered if event.event_type == _PROVIDER_ERROR_EVENT]
    abort_reason = _last_field(ordered, _ABORT_EVENT, "reason")
    final_reason = _last_field(ordered, _FINAL_EVENT, "reason")

    # The abort reason is written by the buyer runtime at the moment it gave up, so it is
    # trusted evidence of why the mission ended even when no failure detail was recorded.
    outage_terminated = abort_reason == _PROVIDER_UNAVAILABLE
    faults: ProviderFaultFacts | None = None
    if outage_terminated:
        # Every recorded provider failure belongs to the outage that ended the mission,
        # including throttles that were retried first: none of them recovered, because the
        # mission did not survive them.
        last = provider_errors[-1] if provider_errors else None
        faults = ProviderFaultFacts(
            outage_terminated_mission=True,
            terminating_kind=None if last is None else _text(last.payload.get("kind")),
            terminating_detail=None if last is None else _bounded(last.payload.get("detail")),
            terminating_event_id=None if last is None else last.identifier,
            throttles_recovered=0,
        )
    elif provider_errors:
        recovered = sum(
            1 for event in provider_errors if event.payload.get("kind") == _THROTTLED_KIND
        )
        faults = ProviderFaultFacts(
            outage_terminated_mission=False,
            terminating_kind=None,
            terminating_detail=None,
            terminating_event_id=None,
            throttles_recovered=recovered,
        )

    interactions = InteractionSummary(
        # Requests rather than responses: every provider round trip records a request,
        # including the attempts a throttle retry replaced and the ones that never answered.
        model_invocations=sum(1 for event in ordered if event.event_type == _MODEL_REQUEST_EVENT),
        tool_calls=sum(1 for event in ordered if event.event_type == _TOOL_CALL_EVENT),
        tool_errors=sum(1 for event in ordered if event.event_type == _TOOL_ERROR_EVENT),
    )
    return TraceFacts(
        interactions=interactions,
        provider_faults=faults,
        requested_model=_single_model(usage.requested_model for usage in usage_records),
        resolved_models=_single_models(usage.actual_model for usage in usage_records),
        final_reason=final_reason,
        abort_reason=abort_reason,
    )


def _single_model(models: Iterable[str | None]) -> str | None:
    distinct = sorted({model for model in models if model})
    return distinct[0] if len(distinct) == 1 else None


def _single_models(models: Iterable[str | None]) -> tuple[str, ...]:
    return tuple(sorted({model for model in models if model}))


def _last_field(events: list[TraceEventRecord], kind: str, field_name: str) -> str | None:
    for event in reversed(events):
        if event.event_type == kind:
            return _text(event.payload.get(field_name))
    return None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _bounded(value: object) -> str | None:
    text = _text(value)
    return text[:200] if text is not None else None
