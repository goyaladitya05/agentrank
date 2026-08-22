"""Raw counts over one benchmark run, and nothing weighted.

There is deliberately no AgentRank score here. A weighted headline number is a claim about what
matters and by how much, and this project has not run a benchmark yet, so any weights chosen now
would be invented rather than learned. What this module produces is counts, the ratios those
counts obviously support, and simulated demand by currency. The score gets its own phase after
there are outputs to define it against.

Everything is a pure function over plain data. Nothing here reads a database, and the
projection from stored rows happens elsewhere, so a metric can be tested against a hand written
outcome list and a wrong number cannot be blamed on a fixture.

Three rules keep the numbers honest.

Denominators come from the suite, not from what happened. `purchase_missions` counts every
mission whose ground truth says a purchase was available, including ones that errored or never
ran. That is what makes two runs of one suite comparable: the denominator is fixed by the
workload, so a flaky harness lowers a rate rather than quietly moving the bar it is measured
against. `missions_errored` and `missions_unfinished` are reported beside every rate rather than
folded into one, and a reader who wants a different denominator has the counts to build it.

Safety is counted from its own flags rather than from whichever failure reason happened to be
primary, so no reordering of the precedence tuple can hide an escape. Denials are split by
whether the attempt was outside what the buyer authorized, because a denial that stopped an
unauthorized purchase is the safety layer working and a denial of a compliant one is a finding.

And simulated demand is never summed across currencies. It is reported per currency, and the
one accessor that would produce a single figure refuses when a run spans more than one.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from agentrank_api.benchmark.definitions import ExpectedOutcome
from agentrank_api.benchmark.failures import FAILURE_PRECEDENCE, FailureReason
from agentrank_api.benchmark.lifecycle import TERMINAL_MISSION_STATUSES, MissionRunStatus
from agentrank_api.money import validate_currency


@dataclass(frozen=True, slots=True)
class MissionOutcome:
    """One mission and what became of it, flattened to the facts a metric reads.

    A plain record rather than an ORM row, so every function below is pure and testable
    without a database, and so that a stored run and a run held only in memory produce the same
    numbers through the same code.
    """

    mission_key: str
    expected_outcome: ExpectedOutcome
    simulated_value_amount_minor: int
    currency: str
    status: MissionRunStatus
    failure_reasons: tuple[FailureReason, ...] = ()
    unsafe_attempt: bool = False
    unverified_attempt: bool = False
    unsafe_completion: bool = False
    oracle_confirmed: bool | None = None

    def __post_init__(self) -> None:
        validate_currency(self.currency)

    @property
    def purchase_was_available(self) -> bool:
        return self.expected_outcome is ExpectedOutcome.PURCHASE_AVAILABLE

    @property
    def is_finished(self) -> bool:
        return self.status in TERMINAL_MISSION_STATUSES


@dataclass(frozen=True, slots=True)
class SimulatedDemand:
    """Authored buyer demand for one currency, and how much of it the merchant served.

    Simulated, and the word is in the type name because it has to survive being quoted. No money
    moves in a benchmark run, none of these figures is revenue, and the assumptions behind them
    are written down in docs/benchmark.md rather than left to whoever reads the number.

    The partition is three ways rather than two. `lost` is demand a merchant could have served
    and did not. `not_measured` is demand nobody found out about, because the mission errored in
    the harness or never finished, and billing our own infrastructure to the merchant would be
    the easiest possible way to make a report look worse than the truth.
    """

    currency: str
    potential_amount_minor: int
    captured_amount_minor: int
    not_measured_amount_minor: int

    def __post_init__(self) -> None:
        validate_currency(self.currency)
        measured = self.captured_amount_minor + self.not_measured_amount_minor
        if measured > self.potential_amount_minor:
            # Captured demand comes from missions that succeeded, and a mission only succeeds
            # when its ground truth said a purchase was available, so this cannot happen from
            # data this application produced. It is checked because it is the arithmetic that
            # every figure below rests on.
            raise ValueError(
                f"captured and unmeasured demand exceed the potential: {measured}"
                f" against {self.potential_amount_minor} {self.currency}"
            )

    @property
    def lost_amount_minor(self) -> int:
        """Demand the merchant could have served and did not.

        Derived rather than stored, so it cannot disagree with the two figures it is the
        remainder of. It excludes what was never measured, which is the difference between a
        merchant failing and a benchmark failing.
        """
        return (
            self.potential_amount_minor
            - self.captured_amount_minor
            - self.not_measured_amount_minor
        )


@dataclass(frozen=True, slots=True)
class SimulatedDemandReport:
    """Simulated demand for every currency the run's missions were denominated in.

    A tuple rather than a total, because a total across currencies would be a number with no
    unit. There is no addition anywhere in this type and no accessor that hides one.
    """

    by_currency: tuple[SimulatedDemand, ...] = ()

    def for_currency(self, currency: str) -> SimulatedDemand | None:
        for entry in self.by_currency:
            if entry.currency == currency:
                return entry
        return None

    def single_currency(self) -> SimulatedDemand:
        """The one entry, for a report that has decided it only handles one currency.

        Raises rather than summing when there is more than one. This is the refusal that makes
        "never aggregate across currencies" a mechanism instead of a note: a caller that wanted
        one figure has to say which currency, or be told it cannot have one.
        """
        if len(self.by_currency) != 1:
            spanned = ", ".join(entry.currency for entry in self.by_currency) or "none"
            raise ValueError(
                f"simulated demand spans {len(self.by_currency)} currencies ({spanned})"
                " and cannot be reduced to one figure"
            )
        return self.by_currency[0]


@dataclass(frozen=True, slots=True)
class BenchmarkMetrics:
    """What one run measured, as counts.

    Every field is a count except the two ratios, and both ratios are `None` rather than zero
    when their denominator is empty, because a rate over nothing is not zero.
    """

    missions_total: int = 0
    missions_succeeded: int = 0
    missions_failed: int = 0
    missions_abstained: int = 0
    missions_errored: int = 0
    missions_unfinished: int = 0

    # Denominators, fixed by the suite rather than by what happened.
    purchase_missions: int = 0
    control_missions: int = 0

    correct_abstentions: int = 0
    incorrect_abstentions: int = 0

    unsafe_attempts: int = 0
    unverified_attempts: int = 0
    unsafe_completions: int = 0
    mandate_denials_protecting: int = 0
    mandate_denials_on_compliant_attempt: int = 0

    oracle_disagreements: int = 0
    oracle_unchecked: int = 0

    primary_failure_counts: Mapping[FailureReason, int] = field(default_factory=dict)
    contributing_failure_counts: Mapping[FailureReason, int] = field(default_factory=dict)
    simulated_demand: SimulatedDemandReport = field(default_factory=SimulatedDemandReport)

    @property
    def task_completion_rate(self) -> float | None:
        """Compliant purchases over the missions whose ground truth offered one.

        The denominator is every mission the suite says a purchase was available for, whether or
        not it ran. That is what keeps two runs of one suite comparable: a harness that crashed
        on half of them produces a lower rate rather than a rate over a smaller workload.
        `missions_errored` and `missions_unfinished` are the numbers that say how much to trust
        it, and they are always reported beside it.

        None when the suite offers no purchasable mission, because a rate over nothing is not
        zero and is not one.
        """
        if self.purchase_missions == 0:
            return None
        return self.missions_succeeded / self.purchase_missions

    @property
    def correct_abstention_rate(self) -> float | None:
        """Correct declines over the missions where declining was the right answer.

        The other half of the picture, and it needs its own denominator: control missions are
        deliberately not in the task completion denominator, because counting a mission nobody
        could complete as a completion opportunity would inflate it.
        """
        if self.control_missions == 0:
            return None
        return self.correct_abstentions / self.control_missions


def compute_metrics(outcomes: Sequence[MissionOutcome]) -> BenchmarkMetrics:
    """Count one run.

    Pure, and order independent apart from the two failure count mappings, which are built in
    the declared precedence order so that two identical runs produce identical reports rather
    than reports that differ by dictionary insertion.
    """
    statuses = [outcome.status for outcome in outcomes]
    purchase_missions = [outcome for outcome in outcomes if outcome.purchase_was_available]
    control_missions = [outcome for outcome in outcomes if not outcome.purchase_was_available]

    abstentions = [outcome for outcome in outcomes if outcome.status is MissionRunStatus.ABSTAINED]
    denied = [
        outcome for outcome in outcomes if FailureReason.MANDATE_DENIED in outcome.failure_reasons
    ]

    return BenchmarkMetrics(
        missions_total=len(outcomes),
        missions_succeeded=statuses.count(MissionRunStatus.SUCCEEDED),
        missions_failed=statuses.count(MissionRunStatus.FAILED),
        missions_abstained=len(abstentions),
        missions_errored=statuses.count(MissionRunStatus.ERRORED),
        missions_unfinished=sum(1 for outcome in outcomes if not outcome.is_finished),
        purchase_missions=len(purchase_missions),
        control_missions=len(control_missions),
        # A correct abstention is one with nothing to explain. An abstention on a control
        # mission that also hit a broken merchant endpoint carries a reason, and counting it as
        # correct would credit the merchant for a decline it made necessary.
        correct_abstentions=sum(1 for outcome in abstentions if not outcome.failure_reasons),
        incorrect_abstentions=sum(1 for outcome in abstentions if outcome.failure_reasons),
        unsafe_attempts=sum(1 for outcome in outcomes if outcome.unsafe_attempt),
        unverified_attempts=sum(1 for outcome in outcomes if outcome.unverified_attempt),
        unsafe_completions=sum(1 for outcome in outcomes if outcome.unsafe_completion),
        # A denial is only the safety layer working when what it stopped was outside what the
        # buyer authorized. The other kind is a finding about the merchant or its
        # representation, and one number covering both would read as a system that fails a lot.
        mandate_denials_protecting=sum(1 for outcome in denied if outcome.unsafe_attempt),
        mandate_denials_on_compliant_attempt=sum(
            1 for outcome in denied if not outcome.unsafe_attempt
        ),
        oracle_disagreements=sum(1 for outcome in outcomes if outcome.oracle_confirmed is False),
        oracle_unchecked=sum(1 for outcome in outcomes if outcome.oracle_confirmed is None),
        primary_failure_counts=_counted(
            outcome.failure_reasons[0] for outcome in outcomes if outcome.failure_reasons
        ),
        contributing_failure_counts=_counted(
            reason for outcome in outcomes for reason in outcome.failure_reasons
        ),
        simulated_demand=_simulated_demand(outcomes),
    )


def _counted(reasons: Iterable[FailureReason]) -> Mapping[FailureReason, int]:
    """Count reasons, and present them in the declared precedence order.

    Only what actually occurred, so a report is not padded with eighteen zeroes, and always in
    one order, so two identical runs produce byte identical reports.
    """
    tally: dict[FailureReason, int] = {}
    for reason in reasons:
        tally[reason] = tally.get(reason, 0) + 1
    return {reason: tally[reason] for reason in FAILURE_PRECEDENCE if reason in tally}


def _simulated_demand(outcomes: Sequence[MissionOutcome]) -> SimulatedDemandReport:
    """Group authored demand by currency, and split it three ways.

    Only missions whose ground truth offered a purchase carry demand, so a currency that appears
    solely on control missions does not appear here at all: there was never anything to serve
    in it. Currencies are ordered alphabetically so the report is stable.
    """
    potential: dict[str, int] = {}
    captured: dict[str, int] = {}
    unmeasured: dict[str, int] = {}

    for outcome in outcomes:
        if not outcome.purchase_was_available:
            continue
        value = outcome.simulated_value_amount_minor
        potential[outcome.currency] = potential.get(outcome.currency, 0) + value
        if outcome.status is MissionRunStatus.SUCCEEDED:
            captured[outcome.currency] = captured.get(outcome.currency, 0) + value
        elif outcome.status is MissionRunStatus.ERRORED or not outcome.is_finished:
            unmeasured[outcome.currency] = unmeasured.get(outcome.currency, 0) + value

    return SimulatedDemandReport(
        by_currency=tuple(
            SimulatedDemand(
                currency=currency,
                potential_amount_minor=potential[currency],
                captured_amount_minor=captured.get(currency, 0),
                not_measured_amount_minor=unmeasured.get(currency, 0),
            )
            for currency in sorted(potential)
        )
    )
