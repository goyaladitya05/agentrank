"""Deterministic diagnosis of one benchmark mission, from trusted evidence only.

This module answers, for a single `BenchmarkMissionRun`: what happened, what trusted
evidence establishes that, who owns the likely remediation, which product or attribute was
involved, whether simulated demand was affected, and what action is supported.

Three boundaries make the answer trustworthy rather than plausible, and all three are the
same boundaries the evaluator already enforces one level down.

Cause versus symptom. An evaluator failure reason is a symptom class. `AGENT_EXECUTION_ERROR`
on a mission whose traces end in a throttled provider is a provider outage wearing an agent
error's clothes; `DISCOVERY_FAILURE` may be merchant data, buyer behavior, both, or neither,
and saying which without further evidence would be invention. This module never promotes a
symptom to a cause without a stated rule over trusted facts, and marks every attribution
with the evidence level that supports it.

Model prose is not evidence. Nothing here reads model text, abstention explanations or error
narratives. The inputs are persisted outcomes, safety flags, commerce identifiers, the
mission's own requirements and structured trace facts. A model that explains itself
persuasively changes nothing.

Merchant blame is earned, not defaulted. Where ownership is genuinely undetermined the
diagnosis reports UNKNOWN ownership with REVIEW_REQUIRED actionability. An unresolved cause
stays unresolved, which is why UNKNOWN exists as an owner rather than a gap in the taxonomy.

The function is pure. No clock, no database, no model, no writes. Assembly of the input from
persisted rows happens in the read layer; everything here can be tested against hand written
evidence.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from agentrank_api.benchmark.definitions import ExpectedOutcome
from agentrank_api.benchmark.failures import FailureReason
from agentrank_api.benchmark.lifecycle import TERMINAL_MISSION_STATUSES, MissionRunStatus
from agentrank_api.constraints.rules import ConstraintOperator, compare, lookup_attribute
from agentrank_api.diagnostics.codes import (
    Actionability,
    DiagnosticCode,
    DiagnosticOwner,
    EvidenceLevel,
    Severity,
    engine_identity,
    identity_for,
    sort_codes,
)

# Kinds of evidence a finding may cite. These names are stable API surface: a future frontend
# uses them to resolve references back to concrete rows.
EVIDENCE_MISSION_RUN = "benchmark_mission_run"
EVIDENCE_TRACE_EVENT = "agent_trace_event"
EVIDENCE_VARIANT = "variant"
EVIDENCE_CHECKOUT = "checkout"
EVIDENCE_PAYMENT_ATTEMPT = "payment_attempt"
EVIDENCE_PROVIDER_USAGE = "agent_provider_usage"
EVIDENCE_REPRESENTATION = "commerce_representation"
EVIDENCE_SOURCE_SNAPSHOT = "merchant_source_snapshot"


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    """A pointer at concrete persisted evidence, with what it establishes.

    References point at rows rather than copying them, and each states in one line what the
    referenced row was used to establish, so a reader can check the reasoning instead of
    trusting a conclusion.
    """

    kind: str
    identifier: str
    establishes: str


@dataclass(frozen=True, slots=True)
class RequiredAttributeFact:
    """One attribute requirement the mission stated, in comparison vocabulary."""

    name: str
    operator: ConstraintOperator
    value: Any


@dataclass(frozen=True, slots=True)
class SelectionFacts:
    """What is known about the variant a mission selected, as the diagnosis may use it.

    `attributes` is the readable attribute document the mission was measured against, or None
    when nobody verified it. Passing today's catalog here without verifying the run's catalog
    pin is the caller's mistake to make loudly in review, which is why the field is optional:
    a diagnosis built without it keeps its findings at reason level rather than inventing
    specifics from a shelf that may have moved.
    """

    variant_id: uuid.UUID
    sku: str | None = None
    product_id: uuid.UUID | None = None
    category: str | None = None
    attributes: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class MissionDiagnosisInput:
    """Flattened trusted evidence for one mission, and nothing else.

    Every field is either copied from persisted rows or derived from them by stated rules in
    the read layer. Nothing here comes from anything the buying agent said about itself.
    """

    run_id: uuid.UUID
    mission_run_id: uuid.UUID
    mission_key: str
    status: MissionRunStatus
    expected_outcome: ExpectedOutcome
    simulated_value_amount_minor: int
    currency: str
    failure_reasons: tuple[FailureReason, ...] = ()
    unsafe_attempt: bool = False
    unverified_attempt: bool = False
    unsafe_completion: bool = False
    oracle_confirmed: bool | None = None
    selected_quantity: int | None = None
    checkout_id: uuid.UUID | None = None
    payment_attempt_id: uuid.UUID | None = None
    selection: SelectionFacts | None = None
    required_attributes: tuple[RequiredAttributeFact, ...] = ()
    required_categories: tuple[str, ...] = ()

    @property
    def purchase_was_available(self) -> bool:
        return self.expected_outcome is ExpectedOutcome.PURCHASE_AVAILABLE

    @property
    def is_finished(self) -> bool:
        return self.status in TERMINAL_MISSION_STATUSES


@dataclass(frozen=True, slots=True)
class SimulatedDemandEffect:
    """Simulated demand one mission carried, and which bucket it landed in.

    Authored benchmark demand is labelled simulated everywhere it appears, is grouped by
    currency, and is never summed across currencies. AT_RISK corresponds to the metrics'
    lost bucket: demand a merchant could have served and did not serve, whatever the reason.
    Which owner, if any, may be blamed for it is decided by the finding that cites it, never
    by this type.
    """

    currency: str
    bucket: str
    amount_minor: int


DEMAND_CAPTURED = "CAPTURED"
DEMAND_AT_RISK = "AT_RISK"
DEMAND_NOT_MEASURED = "NOT_MEASURED"


@dataclass(frozen=True, slots=True)
class MissionFinding:
    """One observation about one mission, with its ownership and evidence.

    A mission can carry several findings at once: a recovered provider throttle beside an
    agent selection error, or an enforcement escape beside everything else. Findings are
    ordered primary first by declared precedence; the first one is what a report leads with.
    """

    code: DiagnosticCode
    owner: DiagnosticOwner
    actionability: Actionability
    severity: Severity
    evidence_level: EvidenceLevel
    summary: str
    recommendation: str | None
    attribute_keys: tuple[str, ...] = ()
    product_ids: tuple[uuid.UUID, ...] = ()
    variant_ids: tuple[uuid.UUID, ...] = ()
    evidence: tuple[EvidenceReference, ...] = ()


@dataclass(frozen=True, slots=True)
class MissionDiagnosis:
    """The complete deterministic reading of one mission.

    `outcome` is one honest sentence about what became of the mission, suitable as a report
    lead. `findings` is ordered and may be empty: a correct abstention and a clean success
    carry observations, not findings, and inventing problems for them would make the whole
    layer noise.
    """

    engine_identity: str
    run_id: uuid.UUID
    mission_run_id: uuid.UUID
    mission_key: str
    status: MissionRunStatus
    outcome: str
    primary: MissionFinding | None
    findings: tuple[MissionFinding, ...]
    simulated_demand: tuple[SimulatedDemandEffect, ...] = ()


def diagnose_mission(evidence: MissionDiagnosisInput) -> MissionDiagnosis:
    """Diagnose one mission from its trusted evidence.

    Deterministic and total: any well formed input produces a diagnosis, including the
    non terminal and errored shapes a partial run leaves behind. Codes are collected from
    independent rules, ordered once, and rendered once, so two engines reading the same rows
    emit identical output.
    """
    builders = [
        _escape_rule,
        _harness_rule,
        _unmeasured_rule,
        _ground_truth_rule,
        _contradiction_rule,
        _surface_error_rule,
        _execution_rule,
        _merchant_data_rules,
        _stock_rule,
        _checkout_refusal_rule,
        _selection_rule,
        _authorization_rule,
        _payment_rules,
        _discovery_rule,
    ]
    findings: list[MissionFinding] = []
    for rule in builders:
        findings.extend(rule(evidence))
    ordered = _ordered(findings)
    demand = _demand_effect(evidence)
    return MissionDiagnosis(
        engine_identity=engine_identity(),
        run_id=evidence.run_id,
        mission_run_id=evidence.mission_run_id,
        mission_key=evidence.mission_key,
        status=evidence.status,
        outcome=_outcome_statement(evidence),
        primary=ordered[0] if ordered else None,
        findings=tuple(ordered),
        simulated_demand=demand,
    )


def _ordered(findings: list[MissionFinding]) -> list[MissionFinding]:
    """Findings sorted by their code's precedence, stable within one code."""
    unique: dict[DiagnosticCode, MissionFinding] = {}
    for finding in findings:
        existing = unique.get(finding.code)
        if existing is None:
            unique[finding.code] = finding
            continue
        unique[finding.code] = _merged(existing, finding)
    ranked = sort_codes(unique.keys())
    return [unique[code] for code in ranked]


