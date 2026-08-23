"""Merchant level findings, aggregated from per mission diagnoses.

One mission's diagnosis is evidence. A merchant finding is what that evidence adds up to:
the same kind of problem recurring across missions, concentrated on one product or attribute,
or carrying enough simulated demand to be worth acting on. Aggregation is where diagnostics
become prioritization, so it has rules rather than instincts:

Grouping is deterministic. Findings group by diagnostic code, and by attribute keys within
the codes that name them, so "wattage was unavailable" is one finding across every mission it
blocked while two unrelated agent failures stay two findings. Nothing groups across owners,
and nothing invents a product level problem out of missions whose evidence does not name one.

Simulated demand follows the primary diagnosis only. A mission carries its authored value
once, and it is billed against the one finding that leads that mission's diagnosis, because
assigning the same lost demand to every co-occurring observation would triple count one
buyer. When the leading diagnosis is unresolved or external, its finding still reports the
affected simulated demand, labelled as always, and its ownership says who may act on it:
never silently the merchant.

Ordering is total. Two engines reading the same diagnoses emit the same report in the same
order: severity first, then the number of missions affected, then declared precedence, then
the grouping key itself.
"""

import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from agentrank_api.diagnostics.codes import (
    Actionability,
    DiagnosticCode,
    DiagnosticOwner,
    EvidenceLevel,
    Severity,
    highest_severity,
    identity_for,
    precedence_rank,
)
from agentrank_api.diagnostics.mission import (
    DEMAND_AT_RISK,
    DEMAND_CAPTURED,
    DEMAND_NOT_MEASURED,
    MissionDiagnosis,
    MissionFinding,
    SimulatedDemandEffect,
)

# Codes that name concrete attributes carry their keys into the grouping key, so gaps on two
# different attributes stay two findings with two different repairs.
_ATTRIBUTE_SCOPED_CODES = frozenset(
    {DiagnosticCode.ATTRIBUTE_NOT_PUBLISHED, DiagnosticCode.ATTRIBUTE_UNREADABLE}
)

_DEMAND_BUCKET_ORDER = {DEMAND_CAPTURED: 0, DEMAND_AT_RISK: 1, DEMAND_NOT_MEASURED: 2}


@dataclass(frozen=True, slots=True)
class MerchantFinding:
    """One repeated or material issue, stated once for every mission it covers.

    `key` is stable within one engine version and one set of evidence: it is what an API
    consumer uses to address this finding and what a future report compares against. It is
    derived entirely from the grouping inputs, never generated, so the same facts always
    produce the same key.
    """

    key: str
    code: DiagnosticCode
    owner: DiagnosticOwner
    actionability: Actionability
    severity: Severity
    evidence_level: EvidenceLevel
    title: str
    recommendation: str | None
    mission_run_ids: tuple[uuid.UUID, ...]
    mission_keys: tuple[str, ...]
    product_ids: tuple[uuid.UUID, ...]
    variant_ids: tuple[uuid.UUID, ...]
    attribute_keys: tuple[str, ...]
    simulated_demand: tuple[SimulatedDemandEffect, ...]


def aggregate_findings(diagnoses: list[MissionDiagnosis]) -> tuple[MerchantFinding, ...]:
    """Aggregate mission diagnoses into ordered merchant findings.

    Secondary observations such as recovered throttles are included: they are real history a
    merchant may want when interpreting a run, and they carry NO_MERCHANT_ACTION so they can
    never masquerade as work. Missions without findings contribute nothing.
    """
    members_by_key: dict[str, list[MissionFinding]] = defaultdict(list)
    diagnoses_by_key: dict[str, list[MissionDiagnosis]] = defaultdict(list)
    for diagnosis in diagnoses:
        seen_in_mission: set[str] = set()
        for finding in diagnosis.findings:
            key = _group_key(finding)
            if key in seen_in_mission:
                continue
            seen_in_mission.add(key)
            members_by_key[key].append(finding)
            diagnoses_by_key[key].append(diagnosis)

    def order(key: str) -> tuple[int, int, int, str]:
        identity = identity_for(members_by_key[key][0].code)
        return (
            -identity.severity.rank,
            -len(diagnoses_by_key[key]),
            precedence_rank(identity.code),
            key,
        )

    return tuple(
        _finding(key, members_by_key[key], diagnoses_by_key[key])
        for key in sorted(members_by_key, key=order)
    )


