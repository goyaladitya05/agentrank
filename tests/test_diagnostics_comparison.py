"""Reading one run against an earlier one, and refusing to say more than the evidence supports.

The engine is pure, so every test here builds two sets of facts by hand and asserts what a
deterministic comparison must say about them. Two runs assembled by the read layer would make
these tests self referential: the comparison would be checked against the same pipeline that
produced its own inputs, and the read layer has its own database backed tests.

What is being defended is not arithmetic. It is that a difference between two runs separated in
time is never presented as an effect, that methodology differences are named rather than
absorbed, that currencies stay apart, and that unknown provider usage stays unknown.
"""

import uuid
from datetime import UTC, datetime, timedelta

from agentrank_api.benchmark.metrics import (
    BenchmarkMetrics,
    SimulatedDemand,
    SimulatedDemandReport,
)
from agentrank_api.diagnostics.comparison import (
    TRANSITION_IMPROVED,
    TRANSITION_REGRESSED,
    MissionOutcomeFact,
    RunComparison,
    RunFacts,
    compare_runs,
)

ENGINE = "sha256:" + "0" * 64
START = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)


def demand(
    currency: str = "INR", *, potential: int = 500000, captured: int = 500000
) -> SimulatedDemand:
    return SimulatedDemand(
        currency=currency,
        potential_amount_minor=potential,
        captured_amount_minor=captured,
        not_measured_amount_minor=0,
    )


def metrics(
    *,
    succeeded: int = 2,
    purchase_missions: int = 2,
    unsafe_completions: int = 0,
    currencies: tuple[SimulatedDemand, ...] = (),
) -> BenchmarkMetrics:
    return BenchmarkMetrics(
        missions_total=3,
        missions_succeeded=succeeded,
        missions_abstained=1,
        purchase_missions=purchase_missions,
        control_missions=1,
        correct_abstentions=1,
        unsafe_completions=unsafe_completions,
        simulated_demand=SimulatedDemandReport(currencies or (demand(),)),
    )


def facts(**overrides: object) -> RunFacts:
    """One completed run of a fixed suite by a fixed buyer, the common shape."""
    fields: dict[str, object] = {
        "run_id": uuid.uuid7(),
        "status": "COMPLETED",
        "suite_label": "voltedge-core@2",
        "suite_definition_hash": "sha256:" + "a" * 64,
        "environment_label": "voltedge-catalog@1",
        "catalog_hash": "sha256:" + "b" * 64,
        "evaluator_version": "sha256:" + "c" * 64,
        "executor_label": "llm-openai-v1",
        "executor_revision": "sha256:" + "d" * 64,
        "buyer_configuration_digest": "sha256:" + "e" * 64,
        "representation_id": None,
        "resolved_models": ("gpt-5.6-terra",),
        "metrics": metrics(),
        "missions_with_provider_errors": 0,
        "terminated_provider_outages": 0,
        "model_invocations": 6,
        "tool_calls": 9,
        "token_usage_reported": True,
        "outcomes": (
            MissionOutcomeFact("buy-one", "SUCCEEDED", None),
            MissionOutcomeFact("buy-two", "SUCCEEDED", None),
            MissionOutcomeFact("skip-one", "ABSTAINED", None),
        ),
        "started_at": START,
        "completed_at": START + timedelta(minutes=4),
    }
    fields.update(overrides)
    return RunFacts(**fields)  # type: ignore[arg-type]


def codes(comparison: RunComparison) -> set[str]:
    return {warning.code for warning in comparison.warnings}


class TestParity:
    def test_identical_runs_read_as_parity_and_never_as_proof(self) -> None:
        comparison = compare_runs(facts(), facts(), engine_identity=ENGINE)

        assert comparison.comparable is True
        assert comparison.conclusion.kind == "PARITY"
        assert comparison.transitions == ()
        assert "not evidence that the representation cannot help" in comparison.conclusion.statement

    def test_every_comparison_says_it_is_not_a_controlled_experiment(self) -> None:
        comparison = compare_runs(facts(), facts(), engine_identity=ENGINE)

        assert "NOT_A_CONTROLLED_EXPERIMENT" in codes(comparison)
        assert "SMALL_SAMPLE" in codes(comparison)