def _merged(first: MissionFinding, second: MissionFinding) -> MissionFinding:
    """Two findings of one code become one, keeping the stronger of each dimension.

    Attribute keys, products, variants and evidence accumulate. Severity takes the higher,
    summaries prefer the first because templates render one sentence per code.
    """
    severity = first.severity if first.severity.rank >= second.severity.rank else second.severity
    return MissionFinding(
        code=first.code,
        owner=first.owner,
        actionability=first.actionability,
        severity=severity,
        evidence_level=first.evidence_level,
        summary=first.summary,
        recommendation=(
            first.recommendation if first.recommendation is not None else second.recommendation
        ),
        attribute_keys=_combined(first.attribute_keys, second.attribute_keys),
        product_ids=_combined(first.product_ids, second.product_ids),
        variant_ids=_combined(first.variant_ids, second.variant_ids),
        evidence=_combined(first.evidence, second.evidence),
    )


def _combined(*groups: tuple[Any, ...]) -> tuple[Any, ...]:
    seen: list[Any] = []
    for group in groups:
        for item in group:
            if item not in seen:
                seen.append(item)
    return tuple(seen)


def _finding(
    code: DiagnosticCode,
    evidence: MissionDiagnosisInput,
    *,
    summary: str,
    recommendation: str | None = None,
    severity: Severity | None = None,
    attribute_keys: tuple[str, ...] = (),
    extra_evidence: tuple[EvidenceReference, ...] = (),
) -> MissionFinding:
    identity = identity_for(code)
    mission_reference = EvidenceReference(
        kind=EVIDENCE_MISSION_RUN,
        identifier=str(evidence.mission_run_id),
        establishes="the mission outcome, failure classification and safety flags",
    )
    selection = evidence.selection
    products = () if selection is None or selection.product_id is None else (selection.product_id,)
    variants = () if selection is None else (selection.variant_id,)
    return MissionFinding(
        code=code,
        owner=identity.owner,
        actionability=identity.actionability,
        severity=severity if severity is not None else identity.severity,
        evidence_level=identity.evidence_level,
        summary=summary,
        recommendation=recommendation,
        attribute_keys=attribute_keys,
        product_ids=products,
        variant_ids=variants,
        evidence=(mission_reference, *extra_evidence),
    )


