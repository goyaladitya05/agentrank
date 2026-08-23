"""Merchant finding aggregation against hand built diagnoses."""

import uuid

from agentrank_api.benchmark.definitions import ExpectedOutcome
from agentrank_api.benchmark.failures import FailureReason
from agentrank_api.benchmark.lifecycle import MissionRunStatus
from agentrank_api.constraints.rules import ConstraintOperator
from agentrank_api.diagnostics.codes import (
    Actionability,
    DiagnosticCode,
    DiagnosticOwner,
    Severity,
)
from agentrank_api.diagnostics.findings import MerchantFinding, aggregate_findings
from agentrank_api.diagnostics.mission import (
    DEMAND_AT_RISK,
    MissionDiagnosis,
    MissionDiagnosisInput,
    RequiredAttributeFact,
    SelectionFacts,
    diagnose_mission,
)

PRODUCT_A = uuid.uuid4()
PRODUCT_B = uuid.uuid4()
VARIANT_A = uuid.uuid4()
VARIANT_B = uuid.uuid4()


def diagnosis(
    mission_key: str,
    *,
    reasons: tuple[FailureReason, ...] = (),
    status: MissionRunStatus = MissionRunStatus.FAILED,
    value: int = 499900,
    required: tuple[RequiredAttributeFact, ...] = (),
    selection: SelectionFacts | None = None,
    unsafe_attempt: bool = False,
) -> MissionDiagnosis:
    evidence = MissionDiagnosisInput(
        run_id=uuid.uuid4(),
        mission_run_id=uuid.uuid4(),
        mission_key=mission_key,
        status=status,
        expected_outcome=ExpectedOutcome.PURCHASE_AVAILABLE,
        simulated_value_amount_minor=value,
        currency="INR",
        failure_reasons=reasons,
        unsafe_attempt=unsafe_attempt,
        required_attributes=required,
        selection=selection,
    )
    return diagnose_mission(evidence)


class TestGrouping:
    def test_same_code_across_missions_becomes_one_finding(self) -> None:
        diagnoses = [
            diagnosis("m1", reasons=(FailureReason.INVENTORY_UNAVAILABLE,)),
            diagnosis("m2", reasons=(FailureReason.INVENTORY_UNAVAILABLE,)),
            diagnosis("m3", reasons=(FailureReason.INVENTORY_UNAVAILABLE,)),
        ]
        findings = aggregate_findings(diagnoses)
        assert len(findings) == 1
        finding = findings[0]
        assert finding.code is DiagnosticCode.STOCK_UNAVAILABLE
        assert len(finding.mission_run_ids) == 3
        assert tuple(sorted(finding.mission_keys)) == ("m1", "m2", "m3")

    def test_attribute_gaps_group_by_attribute_not_by_mission(self) -> None:
        wattage = RequiredAttributeFact("wattage", ConstraintOperator.GTE, 100)
        color = RequiredAttributeFact("color", ConstraintOperator.EQ, "black")
        diagnoses = [
            diagnosis(
                "m1",
                reasons=(FailureReason.ATTRIBUTE_MISSING,),
                required=(wattage,),
                selection=SelectionFacts(VARIANT_A, product_id=PRODUCT_A, attributes={}),
            ),
            diagnosis(
                "m2",
                reasons=(FailureReason.ATTRIBUTE_MISSING,),
                required=(wattage,),
                selection=SelectionFacts(VARIANT_B, product_id=PRODUCT_B, attributes={}),
            ),
            # No verified attributes for this mission, so its gap stays at reason level.
            diagnosis("m3", reasons=(FailureReason.ATTRIBUTE_MISSING,), required=(color,)),
        ]
        findings = aggregate_findings(diagnoses)
        keys = sorted(finding.key for finding in findings)
        assert keys == [
            DiagnosticCode.ATTRIBUTE_NOT_PUBLISHED.value,
            f"{DiagnosticCode.ATTRIBUTE_NOT_PUBLISHED.value}:wattage",
        ]
        wattage_finding = next(f for f in findings if f.attribute_keys == ("wattage",))
        # Two products are linked only because both missions named the same gap.
        assert set(wattage_finding.product_ids) == {PRODUCT_A, PRODUCT_B}

    def test_unrelated_failures_stay_separate(self) -> None:
        diagnoses = [
            diagnosis("m1", reasons=(FailureReason.INVENTORY_UNAVAILABLE,)),
            diagnosis("m2", reasons=(FailureReason.MANDATE_DENIED,), unsafe_attempt=True),
        ]
        findings = aggregate_findings(diagnoses)
        codes = {finding.code for finding in findings}
        assert DiagnosticCode.STOCK_UNAVAILABLE in codes
        assert DiagnosticCode.UNSAFE_ATTEMPT_BLOCKED in codes


