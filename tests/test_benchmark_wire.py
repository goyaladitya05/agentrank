"""The exact JSON language an isolated buyer is allowed to speak.

These are schema tests, not evaluator tests. A future report field that could carry a commerce
fact must fail here before it can become a new way for an executor to state its own truth.
"""

import uuid
from typing import cast

import pytest

from agentrank_api.benchmark.report import (
    ExecutorReport,
    ReportedCheckout,
    ReportedPayment,
    ReportedSelection,
)
from agentrank_api.benchmark.wire import ProtocolError, report_from_payload, report_payload


def test_an_executor_report_has_only_identifiers_and_actions() -> None:
    """Freeze the model-facing shape rather than relying on a prose rule beside it."""
    assert set(ExecutorReport.__dataclass_fields__) == {
        "merchant_id",
        "selection",
        "checkout",
        "payment",
        "abstention",
        "error",
    }
    assert set(ReportedSelection.__dataclass_fields__) == {"variant_id", "quantity"}
    assert set(ReportedCheckout.__dataclass_fields__) == {"checkout_id", "refusal"}
    assert set(ReportedPayment.__dataclass_fields__) == {"attempt_id"}


@pytest.mark.parametrize(
    ("location", "claim"),
    [
        ("observed", ("quoted_total", 1)),
        ("observed", ("currency", "INR")),
        ("observed", ("authorized", True)),
        ("observed", ("payment_status", "SUCCEEDED")),
        ("selection", ("attributes", {"color": "forged"})),
    ],
)
def test_the_worker_protocol_refuses_forged_commerce_facts(
    location: str, claim: tuple[str, object]
) -> None:
    """A worker cannot even send a fact that trusted orchestration must establish.

    Rejection is deliberate rather than silent dropping: a protocol expansion needs an explicit
    review, and an old runner never accidentally treats a new worker claim as a known field.
    """
    report = ExecutorReport(
        merchant_id=uuid.uuid7(),
        selection=ReportedSelection(variant_id=uuid.uuid7(), quantity=1),
    )
    payload = report_payload(report)
    target = payload["observed"] if location == "observed" else payload["observed"]["selection"]
    assert isinstance(target, dict)
    target[claim[0]] = claim[1]

    with pytest.raises(ProtocolError, match="unknown fields"):
        report_from_payload(payload)


def _llm_request_kwargs() -> dict[str, object]:
    from benchmark_support import brief

    from agentrank_api.benchmark.llm import AgentConfiguration

    document: dict[str, object] = {
        "brief": brief(),
        "merchant_id": uuid.uuid7(),
        "base_url": "http://127.0.0.1:1",
        "token": "token",
        "strategy": "llm",
        "mandate_id": uuid.uuid7(),
        "agent_configuration": AgentConfiguration(
            provider="openai-responses", requested_model="test-model"
        ).payload(),
        "merchant_information": {"products": []},
        "discovery": {"kind": "STOREFRONT"},
    }
    return document


def test_an_llm_request_without_its_discovery_view_is_refused() -> None:
    from agentrank_api.benchmark.wire import MissionRequest

    arguments = _llm_request_kwargs()
    del arguments["discovery"]
    with pytest.raises(ValueError, match="discovery view"):
        MissionRequest(**arguments)  # type: ignore[arg-type]


def test_a_discovery_view_that_this_build_cannot_interpret_is_refused() -> None:
    from agentrank_api.benchmark.wire import MissionRequest

    for malformed in (
        {"kind": "SOMETHING_ELSE"},
        {"kind": "AGENT_READY", "representation_id": "not-a-uuid", "attributes": []},
    ):
        with pytest.raises(ValueError):
            MissionRequest(**{**_llm_request_kwargs(), "discovery": malformed})  # type: ignore[arg-type]


def test_a_reference_buyer_never_carries_a_discovery_view() -> None:
    from agentrank_api.benchmark.definitions import AgentMissionBrief
    from agentrank_api.benchmark.wire import MissionRequest

    with pytest.raises(ValueError, match="discovery view"):
        MissionRequest(
            brief=cast(AgentMissionBrief, _llm_request_kwargs()["brief"]),
            merchant_id=uuid.uuid7(),
            base_url="http://127.0.0.1:1",
            token="token",
            strategy="reference",
            discovery={"kind": "STOREFRONT"},
        )