def _escape_rule(evidence: MissionDiagnosisInput) -> list[MissionFinding]:
    if not evidence.unsafe_completion:
        return []
    payment_reference = (
        (
            EvidenceReference(
                kind=EVIDENCE_PAYMENT_ATTEMPT,
                identifier=str(evidence.payment_attempt_id),
                establishes="a payment completed past an authorization refusal",
            ),
        )
        if evidence.payment_attempt_id is not None
        else ()
    )
    return [
        _finding(
            DiagnosticCode.SAFETY_ESCAPE,
            evidence,
            summary=(
                "A payment completed although authorization had refused the purchase."
                " Enforcement failed inside the commerce runtime."
            ),
            recommendation=(
                "Treat this as a system integrity incident rather than a catalog problem:"
                " no merchant change produced or fixes it."
            ),
            extra_evidence=payment_reference,
        )
    ]


def _harness_rule(evidence: MissionDiagnosisInput) -> list[MissionFinding]:
    if evidence.status is not MissionRunStatus.ERRORED:
        return []
    return [
        _finding(
            DiagnosticCode.BENCHMARK_HARNESS_FAULT,
            evidence,
            summary=(
                "The benchmark harness could not measure this mission, so no merchant"
                " conclusion is drawn from it."
            ),
            recommendation=None,
        )
    ]


def _unmeasured_rule(evidence: MissionDiagnosisInput) -> list[MissionFinding]:
    if evidence.is_finished:
        return []
    label = "was never started" if evidence.status is MissionRunStatus.PENDING else "did not finish"
    return [
        _finding(
            DiagnosticCode.MISSION_NOT_MEASURED,
            evidence,
            summary=f"This mission {label}, so nothing about it is established.",
            recommendation=None,
        )
    ]


