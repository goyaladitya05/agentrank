"""The exact JSON language an isolated buyer is allowed to speak.

These are schema tests, not evaluator tests. A future report field that could carry a commerce
fact must fail here before it can become a new way for an executor to state its own truth.
"""

import uuid

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
