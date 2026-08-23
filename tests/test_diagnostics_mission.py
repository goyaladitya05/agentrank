"""Mission diagnosis against hand written evidence, never against the runner.

Every test here builds a `MissionDiagnosisInput` by hand and asserts what a deterministic
engine must say about it. Fixtures that ran real missions would make these tests self
referential: the diagnosis would be checked against the same pipeline that produced its own
inputs. The read layer that assembles inputs from rows gets its own database backed tests.
"""

import uuid

from agentrank_api.benchmark.definitions import ExpectedOutcome
from agentrank_api.benchmark.failures import FailureReason
from agentrank_api.benchmark.lifecycle import MissionRunStatus
from agentrank_api.constraints.rules import ConstraintOperator
from agentrank_api.diagnostics.codes import (
    IDENTITIES,
    PRIMARY_PRECEDENCE,
    SECONDARY_ONLY_CODES,
    Actionability,
    DiagnosticCode,
    DiagnosticOwner,
    EvidenceLevel,
    Severity,
    engine_identity,
    identity_for,
    primary_code,
    sort_codes,
)
from agentrank_api.diagnostics.mission import (
    DEMAND_AT_RISK,
    DEMAND_CAPTURED,
    DEMAND_NOT_MEASURED,
    MissionDiagnosisInput,
    RequiredAttributeFact,
    SelectionFacts,
    SimulatedDemandEffect,
    diagnose_mission,
)

MERCHANT = uuid.uuid4()
RUN = uuid.uuid4()
MISSION_RUN = uuid.uuid4()
VARIANT = uuid.uuid4()
PRODUCT = uuid.uuid4()


def evidence(**overrides: object) -> MissionDiagnosisInput:
    """One failed discovery on a purchase available mission, the common shape."""
    fields: dict[str, object] = {
        "run_id": RUN,
        "mission_run_id": MISSION_RUN,
        "mission_key": "buy-a-charger",
        "status": MissionRunStatus.FAILED,
        "expected_outcome": ExpectedOutcome.PURCHASE_AVAILABLE,
        "simulated_value_amount_minor": 499900,
        "currency": "INR",
        "failure_reasons": (FailureReason.DISCOVERY_FAILURE,),
    }
    fields.update(overrides)
    return MissionDiagnosisInput(**fields)  # type: ignore[arg-type]


class TestCodes:
    def test_every_code_has_exactly_one_identity(self) -> None:
        assert set(IDENTITIES) == set(DiagnosticCode)

    def test_precedence_is_a_permutation_of_primary_eligible_codes(self) -> None:
        eligible = [code for code in DiagnosticCode if code not in SECONDARY_ONLY_CODES]
        assert sorted(PRIMARY_PRECEDENCE) == sorted(eligible)

    def test_secondary_only_codes_are_never_primary(self) -> None:
        assert (
            primary_code(
                [DiagnosticCode.PROVIDER_THROTTLE_RECOVERED, DiagnosticCode.DISCOVERY_FAILED]
            )
            is DiagnosticCode.DISCOVERY_FAILED
        )
        assert primary_code([DiagnosticCode.RESOLVED_MODEL_MISMATCH]) is None

    def test_provider_termination_outranks_agent_failure(self) -> None:
        assert (
            primary_code(
                [
                    DiagnosticCode.AGENT_EXECUTION_FAILURE,
                    DiagnosticCode.PROVIDER_OUTAGE_TERMINATED_MISSION,
                ]
            )
            is DiagnosticCode.PROVIDER_OUTAGE_TERMINATED_MISSION
        )

    def test_sorting_is_insertion_independent(self) -> None:
        codes = [
            DiagnosticCode.STOCK_UNAVAILABLE,
            DiagnosticCode.PROVIDER_THROTTLE_RECOVERED,
            DiagnosticCode.SAFETY_ESCAPE,
            DiagnosticCode.RESOLVED_MODEL_MISMATCH,
        ]
        forward = sort_codes(codes)
        backward = sort_codes(list(reversed(codes)))
        assert forward == backward
        assert forward[0] is DiagnosticCode.SAFETY_ESCAPE
        assert forward[-1] in SECONDARY_ONLY_CODES

    def test_engine_identity_is_stable_and_sensitive(self) -> None:
        first = engine_identity()
        assert first == engine_identity()
        assert first.startswith("sha256:")

    def test_severity_ranking_is_total(self) -> None:
        ordered = [Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW]
        ranks = [severity.rank for severity in ordered]
        assert ranks == sorted(ranks, reverse=True)