def _ground_truth_rule(evidence: MissionDiagnosisInput) -> list[MissionFinding]:
    if evidence.oracle_confirmed is not False:
        return []
    return [
        _finding(
            DiagnosticCode.GROUND_TRUTH_DISAGREEMENT,
            evidence,
            summary=(
                "The suite's authored ground truth disagreed with the merchant's actual"
                " catalog when this mission ran, which qualifies how its result reads."
            ),
            recommendation=(
                "Republish or re-author the affected benchmark expectations against the"
                " current catalog before comparing this run with others."
            ),
        )
    ]


def _contradiction_rule(evidence: MissionDiagnosisInput) -> list[MissionFinding]:
    if FailureReason.AGENT_REASONING_ERROR not in evidence.failure_reasons:
        return []
    return [
        _finding(
            DiagnosticCode.AGENT_REPORT_CONTRADICTION,
            evidence,
            summary=(
                "The buyer's observed actions contradict each other or stopped without an"
                " outcome, so the attempt cannot be classified further."
            ),
            recommendation=None,
        )
    ]


def _surface_error_rule(evidence: MissionDiagnosisInput) -> list[MissionFinding]:
    if FailureReason.MERCHANT_API_ERROR not in evidence.failure_reasons:
        return []
    return [
        _finding(
            DiagnosticCode.MERCHANT_SURFACE_ERROR,
            evidence,
            summary="The merchant's own API returned an error instead of an answer.",
            recommendation=(
                "Investigate the merchant integration errors recorded during this run;"
                " buyers were unable to shop while they lasted."
            ),
        )
    ]


def _execution_rule(evidence: MissionDiagnosisInput) -> list[MissionFinding]:
    if FailureReason.AGENT_EXECUTION_ERROR not in evidence.failure_reasons:
        return []
    return [
        _finding(
            DiagnosticCode.AGENT_EXECUTION_FAILURE,
            evidence,
            summary=(
                "The buyer did not carry the mission out, and the available evidence does"
                " not attribute this to infrastructure."
            ),
            recommendation=None,
        )
    ]


def _merchant_data_rules(evidence: MissionDiagnosisInput) -> list[MissionFinding]:
    findings: list[MissionFinding] = []
    selection = evidence.selection
    attributes = selection.attributes if selection is not None else None

    missing: list[str] = []
    unreadable: list[str] = []
    if attributes is not None:
        document = dict(attributes)
        for requirement in evidence.required_attributes:
            found, actual = lookup_attribute(document, requirement.name)
            if not found:
                missing.append(requirement.name)
                continue
            satisfied = compare(requirement.operator, requirement.value, actual)
            if satisfied is None:
                unreadable.append(requirement.name)

    if FailureReason.CATEGORY_MISSING in evidence.failure_reasons:
        findings.append(
            _finding(
                DiagnosticCode.CATEGORY_NOT_PUBLISHED,
                evidence,
                summary=(
                    "Buyers could not read what this product is: no category is published,"
                    " so stated category requirements could not be checked."
                ),
                recommendation=(
                    "Publish a category for the affected product so agents can match it"
                    " against buyer requirements."
                ),
            )
        )
    if FailureReason.ATTRIBUTE_MISSING in evidence.failure_reasons:
        specifics = tuple(missing) if missing else ()
        findings.append(
            _finding(
                DiagnosticCode.ATTRIBUTE_NOT_PUBLISHED,
                evidence,
                summary=_attribute_summary("is not published", evidence, specifics),
                recommendation=_attribute_recommendation("add or confirm", evidence, specifics),
                attribute_keys=specifics,
            )
        )
    if FailureReason.ATTRIBUTE_UNREADABLE in evidence.failure_reasons:
        specifics = tuple(unreadable) if unreadable else ()
        findings.append(
            _finding(
                DiagnosticCode.ATTRIBUTE_UNREADABLE,
                evidence,
                summary=_attribute_summary(
                    "is published in a form agents cannot compare", evidence, specifics
                ),
                recommendation=_attribute_recommendation(
                    "publish in a typed, comparable form", evidence, specifics
                ),
                attribute_keys=specifics,
            )
        )
    return findings


def _attribute_summary(
    predicate: str, evidence: MissionDiagnosisInput, keys: tuple[str, ...]
) -> str:
    scope = f" ({', '.join(sorted(keys))})" if keys else ""
    return (
        f"A required attribute{scope} {predicate} on the offer this mission evaluated, so"
        " compliance with the buyer's stated requirement could not be established."
    )


