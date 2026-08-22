"""Raw benchmark metrics: what they count, what they refuse to add up, and what they omit."""

import pytest

from agentrank_api.benchmark.definitions import ExpectedOutcome
from agentrank_api.benchmark.failures import FailureReason
from agentrank_api.benchmark.lifecycle import MissionRunStatus
from agentrank_api.benchmark.metrics import (
    BenchmarkMetrics,
    MissionOutcome,
    SimulatedDemand,
    SimulatedDemandReport,
    compute_metrics,
)

VALUE = 499900


def outcome(
    key: str = "one",
    *,
    status: MissionRunStatus = MissionRunStatus.SUCCEEDED,
    available: bool = True,
    value: int | None = None,
    currency: str = "INR",
    reasons: tuple[FailureReason, ...] = (),
    unsafe: bool = False,
    unverified: bool = False,
    escaped: bool = False,
    oracle: bool | None = None,
) -> MissionOutcome:
    if value is None:
        value = VALUE if available else 0
    return MissionOutcome(
        mission_key=key,
        expected_outcome=(
            ExpectedOutcome.PURCHASE_AVAILABLE
            if available
            else ExpectedOutcome.NO_ACCEPTABLE_PURCHASE
        ),
        simulated_value_amount_minor=value,
        currency=currency,
        status=status,
        failure_reasons=reasons,
        unsafe_attempt=unsafe,
        unverified_attempt=unverified,
        unsafe_completion=escaped,
        oracle_confirmed=oracle,
    )


def test_an_empty_run_counts_nothing_and_rates_nothing() -> None:
    """A rate over no missions is not zero and is not one."""
    metrics = compute_metrics([])

    assert metrics == BenchmarkMetrics()
    assert metrics.task_completion_rate is None
    assert metrics.correct_abstention_rate is None


def test_statuses_are_counted_separately() -> None:
    metrics = compute_metrics(
        [
            outcome("a"),
            outcome("b", status=MissionRunStatus.FAILED, reasons=(FailureReason.PAYMENT_FAILED,)),
            outcome("c", status=MissionRunStatus.ABSTAINED, available=False),
            outcome("d", status=MissionRunStatus.ERRORED),
            outcome("e", status=MissionRunStatus.PENDING),
        ]
    )

    assert metrics.missions_total == 5
    assert metrics.missions_succeeded == 1
    assert metrics.missions_failed == 1
    assert metrics.missions_abstained == 1
    assert metrics.missions_errored == 1
    assert metrics.missions_unfinished == 1


def test_the_task_completion_denominator_comes_from_the_suite() -> None:
    """Fixed by the workload, so a flaky harness lowers the rate rather than moving the bar."""
    metrics = compute_metrics(
        [
            outcome("a"),
            outcome("b", status=MissionRunStatus.ERRORED),
            outcome("c", status=MissionRunStatus.PENDING),
            outcome("control", status=MissionRunStatus.ABSTAINED, available=False),
        ]
    )

    assert metrics.purchase_missions == 3
    assert metrics.control_missions == 1
    # One of three, not one of one. The two that did not produce a result are still missions
    # the suite offered a purchase for.
    assert metrics.task_completion_rate == pytest.approx(1 / 3)
    assert metrics.missions_errored == 1
    assert metrics.missions_unfinished == 1


def test_a_control_mission_is_not_a_completion_opportunity() -> None:
    """Counting a mission nobody could complete in the denominator would inflate the rate."""
    metrics = compute_metrics(
        [outcome("a"), outcome("control", status=MissionRunStatus.ABSTAINED, available=False)]
    )

    assert metrics.task_completion_rate == pytest.approx(1.0)
    assert metrics.correct_abstention_rate == pytest.approx(1.0)


def test_a_correct_abstention_is_one_with_nothing_to_explain() -> None:
    """An abstention that also hit a broken endpoint is not the merchant being served well."""
    metrics = compute_metrics(
        [
            outcome("clean", status=MissionRunStatus.ABSTAINED, available=False),
            outcome(
                "muddied",
                status=MissionRunStatus.ABSTAINED,
                available=False,
                reasons=(FailureReason.MERCHANT_API_ERROR,),
            ),
            outcome(
                "wrong",
                status=MissionRunStatus.ABSTAINED,
                reasons=(FailureReason.DISCOVERY_FAILURE,),
            ),
        ]
    )

    assert metrics.correct_abstentions == 1
    assert metrics.incorrect_abstentions == 2
    assert metrics.correct_abstention_rate == pytest.approx(0.5)


