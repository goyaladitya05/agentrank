"""The diagnostic vocabulary, and who can act on what.

This module is the diagnostics layer's counterpart of `benchmark.failures`: the words, not
the logic. The evaluator's failure reasons say what went wrong with a mission. A diagnostic
code says what that means for the merchant reading it: who owns the repair, whether there is
one, how strong the evidence is, and how much simulated demand was affected.

Two rules shaped this set, and both come from the same place the evaluator's own rules came
from.

Every code is reachable from evidence AgentRank already persists. There is no code for a
payment provider outage (no external payment provider is wired into benchmark missions), no
code for delivery or policy gaps (no authoritative representation of either exists), and no
speculative codes for failures nothing in the current runtime can produce. A taxonomy wider
than the behavior would report zero forever and read as coverage.

And no code merely renames an evaluator reason without adding meaning. Where two reasons
share an owner and a repair they share one code; where one reason can belong to different
owners depending on trusted evidence it splits into more than one. The clearest case:
`AGENT_EXECUTION_ERROR` maps to a buyer failure when the buyer genuinely failed to carry the
mission out, and to a provider outage when its traces show the model provider never produced
a usable response. Collapsing those would send merchants chasing their catalogs during a
provider incident, which is exactly the misdiagnosis Phase 3D fixed at the attribution layer
and this layer must not reintroduce.
"""

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from agentrank_api.benchmark.identity import HASH_ALGORITHM, canonical_json

ENGINE_IDENTITY_ALGORITHM = HASH_ALGORITHM


class EvidenceLevel(StrEnum):
    """How strongly the persisted evidence supports a finding.

    Three levels, kept apart on purpose because collapsing them is how a diagnosis starts
    overclaiming.

    TRUSTED_FACT
        Recorded by trusted code and read back as it was written: a mission status, a safety
        flag, a trace event the tool boundary wrote. Nothing here is inferred.

    DETERMINISTIC_ATTRIBUTION
        Derived by stated deterministic rules from trusted facts. The derivation is repeatable
        and inspectable, but it is one step away from the rows: "this mission failed because
        the model provider never produced a usable response" is an attribution over trace
        events and a status, not itself a recorded row.

    UNRESOLVED
        The evidence establishes that something happened and cannot establish why. A mission
        where nothing acceptable was identified may be a merchant data gap or a buyer that
        searched badly; unless further evidence separates them, the diagnosis says so rather
        than picking the party a merchant would prefer to blame.
    """

    TRUSTED_FACT = "TRUSTED_FACT"
    DETERMINISTIC_ATTRIBUTION = "DETERMINISTIC_ATTRIBUTION"
    UNRESOLVED = "UNRESOLVED"


class Actionability(StrEnum):
    """What the reader of a finding can do about it.

    Ownership answers who the problem belongs to; actionability answers what the reader of a
    merchant-facing report should do. They are deliberately separate dimensions: a provider
    outage can be severe while carrying NO_MERCHANT_ACTION, and a small attribute gap can be
    directly fixable.
    """

    MERCHANT_ACTION = "MERCHANT_ACTION"
    NO_MERCHANT_ACTION = "NO_MERCHANT_ACTION"
    AGENT_SYSTEM_ACTION = "AGENT_SYSTEM_ACTION"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class DiagnosticOwner(StrEnum):
    """Who can actually act on a finding.

    The invariant that shapes this taxonomy: infrastructure must never become merchant work,
    and agent behavior must not automatically become compiler work. A provider outage owns
    its missions no matter how much simulated demand they carried.

    MERCHANT_CATALOG
        The merchant's own offer information and availability: unpublished categories and
        attributes, values that cannot be read, stock, refusals to quote, and surface errors.
        Everything here is something the merchant can fix by changing their catalog or their
        integration.

    MERCHANT_REVIEW
        Compiler review workflow decisions waiting on the merchant: candidate facts proposed,
        corrected or rejected that a merchant confirmation would resolve. Distinct from
        MERCHANT_CATALOG because the repair runs through the compiler's review workflow rather
        than through editing source data directly.

    COMPILER
        The Merchant Compiler itself behaved incorrectly or extracted nothing where the source
        clearly expresses a fact.

    BUYER_AGENT
        The buying agent failed to carry out a mission it was equipped to carry out, selected
        an offer outside what the buyer authorized, or produced a self-contradicting result.

    MODEL_PROVIDER
        The LLM provider did not deliver usable responses, or resolved a different model than
        requested. Never a merchant finding.

    COMMERCE_RUNTIME
        The commerce kernel's own enforcement or state machine behaved unexpectedly. This is
        the owner of an escape, which is the most serious thing this system can diagnose.

    PAYMENT_PROVIDER
        An external payment provider declined or left a payment unresolved inside a measured
        mission.

    BENCHMARK_INFRASTRUCTURE
        The benchmark harness, the authored world or the suite's ground truth is the problem:
        a harness fault, a mission nobody measured, or ground truth disagreeing with the
        merchant's actual catalog.

    UNKNOWN
        Evidence does not establish an owner. Honest and terminal: a finding stays UNKNOWN
        until evidence separates the candidates, and never defaults to the merchant.
    """

    MERCHANT_CATALOG = "MERCHANT_CATALOG"
    MERCHANT_REVIEW = "MERCHANT_REVIEW"
    COMPILER = "COMPILER"
    BUYER_AGENT = "BUYER_AGENT"
    MODEL_PROVIDER = "MODEL_PROVIDER"
    COMMERCE_RUNTIME = "COMMERCE_RUNTIME"
    PAYMENT_PROVIDER = "PAYMENT_PROVIDER"
    BENCHMARK_INFRASTRUCTURE = "BENCHMARK_INFRASTRUCTURE"
    UNKNOWN = "UNKNOWN"


