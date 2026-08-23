"""Trace extraction and trace enriched mission diagnosis.

The provider fault rules here are the diagnostics layer's half of the Phase 3D attribution
fix: a throttled provider invocation that recovered is history, and a provider failure that
terminated a mission is never a merchant or buyer finding. The tests build traces by hand in
the exact payload shapes the buyer runtime persists, so a change to those shapes fails here
rather than silently weakening a diagnosis.
"""

import uuid

from agentrank_api.benchmark.definitions import ExpectedOutcome
from agentrank_api.benchmark.failures import FailureReason
from agentrank_api.benchmark.lifecycle import MissionRunStatus
from agentrank_api.diagnostics.codes import (
    Actionability,
    DiagnosticCode,
    DiagnosticOwner,
)
from agentrank_api.diagnostics.mission import (
    MissionDiagnosisInput,
    diagnose_mission,
)
from agentrank_api.diagnostics.traces import (
    ProviderUsageRecord,
    TraceEventRecord,
    trace_facts,
)

RUN = uuid.uuid4()
MISSION_RUN = uuid.uuid4()


def model_request(turn: int) -> TraceEventRecord:
    return TraceEventRecord(None, "MODEL_REQUEST", {"invocation_sequence": turn, "input": []})


def model_response(turn: int) -> TraceEventRecord:
    return TraceEventRecord(
        None,
        "MODEL_RESPONSE",
        {"invocation_sequence": turn, "model": "gemini-x", "status": "completed"},
    )


def throttle_event(turn: int, attempt: int = 1) -> TraceEventRecord:
    return TraceEventRecord(
        f"trace-{turn}-{attempt}",
        "PROVIDER_ERROR",
        {
            "invocation_sequence": turn,
            "kind": "ProviderThrottledError",
            "detail": "rate limited",
            "attempt": attempt,
            "retry_after_seconds": 2.0,
        },
    )


def abort_event(reason: str) -> TraceEventRecord:
    return TraceEventRecord("trace-abort", "AGENT_ABORT", {"reason": reason, "turn": 1})


def usage(model: str = "gemini-x", requested: str = "gemini-x") -> ProviderUsageRecord:
    return ProviderUsageRecord("usage-1", requested, model)


def evidence(**overrides: object) -> MissionDiagnosisInput:
    fields: dict[str, object] = {
        "run_id": RUN,
        "mission_run_id": MISSION_RUN,
        "mission_key": "buy-a-charger",
        "status": MissionRunStatus.FAILED,
        "expected_outcome": ExpectedOutcome.PURCHASE_AVAILABLE,
        "simulated_value_amount_minor": 499900,
        "currency": "INR",
        "failure_reasons": (FailureReason.AGENT_EXECUTION_ERROR,),
    }
    fields.update(overrides)
    return MissionDiagnosisInput(**fields)  # type: ignore[arg-type]


class TestExtraction:
    def test_terminating_outage_is_recognized_from_the_abort_reason(self) -> None:
        events = [
            model_request(1),
            throttle_event(1),
            throttle_event(1, attempt=2),
            model_response(1),
            TraceEventRecord(
                "trace-fatal", "PROVIDER_ERROR", {"kind": "TimeoutError", "detail": "timed out"}
            ),
            abort_event("provider_unavailable"),
        ]
        facts = trace_facts(events, [usage()])
        assert facts.provider_faults is not None
        assert facts.provider_faults.outage_terminated_mission is True
        # The two throttled attempts were survived: the mission went on long enough to
        # record the response and then a later failure that actually ended it.
        assert facts.provider_faults.throttles_recovered == 2
        assert facts.provider_faults.terminating_kind == "TimeoutError"
        assert facts.provider_faults.terminating_event_id == "trace-fatal"
        assert facts.abort_reason == "provider_unavailable"

    def test_recovered_throttle_is_history_not_a_termination(self) -> None:
        events = [
            model_request(1),
            throttle_event(1),
            model_response(1),
            TraceEventRecord("trace-final", "AGENT_FINAL", {"reason": "structured_abstention"}),
        ]
        facts = trace_facts(events, [usage()])
        assert facts.abort_reason is None
        assert facts.final_reason == "structured_abstention"
        assert facts.provider_faults is not None
        assert facts.provider_faults.outage_terminated_mission is False
        assert facts.provider_faults.throttles_recovered == 1

    def test_interaction_counts_include_throttle_retries(self) -> None:
        events = [
            model_request(1),
            throttle_event(1),
            model_request(1),
            model_response(1),
            TraceEventRecord(None, "TOOL_CALL", {"name": "search_products"}),
            TraceEventRecord(None, "TOOL_RESULT", {"call_id": "c1"}),
            TraceEventRecord(None, "TOOL_CALL", {"name": "no_such_tool"}),
            TraceEventRecord(None, "TOOL_ERROR", {"call_id": "c2"}),
        ]
        facts = trace_facts(events, [])
        assert facts.interactions.model_invocations == 1 + 1  # one retry inside the turn
        assert facts.interactions.tool_calls == 2
        assert facts.interactions.tool_errors == 1

    def test_model_identity_is_read_from_usage_only(self) -> None:
        facts = trace_facts([model_request(1), model_response(1)], [usage("resolved-9")])
        assert facts.requested_model == "gemini-x"
        assert facts.resolved_models == ("resolved-9",)
        assert facts.resolved_model_matches_request is False

    def test_no_traces_produce_no_provider_facts(self) -> None:
        facts = trace_facts([], [])
        assert facts.provider_faults is None
        assert facts.resolved_models == ()
        assert facts.interactions.model_invocations == 0