def _attribute_recommendation(
    verb: str, evidence: MissionDiagnosisInput, keys: tuple[str, ...]
) -> str:
    target = f"the '{', '.join(sorted(keys))}' attribute" if keys else "the required attributes"
    return (
        f"{verb.capitalize()} {target} on the affected variants before relying on agent-ready"
        " discovery for these products."
    )


def _stock_rule(evidence: MissionDiagnosisInput) -> list[MissionFinding]:
    if FailureReason.INVENTORY_UNAVAILABLE not in evidence.failure_reasons:
        return []
    return [
        _finding(
            DiagnosticCode.STOCK_UNAVAILABLE,
            evidence,
            summary=(
                "The merchant sells this item but could not hold enough stock to complete"
                " the purchase."
            ),
            recommendation=(
                "Restock the affected variant, or withdraw it from the catalog if it is no"
                " longer offered."
            ),
        )
    ]


def _checkout_refusal_rule(evidence: MissionDiagnosisInput) -> list[MissionFinding]:
    if FailureReason.CHECKOUT_CREATION_FAILED not in evidence.failure_reasons:
        return []
    return [
        _finding(
            DiagnosticCode.CHECKOUT_REFUSED,
            evidence,
            summary=(
                "The merchant declined to quote for this mission, for a reason that is not stock."
            ),
            recommendation=(
                "Review the quoting refusals recorded during this run to confirm the"
                " merchant surface refuses only purchases it means to refuse."
            ),
        )
    ]


_SELECTION_REASON_MAP: tuple[tuple[FailureReason, str], ...] = (
    (
        FailureReason.WRONG_MERCHANT,
        "transacted with a merchant other than the one under evaluation",
    ),
    (
        FailureReason.CURRENCY_MISMATCH,
        "chose an offer priced in a currency the budget does not allow",
    ),
    (FailureReason.BUDGET_EXCEEDED, "chose an offer above the buyer's stated ceiling"),
    (FailureReason.CONSTRAINT_VIOLATION, "chose an offer violating a stated requirement"),
    (FailureReason.INVALID_VARIANT, "chose something the merchant does not sell"),
    (FailureReason.QUANTITY_MISMATCH, "bought a different quantity than the mission asked for"),
)


def _selection_rule(evidence: MissionDiagnosisInput) -> list[MissionFinding]:
    details = [
        description
        for reason, description in _SELECTION_REASON_MAP
        if reason in evidence.failure_reasons
    ]
    if not details:
        return []
    joined = "; ".join(details)
    return [
        _finding(
            DiagnosticCode.SELECTION_VIOLATED_REQUIREMENTS,
            evidence,
            summary=f"The buyer {joined}.",
            recommendation=None,
        )
    ]


def _authorization_rule(evidence: MissionDiagnosisInput) -> list[MissionFinding]:
    if FailureReason.MANDATE_DENIED not in evidence.failure_reasons:
        return []
    if evidence.unsafe_attempt:
        # The denial stopped an attempt the merchant's own data proves was outside what the
        # buyer authorized. Safety worked; nothing here asks a merchant to fix anything.
        return [
            _finding(
                DiagnosticCode.UNSAFE_ATTEMPT_BLOCKED,
                evidence,
                summary=(
                    "Authorization refused an attempt that was outside what the buyer had"
                    " authorized, and no money moved."
                ),
                recommendation=None,
            )
        ]
    return [
        _finding(
            DiagnosticCode.AUTHORIZATION_DENIED_COMPLIANT_ATTEMPT,
            evidence,
            summary=(
                "Authorization refused this purchase although the evaluator could not fault"
                " the attempt itself."
            ),
            recommendation=(
                "Compare the checkout timestamps against the mandate validity window to see"
                " whether timing, rather than policy, ended this purchase."
            ),
        )
    ]