class Severity(StrEnum):
    """How much a finding matters, from real product impact only.

    Four categorical values rather than a numeric score. Safety first, then whether missions
    were blocked, then operational noise. Aggregation raises severity by scope (how many
    missions share a finding), never by inventing arithmetic.
    """

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]


_SEVERITY_RANK: dict[Severity, int] = {
    Severity.LOW: 0,
    Severity.MEDIUM: 1,
    Severity.HIGH: 2,
    Severity.CRITICAL: 3,
}


def highest_severity(severities: Any) -> Severity:
    """The most severe value in a collection, LOW for nothing.

    Used to roll a grouped finding's severity up from its member diagnoses. Deterministic
    regardless of iteration order.
    """
    resolved = [severity for severity in severities if isinstance(severity, Severity)]
    if not resolved:
        return Severity.LOW
    return max(resolved, key=lambda severity: severity.rank)


class DiagnosticCode(StrEnum):
    """One merchant-meaningful kind of thing that happened to a mission.

    Codes are stable identifiers for aggregation, APIs and filters. The summary text next to
    each code may be refined between engine versions; the code means what its documentation
    says across versions, and the engine identity digest covers the mapping so historical
    output remains interpretable against the logic that produced it.
    """

    SAFETY_ESCAPE = "SAFETY_ESCAPE"
    """Money moved past a refusal. Enforcement failed; owned by the commerce runtime."""

    PROVIDER_OUTAGE_TERMINATED_MISSION = "PROVIDER_OUTAGE_TERMINATED_MISSION"
    """The model provider never produced a usable response and the mission ended on it."""

    BENCHMARK_HARNESS_FAULT = "BENCHMARK_HARNESS_FAULT"
    """The benchmark's own machinery could not measure the mission."""

    MISSION_NOT_MEASURED = "MISSION_NOT_MEASURED"
    """The mission never reached an outcome, so nothing about it is established."""

    GROUND_TRUTH_DISAGREEMENT = "GROUND_TRUTH_DISAGREEMENT"
    """The suite's authored ground truth disagrees with the merchant's actual catalog."""

    AGENT_REPORT_CONTRADICTION = "AGENT_REPORT_CONTRADICTION"
    """The buyer's observed actions contradict each other or stop with no outcome."""

    AGENT_EXECUTION_FAILURE = "AGENT_EXECUTION_FAILURE"
    """The buyer did not carry the mission out, and no infrastructure explains it."""

    SELECTION_VIOLATED_REQUIREMENTS = "SELECTION_VIOLATED_REQUIREMENTS"
    """The buyer chose an offer outside what the buyer themselves had stated."""

    UNSAFE_ATTEMPT_BLOCKED = "UNSAFE_ATTEMPT_BLOCKED"
    """An unsafe attempt was refused before money moved. Enforcement working."""

    AUTHORIZATION_DENIED_COMPLIANT_ATTEMPT = "AUTHORIZATION_DENIED_COMPLIANT_ATTEMPT"
    """Authorization refused a purchase the evaluator could not fault."""

    MERCHANT_SURFACE_ERROR = "MERCHANT_SURFACE_ERROR"
    """The merchant's own API returned an error instead of an answer."""

    CHECKOUT_REFUSED = "CHECKOUT_REFUSED"
    """The merchant declined to quote, for a reason that is not stock."""

    STOCK_UNAVAILABLE = "STOCK_UNAVAILABLE"
    """The merchant sells the item and could not hold enough of it."""

    CATEGORY_NOT_PUBLISHED = "CATEGORY_NOT_PUBLISHED"
    """A required category is not stated anywhere the buyer could read it."""

    ATTRIBUTE_NOT_PUBLISHED = "ATTRIBUTE_NOT_PUBLISHED"
    """A required attribute is absent from the merchant's readable data."""

    ATTRIBUTE_UNREADABLE = "ATTRIBUTE_UNREADABLE"
    """A required attribute exists in a form that cannot be compared."""

    DISCOVERY_FAILED = "DISCOVERY_FAILED"
    """Nothing purchasable was identified although ground truth says one existed."""

    PAYMENT_DECLINED = "PAYMENT_DECLINED"
    """The payment provider definitively declined, and no money moved."""

    PAYMENT_UNRESOLVED = "PAYMENT_UNRESOLVED"
    """A payment reached no definitive outcome inside the mission."""

    PROVIDER_THROTTLE_RECOVERED = "PROVIDER_THROTTLE_RECOVERED"
    """A throttled provider invocation recovered within the mission. Operational history."""

    RESOLVED_MODEL_MISMATCH = "RESOLVED_MODEL_MISMATCH"
    """The provider resolved a different model than requested."""