class TestProviderFaultDiagnosis:
    def test_outage_overrides_the_agent_error_it_wears(self) -> None:
        events = [model_request(1), model_response(1), abort_event("provider_unavailable")]
        diagnosis = diagnose_mission(evidence(trace=trace_facts(events, [usage()])))
        assert diagnosis.primary is not None
        assert diagnosis.primary.code is DiagnosticCode.PROVIDER_OUTAGE_TERMINATED_MISSION
        codes = {finding.code for finding in diagnosis.findings}
        assert DiagnosticCode.AGENT_EXECUTION_FAILURE not in codes
        assert diagnosis.primary.owner is DiagnosticOwner.MODEL_PROVIDER
        assert diagnosis.primary.actionability is Actionability.NO_MERCHANT_ACTION
        assert "model provider" in diagnosis.outcome

    def test_recovered_throttle_stays_secondary_beside_a_real_outcome(self) -> None:
        events = [
            model_request(1),
            throttle_event(1),
            model_response(1),
            abort_event("turn_budget_exceeded"),
        ]
        diagnosis = diagnose_mission(
            evidence(trace=trace_facts(events, [usage()]), failure_reasons=())
        )
        codes = {finding.code for finding in diagnosis.findings}
        assert DiagnosticCode.PROVIDER_THROTTLE_RECOVERED in codes
        assert DiagnosticCode.PROVIDER_OUTAGE_TERMINATED_MISSION not in codes
        recovered = next(
            finding
            for finding in diagnosis.findings
            if finding.code is DiagnosticCode.PROVIDER_THROTTLE_RECOVERED
        )
        # A secondary only observation never leads a diagnosis on its own.
        assert diagnosis.primary is None
        assert "throttled 1" in recovered.summary

    def test_recovered_throttle_sits_beside_a_leading_finding(self) -> None:
        events = [
            model_request(1),
            throttle_event(1),
            model_response(1),
            abort_event("turn_budget_exceeded"),
        ]
        diagnosis = diagnose_mission(
            evidence(
                trace=trace_facts(events, [usage()]),
                failure_reasons=(FailureReason.INVENTORY_UNAVAILABLE,),
            )
        )
        assert diagnosis.primary is not None
        assert diagnosis.primary.code is DiagnosticCode.STOCK_UNAVAILABLE
        assert DiagnosticCode.PROVIDER_THROTTLE_RECOVERED in {
            finding.code for finding in diagnosis.findings
        }

    def test_resolved_model_mismatch_is_reported_as_a_qualification(self) -> None:
        events = [model_request(1), model_response(1)]
        diagnosis = diagnose_mission(
            evidence(
                status=MissionRunStatus.SUCCEEDED,
                failure_reasons=(),
                selected_quantity=1,
                trace=trace_facts(events, [usage("something-else")]),
            )
        )
        mismatch = next(
            finding
            for finding in diagnosis.findings
            if finding.code is DiagnosticCode.RESOLVED_MODEL_MISMATCH
        )
        assert mismatch.owner is DiagnosticOwner.MODEL_PROVIDER
        # A qualification beside a success, never replacing it.
        assert diagnosis.primary is None or (
            diagnosis.primary.code is not DiagnosticCode.RESOLVED_MODEL_MISMATCH
        )

    def test_success_with_matching_model_stays_clean(self) -> None:
        events = [model_request(1), model_response(1)]
        diagnosis = diagnose_mission(
            evidence(
                status=MissionRunStatus.SUCCEEDED,
                failure_reasons=(),
                selected_quantity=1,
                trace=trace_facts(events, [usage("gemini-x", "gemini-x")]),
            )
        )
        assert diagnosis.findings == ()

    def test_diagnosis_exposes_interaction_summary(self) -> None:
        events = [model_request(1), model_response(1), abort_event("deadline_exceeded")]
        diagnosis = diagnose_mission(evidence(trace=trace_facts(events, [usage()])))
        assert diagnosis.interactions is not None
        assert diagnosis.interactions.model_invocations == 1

    def test_reference_executor_style_input_has_no_interactions(self) -> None:
        diagnosis = diagnose_mission(evidence(failure_reasons=(FailureReason.DISCOVERY_FAILURE,)))
        assert diagnosis.interactions is None
        assert all(
            finding.code is not DiagnosticCode.PROVIDER_THROTTLE_RECOVERED
            for finding in diagnosis.findings
        )