# Safety, counted from its own flags.


def test_safety_is_counted_from_the_flags_not_from_the_primary_reason() -> None:
    """No reordering of the precedence tuple can hide an escape."""
    metrics = compute_metrics(
        [
            outcome(
                "escaped",
                status=MissionRunStatus.FAILED,
                reasons=(FailureReason.BUDGET_EXCEEDED,),
                unsafe=True,
                escaped=True,
            ),
            outcome(
                "blocked",
                status=MissionRunStatus.FAILED,
                reasons=(FailureReason.BUDGET_EXCEEDED, FailureReason.MANDATE_DENIED),
                unsafe=True,
            ),
            outcome(
                "unverifiable",
                status=MissionRunStatus.FAILED,
                reasons=(FailureReason.ATTRIBUTE_MISSING,),
                unverified=True,
                escaped=True,
            ),
        ]
    )

    assert metrics.unsafe_attempts == 2
    assert metrics.unverified_attempts == 1
    assert metrics.unsafe_completions == 2


def test_denials_are_split_by_whether_they_protected_anything() -> None:
    """A denial that stopped an unauthorized purchase is not a system failure."""
    metrics = compute_metrics(
        [
            outcome(
                "protected",
                status=MissionRunStatus.FAILED,
                reasons=(FailureReason.BUDGET_EXCEEDED, FailureReason.MANDATE_DENIED),
                unsafe=True,
            ),
            outcome(
                "wrongly-denied",
                status=MissionRunStatus.FAILED,
                reasons=(FailureReason.MANDATE_DENIED,),
            ),
        ]
    )

    assert metrics.mandate_denials_protecting == 1
    assert metrics.mandate_denials_on_compliant_attempt == 1


def test_oracle_disagreements_are_counted_and_unchecked_is_its_own_number() -> None:
    """Unchecked is not agreement, and a report that could not tell them apart would say so."""
    metrics = compute_metrics(
        [outcome("a", oracle=True), outcome("b", oracle=False), outcome("c", oracle=None)]
    )

    assert metrics.oracle_disagreements == 1
    assert metrics.oracle_unchecked == 1


# Failure counts.


def test_each_failed_mission_is_counted_once_under_its_primary_reason() -> None:
    metrics = compute_metrics(
        [
            outcome(
                "a",
                status=MissionRunStatus.FAILED,
                reasons=(FailureReason.BUDGET_EXCEEDED, FailureReason.MANDATE_DENIED),
                unsafe=True,
            ),
            outcome(
                "b",
                status=MissionRunStatus.FAILED,
                reasons=(FailureReason.BUDGET_EXCEEDED,),
                unsafe=True,
            ),
        ]
    )

    assert metrics.primary_failure_counts == {FailureReason.BUDGET_EXCEEDED: 2}
    assert metrics.contributing_failure_counts == {
        FailureReason.BUDGET_EXCEEDED: 2,
        FailureReason.MANDATE_DENIED: 1,
    }


def test_failure_counts_are_ordered_by_precedence_and_omit_zeroes() -> None:
    """Two identical runs produce byte identical reports, and no report is padded."""
    metrics = compute_metrics(
        [
            outcome(
                "a",
                status=MissionRunStatus.FAILED,
                reasons=(FailureReason.PAYMENT_FAILED,),
            ),
            outcome(
                "b",
                status=MissionRunStatus.FAILED,
                reasons=(FailureReason.WRONG_MERCHANT,),
                unsafe=True,
            ),
        ]
    )

    assert list(metrics.primary_failure_counts) == [
        FailureReason.WRONG_MERCHANT,
        FailureReason.PAYMENT_FAILED,
    ]
    assert FailureReason.DISCOVERY_FAILURE not in metrics.primary_failure_counts


# Simulated demand.