class TestOutcomes:
    def test_successful_mission_reports_no_findings(self) -> None:
        diagnosis = diagnose_mission(
            evidence(
                status=MissionRunStatus.SUCCEEDED,
                failure_reasons=(),
                selected_quantity=1,
                selection=SelectionFacts(variant_id=VARIANT, product_id=PRODUCT),
            )
        )
        assert diagnosis.findings == ()
        assert diagnosis.primary is None
        assert "compliantly" in diagnosis.outcome

    def test_correct_abstention_reports_no_findings(self) -> None:
        diagnosis = diagnose_mission(
            evidence(
                status=MissionRunStatus.ABSTAINED,
                expected_outcome=ExpectedOutcome.NO_ACCEPTABLE_PURCHASE,
                simulated_value_amount_minor=0,
                failure_reasons=(),
            )
        )
        assert diagnosis.findings == ()
        assert diagnosis.simulated_demand == ()

    def test_failed_discovery_is_unresolved_not_merchant_blame(self) -> None:
        diagnosis = diagnose_mission(evidence())
        assert diagnosis.primary is not None
        assert diagnosis.primary.code is DiagnosticCode.DISCOVERY_FAILED
        assert diagnosis.primary.owner is DiagnosticOwner.UNKNOWN
        assert diagnosis.primary.evidence_level is EvidenceLevel.UNRESOLVED
        assert diagnosis.primary.actionability is Actionability.REVIEW_REQUIRED


class TestProviderFaults:
    def test_provider_outage_cannot_be_filed_as_merchant_or_buyer(self) -> None:
        # Feature 2 enriches this rule with trace evidence; the code mapping already
        # guarantees the ownership once the outage code is present.
        identity = identity_for(DiagnosticCode.PROVIDER_OUTAGE_TERMINATED_MISSION)
        assert identity.owner is DiagnosticOwner.MODEL_PROVIDER
        assert identity.actionability is Actionability.NO_MERCHANT_ACTION


class TestSafety:
    def test_escape_is_critical_and_owned_by_the_runtime(self) -> None:
        diagnosis = diagnose_mission(
            evidence(
                failure_reasons=(
                    FailureReason.ENFORCEMENT_BYPASSED,
                    FailureReason.MANDATE_DENIED,
                ),
                unsafe_attempt=True,
                unsafe_completion=True,
                payment_attempt_id=uuid.uuid4(),
            )
        )
        assert diagnosis.primary is not None
        assert diagnosis.primary.code is DiagnosticCode.SAFETY_ESCAPE
        assert diagnosis.primary.severity is Severity.CRITICAL
        assert diagnosis.primary.owner is DiagnosticOwner.COMMERCE_RUNTIME
        codes = {finding.code for finding in diagnosis.findings}
        assert DiagnosticCode.UNSAFE_ATTEMPT_BLOCKED in codes

    def test_blocked_unsafe_attempt_requires_no_merchant_action(self) -> None:
        diagnosis = diagnose_mission(
            evidence(
                failure_reasons=(FailureReason.MANDATE_DENIED, FailureReason.BUDGET_EXCEEDED),
                unsafe_attempt=True,
            )
        )
        blocked = next(
            finding
            for finding in diagnosis.findings
            if finding.code is DiagnosticCode.UNSAFE_ATTEMPT_BLOCKED
        )
        assert blocked.actionability is Actionability.NO_MERCHANT_ACTION
        # The selection breach is still the primary finding about what went wrong.
        assert diagnosis.primary is not None
        assert diagnosis.primary.code is DiagnosticCode.SELECTION_VIOLATED_REQUIREMENTS

    def test_compliant_denial_stays_unowned(self) -> None:
        diagnosis = diagnose_mission(
            evidence(failure_reasons=(FailureReason.MANDATE_DENIED,), unsafe_attempt=False)
        )
        assert diagnosis.primary is not None
        assert diagnosis.primary.code is DiagnosticCode.AUTHORIZATION_DENIED_COMPLIANT_ATTEMPT
        assert diagnosis.primary.owner is DiagnosticOwner.UNKNOWN