class TestDifferences:
    def test_a_mission_that_newly_completes_is_an_improvement(self) -> None:
        before = facts(
            outcomes=(
                MissionOutcomeFact("buy-one", "FAILED", "ATTRIBUTE_MISSING"),
                MissionOutcomeFact("buy-two", "SUCCEEDED", None),
                MissionOutcomeFact("skip-one", "ABSTAINED", None),
            ),
            metrics=metrics(succeeded=1, currencies=(demand(captured=250000),)),
        )
        comparison = compare_runs(before, facts(), engine_identity=ENGINE)

        assert comparison.conclusion.kind == "OUTCOME_DIFFERENCES"
        transition = next(t for t in comparison.transitions if t.mission_key == "buy-one")
        assert transition.direction == TRANSITION_IMPROVED
        assert transition.before_primary_failure_reason == "ATTRIBUTE_MISSING"
        completion = next(rate for rate in comparison.rates if rate.key == "task_completion_rate")
        assert completion.before == 0.5
        assert completion.after == 1.0
        assert completion.delta == 0.5

    def test_a_mission_that_stops_completing_is_a_regression(self) -> None:
        after = facts(
            outcomes=(
                MissionOutcomeFact("buy-one", "FAILED", "STOCK_UNAVAILABLE"),
                MissionOutcomeFact("buy-two", "SUCCEEDED", None),
                MissionOutcomeFact("skip-one", "ABSTAINED", None),
            ),
            metrics=metrics(succeeded=1),
        )
        comparison = compare_runs(facts(), after, engine_identity=ENGINE)

        transition = next(t for t in comparison.transitions if t.mission_key == "buy-one")
        assert transition.direction == TRANSITION_REGRESSED
        assert "no longer completed" in comparison.conclusion.statement

    def test_any_published_count_that_moves_refuses_parity(self) -> None:
        """The conclusion is judged against everything the panel prints.

        A ground truth disagreement moves a published count without moving a single mission's
        terminal position, so a parity conclusion here would sit directly above a table headed
        "counts that moved" and contradict it.
        """
        moved = metrics()
        after = facts(
            metrics=BenchmarkMetrics(
                missions_total=moved.missions_total,
                missions_succeeded=moved.missions_succeeded,
                missions_abstained=moved.missions_abstained,
                purchase_missions=moved.purchase_missions,
                control_missions=moved.control_missions,
                correct_abstentions=moved.correct_abstentions,
                oracle_disagreements=1,
                simulated_demand=moved.simulated_demand,
            )
        )
        comparison = compare_runs(facts(), after, engine_identity=ENGINE)

        assert comparison.conclusion.kind == "OUTCOME_DIFFERENCES"
        assert "oracle_disagreements 0 to 1" in comparison.conclusion.statement
        assert comparison.transitions == ()

    def test_a_safety_change_alone_is_reported_as_a_difference(self) -> None:
        after = facts(metrics=metrics(unsafe_completions=1))
        comparison = compare_runs(facts(), after, engine_identity=ENGINE)

        assert comparison.conclusion.kind == "OUTCOME_DIFFERENCES"
        assert "safety counts changed" in comparison.conclusion.statement


class TestMethodology:
    def test_a_different_suite_makes_the_comparison_refuse_rather_than_qualify(self) -> None:
        comparison = compare_runs(
            facts(suite_label="voltedge-core@1", suite_definition_hash="sha256:" + "f" * 64),
            facts(),
            engine_identity=ENGINE,
        )

        assert comparison.comparable is False
        assert comparison.conclusion.kind == "INCOMPLETE"
        assert "SUITE_DIFFERS" in codes(comparison)

    def test_an_unfinished_run_is_not_comparable(self) -> None:
        comparison = compare_runs(facts(), facts(status="ABORTED"), engine_identity=ENGINE)

        assert comparison.comparable is False
        assert "RUN_NOT_COMPLETED" in codes(comparison)

    def test_a_different_buyer_makes_the_two_runs_incomparable(self) -> None:
        """One of the five pins this benchmark has always required to match.

        A deterministic buyer measured against a model buyer is the realistic case here, and
        their numbers are not two readings of the same thing.
        """
        comparison = compare_runs(
            facts(executor_label="reference-isolated-v1"), facts(), engine_identity=ENGINE
        )

        assert "EXECUTOR_DIFFERS" in codes(comparison)
        assert comparison.comparable is False
        assert comparison.conclusion.kind == "INCOMPLETE"

    def test_the_same_buyer_built_from_different_code_is_still_flagged(self) -> None:
        comparison = compare_runs(
            facts(executor_revision="sha256:" + "9" * 64), facts(), engine_identity=ENGINE
        )

        assert "EXECUTOR_REVISION_DIFFERS" in codes(comparison)
        assert "EXECUTOR_DIFFERS" not in codes(comparison)

    def test_a_moved_catalog_pin_claims_no_share_of_the_difference(self) -> None:
        """A moved shelf is fatal, and the sentence attributes nothing to what was published.

        Saying a difference is "jointly caused by the representation" would presuppose the
        representation caused part of it, which is exactly the reading every comparison here
        refuses.
        """
        comparison = compare_runs(
            facts(catalog_hash="sha256:" + "1" * 64), facts(), engine_identity=ENGINE
        )

        assert "CATALOG_PIN_DIFFERS" in codes(comparison)
        assert comparison.comparable is False
        message = next(w.message for w in comparison.warnings if w.code == "CATALOG_PIN_DIFFERS")
        assert "caused" not in message
        assert "different merchant data" in message

    def test_only_one_run_seeing_a_representation_is_a_methodology_difference(self) -> None:
        comparison = compare_runs(
            facts(), facts(representation_id=uuid.uuid7()), engine_identity=ENGINE
        )

        assert "REPRESENTATION_DELIVERY_DIFFERS" in codes(comparison)

    def test_the_same_representation_on_both_sides_is_said_out_loud(self) -> None:
        """Re-running an unchanged artifact is variation, and the reader is told so."""
        representation = uuid.uuid7()
        comparison = compare_runs(
            facts(representation_id=representation),
            facts(representation_id=representation),
            engine_identity=ENGINE,
        )

        assert "REPRESENTATION_UNCHANGED" in codes(comparison)
        assert comparison.comparable is True

    def test_a_different_resolved_model_is_flagged(self) -> None:
        comparison = compare_runs(
            facts(resolved_models=("gpt-5.6-terra-2026-08",)), facts(), engine_identity=ENGINE
        )

        assert "RESOLVED_MODEL_MISMATCH" in codes(comparison)

    def test_provider_outages_are_reported_beside_the_metrics(self) -> None:
        comparison = compare_runs(
            facts(), facts(terminated_provider_outages=2), engine_identity=ENGINE
        )

        assert "PROVIDER_FAILURES_PRESENT" in codes(comparison)
        failures = next(c for c in comparison.counts if c.key == "provider_failure_missions")
        assert failures.before == 0