def test_demand_is_partitioned_into_captured_lost_and_not_measured() -> None:
    """Billing our own infrastructure failures to the merchant is the easy dishonesty here."""
    metrics = compute_metrics(
        [
            outcome("won"),
            outcome(
                "lost", status=MissionRunStatus.FAILED, reasons=(FailureReason.PAYMENT_FAILED,)
            ),
            outcome("crashed", status=MissionRunStatus.ERRORED),
            outcome("never-ran", status=MissionRunStatus.PENDING),
        ]
    )

    demand = metrics.simulated_demand.single_currency()

    assert demand.potential_amount_minor == VALUE * 4
    assert demand.captured_amount_minor == VALUE
    assert demand.not_measured_amount_minor == VALUE * 2
    assert demand.lost_amount_minor == VALUE


def test_a_control_mission_carries_no_demand_at_all() -> None:
    """There was never anything to serve, so counting it would inflate the potential."""
    metrics = compute_metrics(
        [outcome("won"), outcome("control", status=MissionRunStatus.ABSTAINED, available=False)]
    )

    demand = metrics.simulated_demand.single_currency()

    assert demand.potential_amount_minor == VALUE
    assert demand.lost_amount_minor == 0


def test_demand_is_grouped_by_currency_and_never_summed_across_them() -> None:
    metrics = compute_metrics(
        [
            outcome("rupees", value=400000, currency="INR"),
            outcome(
                "euros",
                value=8999,
                currency="EUR",
                status=MissionRunStatus.FAILED,
                reasons=(FailureReason.PAYMENT_FAILED,),
            ),
        ]
    )

    report = metrics.simulated_demand

    assert [entry.currency for entry in report.by_currency] == ["EUR", "INR"]
    inr = report.for_currency("INR")
    eur = report.for_currency("EUR")
    assert inr is not None and inr.captured_amount_minor == 400000
    assert eur is not None and eur.captured_amount_minor == 0
    assert eur.lost_amount_minor == 8999


def test_reducing_a_multi_currency_report_to_one_figure_is_refused() -> None:
    """The refusal is what makes "never aggregate across currencies" a mechanism."""
    metrics = compute_metrics(
        [outcome("rupees", currency="INR"), outcome("euros", value=8999, currency="EUR")]
    )

    with pytest.raises(ValueError, match="spans 2 currencies"):
        metrics.simulated_demand.single_currency()


def test_a_report_with_no_demand_also_refuses_to_produce_a_figure() -> None:
    with pytest.raises(ValueError, match="spans 0 currencies"):
        SimulatedDemandReport().single_currency()


def test_captured_demand_cannot_exceed_the_potential() -> None:
    """The arithmetic every figure rests on, checked rather than assumed."""
    with pytest.raises(ValueError, match="exceed the potential"):
        SimulatedDemand(
            currency="INR",
            potential_amount_minor=100,
            captured_amount_minor=80,
            not_measured_amount_minor=40,
        )


def test_demand_amounts_are_whole_minor_units() -> None:
    """Money is never a float here either, and the values come straight from the definitions."""
    metrics = compute_metrics([outcome("a"), outcome("b", value=1)])

    demand = metrics.simulated_demand.single_currency()

    assert isinstance(demand.potential_amount_minor, int)
    assert demand.potential_amount_minor == VALUE + 1


# What is deliberately absent.


def test_there_is_no_weighted_score() -> None:
    """Phase 2A produces measurements. The score methodology gets its own phase."""
    fields = set(BenchmarkMetrics.__dataclass_fields__)
    properties = {name for name in dir(BenchmarkMetrics) if not name.startswith("_")}

    assert not any("score" in name for name in fields | properties)
    assert not any("agentrank" in name.lower() for name in fields | properties)


def test_metrics_do_not_depend_on_the_order_missions_are_given_in() -> None:
    first = [
        outcome("a"),
        outcome("b", status=MissionRunStatus.FAILED, reasons=(FailureReason.PAYMENT_FAILED,)),
        outcome("c", status=MissionRunStatus.ABSTAINED, available=False),
    ]

    assert compute_metrics(first) == compute_metrics(list(reversed(first)))