class TestMerchantData:
    def test_missing_attribute_names_the_key_when_selection_is_known(self) -> None:
        diagnosis = diagnose_mission(
            evidence(
                failure_reasons=(FailureReason.ATTRIBUTE_MISSING,),
                required_attributes=(
                    RequiredAttributeFact("wattage", ConstraintOperator.GTE, 100),
                    RequiredAttributeFact("color", ConstraintOperator.EQ, "black"),
                ),
                selection=SelectionFacts(
                    variant_id=VARIANT,
                    product_id=PRODUCT,
                    attributes={"color": "black"},
                ),
            )
        )
        gap = next(
            finding
            for finding in diagnosis.findings
            if finding.code is DiagnosticCode.ATTRIBUTE_NOT_PUBLISHED
        )
        assert gap.attribute_keys == ("wattage",)
        assert gap.owner is DiagnosticOwner.MERCHANT_CATALOG
        assert gap.actionability is Actionability.MERCHANT_ACTION
        assert "wattage" in (gap.recommendation or "")

    def test_missing_attribute_stays_at_reason_level_without_selection_facts(self) -> None:
        diagnosis = diagnose_mission(evidence(failure_reasons=(FailureReason.ATTRIBUTE_MISSING,)))
        gap = next(
            finding
            for finding in diagnosis.findings
            if finding.code is DiagnosticCode.ATTRIBUTE_NOT_PUBLISHED
        )
        assert gap.attribute_keys == ()
        # No invented specifics: without verified attributes nothing names a key.
        assert "add or confirm the required attributes" in (gap.recommendation or "").lower()

    def test_unreadable_attribute_names_the_key(self) -> None:
        diagnosis = diagnose_mission(
            evidence(
                failure_reasons=(FailureReason.ATTRIBUTE_UNREADABLE,),
                required_attributes=(
                    RequiredAttributeFact("wattage", ConstraintOperator.GTE, 100),
                ),
                selection=SelectionFacts(
                    variant_id=VARIANT,
                    product_id=PRODUCT,
                    attributes={"wattage": "100W"},
                ),
            )
        )
        unreadable = next(
            finding
            for finding in diagnosis.findings
            if finding.code is DiagnosticCode.ATTRIBUTE_UNREADABLE
        )
        assert unreadable.attribute_keys == ("wattage",)

    def test_category_gap_is_a_catalog_finding(self) -> None:
        diagnosis = diagnose_mission(
            evidence(
                failure_reasons=(FailureReason.CATEGORY_MISSING,),
                selection=SelectionFacts(variant_id=VARIANT, product_id=PRODUCT),
            )
        )
        gap = next(
            finding
            for finding in diagnosis.findings
            if finding.code is DiagnosticCode.CATEGORY_NOT_PUBLISHED
        )
        assert gap.product_ids == (PRODUCT,)
        assert gap.actionability is Actionability.MERCHANT_ACTION


class TestInfrastructure:
    def test_harness_fault_carries_no_merchant_action(self) -> None:
        diagnosis = diagnose_mission(evidence(status=MissionRunStatus.ERRORED, failure_reasons=()))
        assert diagnosis.primary is not None
        assert diagnosis.primary.code is DiagnosticCode.BENCHMARK_HARNESS_FAULT
        assert diagnosis.primary.owner is DiagnosticOwner.BENCHMARK_INFRASTRUCTURE
        assert diagnosis.primary.recommendation is None

    def test_pending_mission_is_reported_as_unmeasured(self) -> None:
        diagnosis = diagnose_mission(evidence(status=MissionRunStatus.PENDING, failure_reasons=()))
        assert diagnosis.primary is not None
        assert diagnosis.primary.code is DiagnosticCode.MISSION_NOT_MEASURED

    def test_running_mission_is_reported_as_unmeasured(self) -> None:
        diagnosis = diagnose_mission(evidence(status=MissionRunStatus.RUNNING, failure_reasons=()))
        assert diagnosis.primary is not None
        assert diagnosis.primary.code is DiagnosticCode.MISSION_NOT_MEASURED

    def test_stale_oracle_outranks_an_unresolved_outcome(self) -> None:
        # A ground truth disagreement reframes the whole mission: a discovery failure on a
        # mission whose oracle the catalog contradicts may be the agent being right. The
        # infrastructure fact therefore leads, and the unresolved finding stays beside it.
        diagnosis = diagnose_mission(evidence(oracle_confirmed=False))
        disagreement = next(
            finding
            for finding in diagnosis.findings
            if finding.code is DiagnosticCode.GROUND_TRUTH_DISAGREEMENT
        )
        assert disagreement.owner is DiagnosticOwner.BENCHMARK_INFRASTRUCTURE
        assert diagnosis.primary is not None
        assert diagnosis.primary.code is DiagnosticCode.GROUND_TRUTH_DISAGREEMENT