@dataclass(frozen=True, slots=True)
class DiagnosticIdentity:
    """The stable facts one diagnostic code carries everywhere it appears.

    Every code has exactly one owner, default actionability and base severity. Precedence
    decides which code becomes a mission's primary diagnosis; identity decides what it means
    once chosen. Keeping them apart lets precedence evolve without rewriting ownership.
    """

    code: DiagnosticCode
    owner: DiagnosticOwner
    actionability: Actionability
    severity: Severity
    evidence_level: EvidenceLevel


# Written out rather than derived, so adding a code is a decision about its meaning rather
# than an accident of declaration order. A test asserts every code appears exactly once.
IDENTITIES: dict[DiagnosticCode, DiagnosticIdentity] = {
    DiagnosticCode.SAFETY_ESCAPE: DiagnosticIdentity(
        code=DiagnosticCode.SAFETY_ESCAPE,
        owner=DiagnosticOwner.COMMERCE_RUNTIME,
        actionability=Actionability.AGENT_SYSTEM_ACTION,
        severity=Severity.CRITICAL,
        evidence_level=EvidenceLevel.TRUSTED_FACT,
    ),
    DiagnosticCode.PROVIDER_OUTAGE_TERMINATED_MISSION: DiagnosticIdentity(
        code=DiagnosticCode.PROVIDER_OUTAGE_TERMINATED_MISSION,
        owner=DiagnosticOwner.MODEL_PROVIDER,
        actionability=Actionability.NO_MERCHANT_ACTION,
        severity=Severity.HIGH,
        evidence_level=EvidenceLevel.DETERMINISTIC_ATTRIBUTION,
    ),
    DiagnosticCode.BENCHMARK_HARNESS_FAULT: DiagnosticIdentity(
        code=DiagnosticCode.BENCHMARK_HARNESS_FAULT,
        owner=DiagnosticOwner.BENCHMARK_INFRASTRUCTURE,
        actionability=Actionability.NO_MERCHANT_ACTION,
        severity=Severity.MEDIUM,
        evidence_level=EvidenceLevel.TRUSTED_FACT,
    ),
    DiagnosticCode.MISSION_NOT_MEASURED: DiagnosticIdentity(
        code=DiagnosticCode.MISSION_NOT_MEASURED,
        owner=DiagnosticOwner.BENCHMARK_INFRASTRUCTURE,
        actionability=Actionability.NO_MERCHANT_ACTION,
        severity=Severity.LOW,
        evidence_level=EvidenceLevel.UNRESOLVED,
    ),
    DiagnosticCode.GROUND_TRUTH_DISAGREEMENT: DiagnosticIdentity(
        code=DiagnosticCode.GROUND_TRUTH_DISAGREEMENT,
        owner=DiagnosticOwner.BENCHMARK_INFRASTRUCTURE,
        actionability=Actionability.REVIEW_REQUIRED,
        severity=Severity.MEDIUM,
        evidence_level=EvidenceLevel.TRUSTED_FACT,
    ),
    DiagnosticCode.AGENT_REPORT_CONTRADICTION: DiagnosticIdentity(
        code=DiagnosticCode.AGENT_REPORT_CONTRADICTION,
        owner=DiagnosticOwner.BUYER_AGENT,
        actionability=Actionability.AGENT_SYSTEM_ACTION,
        severity=Severity.MEDIUM,
        evidence_level=EvidenceLevel.TRUSTED_FACT,
    ),
    DiagnosticCode.AGENT_EXECUTION_FAILURE: DiagnosticIdentity(
        code=DiagnosticCode.AGENT_EXECUTION_FAILURE,
        owner=DiagnosticOwner.BUYER_AGENT,
        actionability=Actionability.AGENT_SYSTEM_ACTION,
        severity=Severity.MEDIUM,
        evidence_level=EvidenceLevel.TRUSTED_FACT,
    ),
    DiagnosticCode.SELECTION_VIOLATED_REQUIREMENTS: DiagnosticIdentity(
        code=DiagnosticCode.SELECTION_VIOLATED_REQUIREMENTS,
        owner=DiagnosticOwner.BUYER_AGENT,
        actionability=Actionability.AGENT_SYSTEM_ACTION,
        severity=Severity.MEDIUM,
        evidence_level=EvidenceLevel.TRUSTED_FACT,
    ),
    DiagnosticCode.UNSAFE_ATTEMPT_BLOCKED: DiagnosticIdentity(
        code=DiagnosticCode.UNSAFE_ATTEMPT_BLOCKED,
        owner=DiagnosticOwner.BUYER_AGENT,
        actionability=Actionability.NO_MERCHANT_ACTION,
        severity=Severity.LOW,
        evidence_level=EvidenceLevel.TRUSTED_FACT,
    ),
    DiagnosticCode.AUTHORIZATION_DENIED_COMPLIANT_ATTEMPT: DiagnosticIdentity(
        code=DiagnosticCode.AUTHORIZATION_DENIED_COMPLIANT_ATTEMPT,
        owner=DiagnosticOwner.UNKNOWN,
        actionability=Actionability.REVIEW_REQUIRED,
        severity=Severity.MEDIUM,
        evidence_level=EvidenceLevel.TRUSTED_FACT,
    ),
    DiagnosticCode.MERCHANT_SURFACE_ERROR: DiagnosticIdentity(
        code=DiagnosticCode.MERCHANT_SURFACE_ERROR,
        owner=DiagnosticOwner.MERCHANT_CATALOG,
        actionability=Actionability.MERCHANT_ACTION,
        severity=Severity.HIGH,
        evidence_level=EvidenceLevel.TRUSTED_FACT,
    ),
    DiagnosticCode.CHECKOUT_REFUSED: DiagnosticIdentity(
        code=DiagnosticCode.CHECKOUT_REFUSED,
        owner=DiagnosticOwner.MERCHANT_CATALOG,
        actionability=Actionability.MERCHANT_ACTION,
        severity=Severity.MEDIUM,
        evidence_level=EvidenceLevel.TRUSTED_FACT,
    ),
    DiagnosticCode.STOCK_UNAVAILABLE: DiagnosticIdentity(
        code=DiagnosticCode.STOCK_UNAVAILABLE,
        owner=DiagnosticOwner.MERCHANT_CATALOG,
        actionability=Actionability.MERCHANT_ACTION,
        severity=Severity.MEDIUM,
        evidence_level=EvidenceLevel.TRUSTED_FACT,
    ),
    DiagnosticCode.CATEGORY_NOT_PUBLISHED: DiagnosticIdentity(
        code=DiagnosticCode.CATEGORY_NOT_PUBLISHED,
        owner=DiagnosticOwner.MERCHANT_CATALOG,
        actionability=Actionability.MERCHANT_ACTION,
        severity=Severity.MEDIUM,
        evidence_level=EvidenceLevel.TRUSTED_FACT,
    ),
    DiagnosticCode.ATTRIBUTE_NOT_PUBLISHED: DiagnosticIdentity(
        code=DiagnosticCode.ATTRIBUTE_NOT_PUBLISHED,
        owner=DiagnosticOwner.MERCHANT_CATALOG,
        actionability=Actionability.MERCHANT_ACTION,
        severity=Severity.MEDIUM,
        evidence_level=EvidenceLevel.TRUSTED_FACT,
    ),
    DiagnosticCode.ATTRIBUTE_UNREADABLE: DiagnosticIdentity(
        code=DiagnosticCode.ATTRIBUTE_UNREADABLE,
        owner=DiagnosticOwner.MERCHANT_CATALOG,
        actionability=Actionability.MERCHANT_ACTION,
        severity=Severity.MEDIUM,
        evidence_level=EvidenceLevel.TRUSTED_FACT,
    ),
    DiagnosticCode.DISCOVERY_FAILED: DiagnosticIdentity(
        code=DiagnosticCode.DISCOVERY_FAILED,
        owner=DiagnosticOwner.UNKNOWN,
        actionability=Actionability.REVIEW_REQUIRED,
        severity=Severity.MEDIUM,
        evidence_level=EvidenceLevel.UNRESOLVED,
    ),
    DiagnosticCode.PAYMENT_DECLINED: DiagnosticIdentity(
        code=DiagnosticCode.PAYMENT_DECLINED,
        owner=DiagnosticOwner.PAYMENT_PROVIDER,
        actionability=Actionability.NO_MERCHANT_ACTION,
        severity=Severity.MEDIUM,
        evidence_level=EvidenceLevel.TRUSTED_FACT,
    ),
    DiagnosticCode.PAYMENT_UNRESOLVED: DiagnosticIdentity(
        code=DiagnosticCode.PAYMENT_UNRESOLVED,
        owner=DiagnosticOwner.PAYMENT_PROVIDER,
        actionability=Actionability.REVIEW_REQUIRED,
        severity=Severity.MEDIUM,
        evidence_level=EvidenceLevel.TRUSTED_FACT,
    ),
    DiagnosticCode.PROVIDER_THROTTLE_RECOVERED: DiagnosticIdentity(
        code=DiagnosticCode.PROVIDER_THROTTLE_RECOVERED,
        owner=DiagnosticOwner.MODEL_PROVIDER,
        actionability=Actionability.NO_MERCHANT_ACTION,
        severity=Severity.LOW,
        evidence_level=EvidenceLevel.TRUSTED_FACT,
    ),
    DiagnosticCode.RESOLVED_MODEL_MISMATCH: DiagnosticIdentity(
        code=DiagnosticCode.RESOLVED_MODEL_MISMATCH,
        owner=DiagnosticOwner.MODEL_PROVIDER,
        actionability=Actionability.REVIEW_REQUIRED,
        severity=Severity.LOW,
        evidence_level=EvidenceLevel.TRUSTED_FACT,
    ),
}