def _payment_rules(evidence: MissionDiagnosisInput) -> list[MissionFinding]:
    findings: list[MissionFinding] = []
    payment_reference = (
        EvidenceReference(
            kind=EVIDENCE_PAYMENT_ATTEMPT,
            identifier=str(evidence.payment_attempt_id),
            establishes="the payment state the mission reached",
        ),
    )
    if FailureReason.PAYMENT_FAILED in evidence.failure_reasons:
        findings.append(
            _finding(
                DiagnosticCode.PAYMENT_DECLINED,
                evidence,
                summary="The payment provider definitively declined, and no money moved.",
                recommendation=None,
                extra_evidence=payment_reference if evidence.payment_attempt_id else (),
            )
        )
    if FailureReason.PAYMENT_UNRESOLVED in evidence.failure_reasons:
        findings.append(
            _finding(
                DiagnosticCode.PAYMENT_UNRESOLVED,
                evidence,
                summary=(
                    "The payment reached no definitive outcome, so the purchase is neither"
                    " complete nor declined."
                ),
                recommendation=(
                    "Resolve the outstanding payment through the operator recovery tooling"
                    " before drawing conclusions from this mission."
                ),
                extra_evidence=payment_reference if evidence.payment_attempt_id else (),
            )
        )
    return findings


def _discovery_rule(evidence: MissionDiagnosisInput) -> list[MissionFinding]:
    if FailureReason.DISCOVERY_FAILURE not in evidence.failure_reasons:
        return []
    return [
        _finding(
            DiagnosticCode.DISCOVERY_FAILED,
            evidence,
            summary=(
                "No purchasable option was identified although this mission expects one to"
                " exist. Available evidence does not establish whether merchant data, buyer"
                " behavior, or both contributed."
            ),
            recommendation=(
                "Reproduce the mission's search path against the storefront view before"
                " changing anything: the cause is unresolved."
            ),
        )
    ]


def _outcome_statement(evidence: MissionDiagnosisInput) -> str:
    if evidence.status is MissionRunStatus.SUCCEEDED:
        return (
            f"The buyer purchased {evidence.selected_quantity or 1} unit(s) compliantly and"
            " payment completed."
        )
    if evidence.status is MissionRunStatus.ABSTAINED:
        if evidence.failure_reasons:
            return "The buyer declined, but this mission expected a purchase to be possible."
        return "The buyer correctly declined a mission where nothing acceptable was for sale."
    if evidence.status is MissionRunStatus.FAILED:
        primary = evidence.failure_reasons[0].value if evidence.failure_reasons else "UNKNOWN"
        return f"The mission failed. Primary evaluator reason: {primary}."
    if evidence.status is MissionRunStatus.ERRORED:
        return "The mission could not be measured because the benchmark harness failed."
    if evidence.status is MissionRunStatus.RUNNING:
        return "The mission did not finish."
    return "The mission was never started."


def _demand_effect(evidence: MissionDiagnosisInput) -> tuple[SimulatedDemandEffect, ...]:
    """Which bucket this mission's authored demand landed in, if it carried any.

    Mirrors the metric buckets exactly, because a diagnosis whose demand arithmetic disagreed
    with the run's own metrics would be two sources of truth about one number. Control
    missions carry zero value by constraint and produce no entry at all.
    """
    if not evidence.purchase_was_available or evidence.simulated_value_amount_minor <= 0:
        return ()
    if evidence.status is MissionRunStatus.SUCCEEDED:
        bucket = DEMAND_CAPTURED
    elif evidence.status is MissionRunStatus.ERRORED or not evidence.is_finished:
        bucket = DEMAND_NOT_MEASURED
    else:
        bucket = DEMAND_AT_RISK
    effect = SimulatedDemandEffect(evidence.currency, bucket, evidence.simulated_value_amount_minor)
    return (effect,)


# Re-exported so callers building inputs do not import the enums from three modules.
__all__ = [
    "DEMAND_AT_RISK",
    "DEMAND_CAPTURED",
    "DEMAND_NOT_MEASURED",
    "EVIDENCE_CHECKOUT",
    "EVIDENCE_MISSION_RUN",
    "EVIDENCE_PAYMENT_ATTEMPT",
    "EVIDENCE_PROVIDER_USAGE",
    "EVIDENCE_REPRESENTATION",
    "EVIDENCE_SOURCE_SNAPSHOT",
    "EVIDENCE_TRACE_EVENT",
    "EVIDENCE_VARIANT",
    "EvidenceReference",
    "MissionDiagnosis",
    "MissionDiagnosisInput",
    "MissionFinding",
    "RequiredAttributeFact",
    "SelectionFacts",
    "SimulatedDemandEffect",
    "diagnose_mission",
    "engine_identity",
]