def _group_key(finding: MissionFinding) -> str:
    if finding.code in _ATTRIBUTE_SCOPED_CODES and finding.attribute_keys:
        attributes = ":".join(sorted(finding.attribute_keys))
        return f"{finding.code.value}:{attributes}"
    return finding.code.value


def _finding(
    key: str,
    members: list[MissionFinding],
    contributing: list[MissionDiagnosis],
) -> MerchantFinding:
    """Build one grouped finding from every mission that produced it."""
    first = members[0]
    identity = identity_for(first.code)
    mission_run_ids = tuple(dict.fromkeys(diagnosis.mission_run_id for diagnosis in contributing))
    mission_keys = tuple(dict.fromkeys(diagnosis.mission_key for diagnosis in contributing))
    return MerchantFinding(
        key=key,
        code=identity.code,
        owner=identity.owner,
        actionability=first.actionability,
        severity=highest_severity(member.severity for member in members),
        evidence_level=identity.evidence_level,
        title=_title(
            key,
            len(mission_run_ids),
            tuple(_unique(attribute for member in members for attribute in member.attribute_keys)),
        ),
        recommendation=_recommendation(members),
        mission_run_ids=mission_run_ids,
        mission_keys=mission_keys,
        product_ids=tuple(_unique(identifier for m in members for identifier in m.product_ids)),
        variant_ids=tuple(_unique(identifier for m in members for identifier in m.variant_ids)),
        attribute_keys=tuple(
            _unique(attribute for member in members for attribute in member.attribute_keys)
        ),
        simulated_demand=_demand(contributing, key),
    )


def _unique[T](items: Iterable[T]) -> list[T]:
    """First occurrence order, preserved, so grouped links never depend on set ordering."""
    seen: list[T] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


def _title(key: str, missions: int, attributes: tuple[str, ...]) -> str:
    subject = f"{missions} mission(s)" if missions != 1 else "1 mission"
    if attributes:
        names = ", ".join(sorted(attributes))
        verb = (
            "was not published for"
            if key.startswith("ATTRIBUTE_NOT_PUBLISHED")
            else "could not be read for"
        )
        return f"Required attribute '{names}' {verb} {subject}."
    code = DiagnosticCode(key.split(":", 1)[0])
    owner_label = identity_for(code).owner.value.replace("_", " ").title()
    return f"{code.value} on {subject} ({owner_label})."


def _recommendation(members: list[MissionFinding]) -> str | None:
    for member in members:
        if member.recommendation is not None:
            return member.recommendation
    return None


def _demand(
    contributing: list[MissionDiagnosis],
    key: str,
) -> tuple[SimulatedDemandEffect, ...]:
    """Simulated demand behind one finding, attributed through each mission's lead diagnosis.

    Only missions whose primary diagnosis resolves to this finding contribute their demand,
    matched on the full grouping key, so two attribute gaps never split one mission's value
    between them and an unresolved mission's value stays attached to its unresolved finding
    instead of being handed to whoever else was mentioned beside it.
    """
    totals: dict[tuple[str, str], int] = defaultdict(int)
    for diagnosis in contributing:
        lead = next((f for f in diagnosis.findings if _group_key(f) == key), None)
        if lead is None or diagnosis.primary is None or diagnosis.primary.code != lead.code:
            continue
        for effect in diagnosis.simulated_demand:
            totals[(effect.currency, effect.bucket)] += effect.amount_minor

    def order(pair: tuple[str, str]) -> tuple[str, int]:
        currency, bucket = pair
        return currency, _DEMAND_BUCKET_ORDER[bucket]

    return tuple(
        SimulatedDemandEffect(currency=pair[0], bucket=pair[1], amount_minor=totals[pair])
        for pair in sorted(totals, key=order)
    )