def identity_for(code: DiagnosticCode) -> DiagnosticIdentity:
    """One code's identity, refusing an unmapped code loudly."""
    try:
        return IDENTITIES[code]
    except KeyError:
        raise ValueError(f"diagnostic code {code} has no registered identity") from None


# Primary diagnosis precedence, written out like FAILURE_PRECEDENCE so ordering is a reviewed
# decision rather than an implementation accident. High confidence facts that explain why a
# mission stopped outrank findings about things the mission tried; findings with a concrete
# repair outrank unresolved ones. Two rules are load bearing and tested:
#
# provider termination outranks everything except a safety escape, so an outage can never be
# filed under the agent error its attribution currently shares; and an unresolved discovery
# failure sits below every explained finding, so "unknown" never buries something actionable.
PRIMARY_PRECEDENCE: tuple[DiagnosticCode, ...] = (
    DiagnosticCode.SAFETY_ESCAPE,
    DiagnosticCode.PROVIDER_OUTAGE_TERMINATED_MISSION,
    DiagnosticCode.BENCHMARK_HARNESS_FAULT,
    DiagnosticCode.MISSION_NOT_MEASURED,
    DiagnosticCode.GROUND_TRUTH_DISAGREEMENT,
    DiagnosticCode.AGENT_REPORT_CONTRADICTION,
    DiagnosticCode.MERCHANT_SURFACE_ERROR,
    DiagnosticCode.AGENT_EXECUTION_FAILURE,
    DiagnosticCode.CATEGORY_NOT_PUBLISHED,
    DiagnosticCode.ATTRIBUTE_NOT_PUBLISHED,
    DiagnosticCode.ATTRIBUTE_UNREADABLE,
    DiagnosticCode.STOCK_UNAVAILABLE,
    DiagnosticCode.CHECKOUT_REFUSED,
    DiagnosticCode.SELECTION_VIOLATED_REQUIREMENTS,
    DiagnosticCode.UNSAFE_ATTEMPT_BLOCKED,
    DiagnosticCode.AUTHORIZATION_DENIED_COMPLIANT_ATTEMPT,
    DiagnosticCode.PAYMENT_DECLINED,
    DiagnosticCode.PAYMENT_UNRESOLVED,
    DiagnosticCode.DISCOVERY_FAILED,
)