class TestOrderingAndIdentity:
    def test_order_is_severity_then_scope_then_precedence(self) -> None:
        diagnoses = [
            diagnosis("a", reasons=(FailureReason.INVENTORY_UNAVAILABLE,)),
            diagnosis("b", reasons=(FailureReason.INVENTORY_UNAVAILABLE,)),
            diagnosis("c", reasons=(FailureReason.ATTRIBUTE_MISSING,)),
        ]
        findings = aggregate_findings(diagnoses)
        assert [finding.code for finding in findings] == [
            DiagnosticCode.STOCK_UNAVAILABLE,
            DiagnosticCode.ATTRIBUTE_NOT_PUBLISHED,
        ]

    def test_aggregation_is_insertion_independent(self) -> None:
        first = diagnosis("m1", reasons=(FailureReason.INVENTORY_UNAVAILABLE,))
        second = diagnosis("m2", reasons=(FailureReason.DISCOVERY_FAILURE,))
        forward = aggregate_findings([first, second])
        backward = aggregate_findings([second, first])
        assert [f.key for f in forward] == [f.key for f in backward]

    def test_secondary_observations_are_included_without_leading(self) -> None:
        from agentrank_api.diagnostics.mission import MissionDiagnosisInput as Input
        from agentrank_api.diagnostics.traces import TraceEventRecord, trace_facts

        events = [
            TraceEventRecord(None, "MODEL_REQUEST", {}),
            TraceEventRecord("t1", "PROVIDER_ERROR", {"kind": "ProviderThrottledError"}),
            TraceEventRecord(None, "MODEL_RESPONSE", {}),
        ]
        evidence = Input(
            run_id=uuid.uuid4(),
            mission_run_id=uuid.uuid4(),
            mission_key="m1",
            status=MissionRunStatus.FAILED,
            expected_outcome=ExpectedOutcome.PURCHASE_AVAILABLE,
            simulated_value_amount_minor=499900,
            currency="INR",
            failure_reasons=(FailureReason.INVENTORY_UNAVAILABLE,),
            trace=trace_facts(events, []),
        )
        findings = aggregate_findings([diagnose_mission(evidence)])
        codes = {finding.code for finding in findings}
        assert DiagnosticCode.PROVIDER_THROTTLE_RECOVERED in codes


class TestDemandAttribution:
    def test_demand_follows_the_primary_diagnosis_once(self) -> None:
        diagnoses = [
            diagnosis("m1", reasons=(FailureReason.INVENTORY_UNAVAILABLE,), value=1000),
            diagnosis(
                "m2",
                reasons=(
                    FailureReason.INVENTORY_UNAVAILABLE,
                    FailureReason.AGENT_EXECUTION_ERROR,
                ),
                value=2000,
            ),
        ]
        findings = aggregate_findings(diagnoses)
        stock = next(
            finding for finding in findings if finding.code is DiagnosticCode.STOCK_UNAVAILABLE
        )
        # Mission m1 leads with the stock failure; m2 leads with the agent execution
        # failure that outranks it, so each mission's value bills exactly once, apart.
        assert (stock.simulated_demand[0].currency, stock.simulated_demand[0].bucket) == (
            "INR",
            DEMAND_AT_RISK,
        )
        assert stock.simulated_demand[0].amount_minor == 1000
        execution = next(
            finding
            for finding in findings
            if finding.code is DiagnosticCode.AGENT_EXECUTION_FAILURE
        )
        assert execution.simulated_demand[0].amount_minor == 2000

    def test_demand_never_sums_across_currencies(self) -> None:
        diagnoses = [
            diagnosis("m1", reasons=(FailureReason.INVENTORY_UNAVAILABLE,), value=1000),
            diagnosis(
                "inr-mission",
                reasons=(FailureReason.INVENTORY_UNAVAILABLE,),
                value=500,
            ),
        ]
        eur_evidence = MissionDiagnosisInput(
            run_id=uuid.uuid4(),
            mission_run_id=uuid.uuid4(),
            mission_key="eur-mission",
            status=MissionRunStatus.FAILED,
            expected_outcome=ExpectedOutcome.PURCHASE_AVAILABLE,
            simulated_value_amount_minor=700,
            currency="EUR",
            failure_reasons=(FailureReason.INVENTORY_UNAVAILABLE,),
        )
        eur_diagnosis = diagnose_mission(eur_evidence)
        findings = aggregate_findings([diagnoses[0], eur_diagnosis, diagnoses[1]])
        stock = next(
            finding for finding in findings if finding.code is DiagnosticCode.STOCK_UNAVAILABLE
        )
        by_currency = {effect.currency: effect.amount_minor for effect in stock.simulated_demand}
        # Exactly the per currency amounts, never one summed figure.
        assert by_currency == {"INR": 1500, "EUR": 700}

    def test_co_occurring_observation_does_not_double_bill_demand(self) -> None:
        diagnoses = [
            diagnosis(
                "m1",
                reasons=(
                    FailureReason.INVENTORY_UNAVAILABLE,
                    FailureReason.AGENT_EXECUTION_ERROR,
                ),
                value=499900,
            )
        ]
        findings = aggregate_findings(diagnoses)
        stock = next(
            finding for finding in findings if finding.code is DiagnosticCode.STOCK_UNAVAILABLE
        )
        execution = next(
            finding
            for finding in findings
            if finding.code is DiagnosticCode.AGENT_EXECUTION_FAILURE
        )
        # One mission, one value: the co-occurring stock observation carries no demand,
        # because the leading diagnosis already billed it.
        assert stock.simulated_demand == ()
        assert execution.simulated_demand[0].amount_minor == 499900


class TestPresentation:
    def test_owner_and_actionability_survive_aggregation(self) -> None:
        diagnoses = [diagnosis("m1", reasons=(FailureReason.INVENTORY_UNAVAILABLE,))]
        finding = aggregate_findings(diagnoses)[0]
        assert isinstance(finding, MerchantFinding)
        assert finding.owner is DiagnosticOwner.MERCHANT_CATALOG
        assert finding.actionability is Actionability.MERCHANT_ACTION
        assert finding.severity is Severity.MEDIUM
        assert finding.recommendation is not None

    def test_title_names_the_attribute_when_one_exists(self) -> None:
        diagnoses = [
            diagnosis(
                "m1",
                reasons=(FailureReason.ATTRIBUTE_MISSING,),
                required=(RequiredAttributeFact("wattage", ConstraintOperator.GTE, 100),),
                selection=SelectionFacts(VARIANT_A, product_id=PRODUCT_A, attributes={}),
            )
        ]
        finding = aggregate_findings(diagnoses)[0]
        assert "wattage" in finding.title