class TestHonestNumbers:
    def test_currencies_are_never_summed(self) -> None:
        two = metrics(currencies=(demand("EUR", potential=1000, captured=0), demand("INR")))
        comparison = compare_runs(facts(metrics=two), facts(metrics=two), engine_identity=ENGINE)

        currencies = {change.currency for change in comparison.simulated_demand}
        assert currencies == {"EUR", "INR"}
        buckets = {change.bucket for change in comparison.simulated_demand}
        assert buckets == {"POTENTIAL", "CAPTURED", "LOST", "NOT_MEASURED"}
        # Every row names exactly one currency, so no field can hold a mixed total.
        assert all(isinstance(change.currency, str) for change in comparison.simulated_demand)

    def test_unknown_token_usage_stays_unknown(self) -> None:
        comparison = compare_runs(
            facts(), facts(token_usage_reported=False), engine_identity=ENGINE
        )

        assert comparison.interactions.token_usage_complete is False
        assert "TOKEN_USAGE_UNAVAILABLE" in codes(comparison)
        # Round trips and tool calls are real counts and are still compared.
        assert comparison.interactions.model_invocations is not None

    def test_no_provider_invocation_is_not_the_same_as_no_usage_reported(self) -> None:
        """Three states, kept apart.

        "Nobody asked a model" is not "a model answered and said nothing about tokens", and
        collapsing them would let the console assert a report that was never made.
        """
        comparison = compare_runs(
            facts(model_invocations=None, tool_calls=None, token_usage_reported=None),
            facts(model_invocations=None, tool_calls=None, token_usage_reported=None),
            engine_identity=ENGINE,
        )

        assert comparison.interactions.token_usage_complete is None
        assert "TOKEN_USAGE_UNAVAILABLE" not in codes(comparison)

    def test_a_trace_on_one_side_only_is_reported_as_such(self) -> None:
        comparison = compare_runs(
            facts(model_invocations=None, tool_calls=None, token_usage_reported=None),
            facts(),
            engine_identity=ENGINE,
        )

        assert comparison.interactions.baseline_traced is False
        assert comparison.interactions.candidate_traced is True
        # No count is published against a side that recorded nothing, because zero is not what
        # "no model was asked" means.
        assert comparison.interactions.model_invocations is None
        assert comparison.interactions.tool_calls is None

    def test_an_empty_denominator_stays_null_rather_than_zero(self) -> None:
        empty = metrics(succeeded=0, purchase_missions=0)
        comparison = compare_runs(facts(metrics=empty), facts(), engine_identity=ENGINE)

        completion = next(rate for rate in comparison.rates if rate.key == "task_completion_rate")
        assert completion.before is None
        assert completion.delta is None

    def test_no_weighted_score_is_produced_anywhere(self) -> None:
        comparison = compare_runs(facts(), facts(), engine_identity=ENGINE)

        fields = set(comparison.__slots__)
        assert not any("score" in name for name in fields)
        assert not any("score" in change.key for change in comparison.counts)
        assert not any("score" in rate.key for rate in comparison.rates)