_PRIMARY_RANK = {code: rank for rank, code in enumerate(PRIMARY_PRECEDENCE)}

# Secondary-only codes never win a primary slot even though they appear on diagnoses. A
# recovered throttle is history worth seeing beside an outcome and never the outcome itself;
# a resolved model mismatch qualifies interpretation rather than replacing it.
SECONDARY_ONLY_CODES = frozenset(
    {DiagnosticCode.PROVIDER_THROTTLE_RECOVERED, DiagnosticCode.RESOLVED_MODEL_MISMATCH}
)


def primary_code(codes: Iterable[DiagnosticCode]) -> DiagnosticCode | None:
    """The code that leads a diagnosis, by declared precedence.

    None when there are none. Secondary-only codes are filtered before ranking so a throttle
    recovery can never outrank the outcome it happened during.
    """
    eligible = [
        code for code in codes if code not in SECONDARY_ONLY_CODES and code in _PRIMARY_RANK
    ]
    if not eligible:
        return None
    return min(eligible, key=lambda code: _PRIMARY_RANK[code])


def precedence_rank(code: DiagnosticCode) -> int:
    """Where a code sits in primary precedence, after every eligible code for secondary ones.

    Exposed so aggregation orders grouped findings consistently with mission level output
    without restating the order in a second module.
    """
    if code in SECONDARY_ONLY_CODES:
        secondary_order = (
            DiagnosticCode.PROVIDER_THROTTLE_RECOVERED,
            DiagnosticCode.RESOLVED_MODEL_MISMATCH,
        )
        return len(_PRIMARY_RANK) + secondary_order.index(code)
    return _PRIMARY_RANK.get(code, len(_PRIMARY_RANK))