class TestSimulatedDemand:
    def test_failed_purchase_mission_demand_is_at_risk(self) -> None:
        diagnosis = diagnose_mission(evidence())
        assert diagnosis.simulated_demand == (SimulatedDemandEffect("INR", DEMAND_AT_RISK, 499900),)

    def test_success_captures_demand(self) -> None:
        diagnosis = diagnose_mission(
            evidence(status=MissionRunStatus.SUCCEEDED, failure_reasons=(), selected_quantity=1)
        )
        assert diagnosis.simulated_demand[0].bucket == DEMAND_CAPTURED
        assert diagnosis.simulated_demand[0].amount_minor == 499900

    def test_errored_mission_demand_is_not_measured(self) -> None:
        diagnosis = diagnose_mission(evidence(status=MissionRunStatus.ERRORED, failure_reasons=()))
        assert diagnosis.simulated_demand[0].bucket == DEMAND_NOT_MEASURED

    def test_control_missions_carry_no_demand(self) -> None:
        diagnosis = diagnose_mission(
            evidence(
                expected_outcome=ExpectedOutcome.NO_ACCEPTABLE_PURCHASE,
                simulated_value_amount_minor=0,
            )
        )
        assert diagnosis.simulated_demand == ()


class TestDeterminism:
    def test_identical_inputs_produce_identical_diagnoses(self) -> None:
        first = diagnose_mission(evidence())
        second = diagnose_mission(evidence())
        assert first == second

    def test_diagnosis_carries_the_engine_identity_that_produced_it(self) -> None:
        diagnosis = diagnose_mission(evidence())
        assert diagnosis.engine_identity == engine_identity()

    def test_finding_order_does_not_depend_on_rule_discovery_order(self) -> None:
        # Safety escape plus merchant data gap: escape leads regardless of which rule ran.
        escaped = diagnose_mission(
            evidence(
                failure_reasons=(
                    FailureReason.ENFORCEMENT_BYPASSED,
                    FailureReason.ATTRIBUTE_MISSING,
                ),
                unsafe_attempt=True,
                unsafe_completion=True,
            )
        )
        reordered = diagnose_mission(
            evidence(
                failure_reasons=(
                    FailureReason.ATTRIBUTE_MISSING,
                    FailureReason.ENFORCEMENT_BYPASSED,
                ),
                unsafe_attempt=True,
                unsafe_completion=True,
            )
        )
        assert [finding.code for finding in escaped.findings] == [
            finding.code for finding in reordered.findings
        ]
        assert escaped.primary is not None
        assert escaped.primary.code is DiagnosticCode.SAFETY_ESCAPE


class TestMultipleFindings:
    def test_secondary_observations_survive_alongside_the_primary(self) -> None:
        diagnosis = diagnose_mission(
            evidence(
                failure_reasons=(
                    FailureReason.AGENT_EXECUTION_ERROR,
                    FailureReason.INVENTORY_UNAVAILABLE,
                ),
                selection=SelectionFacts(variant_id=VARIANT, product_id=PRODUCT),
            )
        )
        codes = [finding.code for finding in diagnosis.findings]
        assert DiagnosticCode.AGENT_EXECUTION_FAILURE in codes
        assert DiagnosticCode.STOCK_UNAVAILABLE in codes
        stock = next(
            finding
            for finding in diagnosis.findings
            if finding.code is DiagnosticCode.STOCK_UNAVAILABLE
        )
        assert stock.variant_ids == (VARIANT,)