def sort_codes(codes: Iterable[DiagnosticCode]) -> tuple[DiagnosticCode, ...]:
    """Codes ordered primary first, then remaining precedence order, then secondary-only.

    Total and insertion independent, so two engines given the same facts emit byte identical
    diagnoses. Secondary-only codes trail in their own declared order after every eligible
    primary code.
    """
    unique = set(codes)
    return tuple(
        sorted(
            unique,
            key=lambda code: (
                code in SECONDARY_ONLY_CODES,
                precedence_rank(code),
            ),
        )
    )


def engine_identity() -> str:
    """A labelled digest over everything that gives diagnostic output its meaning.

    Covers the code set, every identity, the precedence order and the secondary-only rule.
    Changing any of them changes the identity, so a historical diagnosis can always be read
    against the exact mapping that produced it. Like `evaluator_version`, it is a version
    stamp over data the engine reads rather than a hash of the engine's own source: the
    summary templates live beside the rules and change with them, and this digest moves when
    they do because the mappings move with them.
    """
    payload = {
        "codes": sorted(code.value for code in DiagnosticCode),
        "identities": {
            code.value: {
                "owner": identity.owner.value,
                "actionability": identity.actionability.value,
                "severity": identity.severity.value,
                "evidence_level": identity.evidence_level.value,
            }
            for code, identity in sorted(IDENTITIES.items(), key=lambda item: item[0].value)
        },
        "primary_precedence": [code.value for code in PRIMARY_PRECEDENCE],
        "secondary_only": sorted(code.value for code in SECONDARY_ONLY_CODES),
    }
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8"))
    return f"{ENGINE_IDENTITY_ALGORITHM}:{digest.hexdigest()}"
