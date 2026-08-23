"""Experiment comparison diagnosis against hand built experiment facts.

The clean parity shape mirrors the real post boundary N=2 result: both arms complete every
purchasable mission and abstain correctly, no transitions, no safety difference. The tests
assert that this is representable as an honest parity conclusion with its sample size
attached, that every historical confound AgentRank actually met produces its warning, and
that nothing here can manufacture a compiler claim from parity.
"""

import uuid

from agentrank_api.benchmark.definitions import ExpectedOutcome
from agentrank_api.benchmark.failures import FailureReason
from agentrank_api.benchmark.lifecycle import MissionRunStatus
from agentrank_api.benchmark.metrics import MissionOutcome, compute_metrics
from agentrank_api.diagnostics.codes import engine_identity
from agentrank_api.diagnostics.experiment import (
    CONCLUSION_INCOMPLETE,
    CONCLUSION_OUTCOME_DIFFERENCES,
    CONCLUSION_PARITY,
    TRANSITION_COMPILED_GAIN,
    TRANSITION_COMPILED_LOSS,
    ExperimentDiagnosis,
    ExperimentFacts,
    ExperimentSampleFacts,
    MissionOutcomeFacts,
    diagnose_experiment,
)

CURRENCY = "INR"
VALUE = 499900


def outcome(key: str, status: MissionRunStatus) -> MissionOutcome:
    return MissionOutcome(
        mission_key=key,
        expected_outcome=ExpectedOutcome.PURCHASE_AVAILABLE,
        simulated_value_amount_minor=VALUE if status is not MissionRunStatus.ABSTAINED else 0,
        currency=CURRENCY,
        status=status,
    )


def outcomes(*specs: tuple[str, MissionRunStatus]) -> tuple[MissionOutcome, ...]:
    return tuple(outcome(key, status) for key, status in specs)


def outcome_facts(
    *specs: tuple[str, MissionRunStatus],
) -> tuple[MissionOutcomeFacts, ...]:
    return tuple(
        MissionOutcomeFacts(
            mission_key=key,
            status=status.value,
            primary_failure_reason=None
            if status is MissionRunStatus.SUCCEEDED
            else FailureReason.DISCOVERY_FAILURE.value,
        )
        for key, status in specs
    )


def sample(
    arm: str,
    pair: int,
    *,
    completed: bool = True,
    statuses: tuple[tuple[str, MissionRunStatus], ...] = (
        ("buy-a-charger", MissionRunStatus.SUCCEEDED),
    ),
    provider_failures: int = 0,
    invocations: int = 40,
    tokens_reported: bool | None = True,
    implementation_version: int | None = 4,
    resolved: tuple[str, ...] = ("gemini-3.5-flash-lite",),
) -> ExperimentSampleFacts:
    metrics = compute_metrics(outcomes(*statuses)) if completed else None
    return ExperimentSampleFacts(
        sample_id=uuid.uuid4(),
        pair_ordinal=pair,
        arm=arm,
        run_id=uuid.uuid4() if completed else None,
        run_status="COMPLETED" if completed else None,
        metrics=metrics,
        mission_outcomes=outcome_facts(*statuses) if completed else (),
        provider_failure_missions=provider_failures,
        model_invocations=invocations,
        tool_calls=invocations * 2,
        token_usage_reported=tokens_reported,
        requested_model="gemini-3.5-flash-lite",
        resolved_models=resolved,
        agent_implementation_version=implementation_version,
    )


def facts(
    samples: list[ExperimentSampleFacts],
    *,
    designation: str = "EVALUATION",
    pair_order: str = "counterbalanced",
    pairs: int = 2,
) -> ExperimentFacts:
    return ExperimentFacts(
        experiment_id=uuid.uuid4(),
        benchmark_designation=designation,
        pair_order=pair_order,
        declared_sample_pairs=pairs,
        buyer_configuration_digest="sha256:" + "a" * 64,
        samples=tuple(samples),
    )


def saturated_statuses() -> tuple[tuple[str, MissionRunStatus], ...]:
    purchases = tuple((f"buy-{n}", MissionRunStatus.SUCCEEDED) for n in range(10))
    controls = tuple((f"skip-{n}", MissionRunStatus.ABSTAINED) for n in range(8))
    return purchases + controls


def failed_status() -> tuple[tuple[str, MissionRunStatus], ...]:
    return (("buy-a-charger", MissionRunStatus.FAILED),)


class TestCleanParity:
    def diagnosis(self) -> ExperimentDiagnosis:
        samples = [
            sample("RAW", 1, statuses=saturated_statuses()),
            sample("COMPILED", 1, statuses=saturated_statuses()),
            sample("COMPILED", 2, statuses=saturated_statuses()),
            sample("RAW", 2, statuses=saturated_statuses()),
        ]
        return diagnose_experiment(facts(samples), engine_identity=engine_identity())

    def test_clean_null_experiment_reads_as_honest_parity(self) -> None:
        diagnosis = self.diagnosis()
        assert diagnosis.conclusion.kind is CONCLUSION_PARITY
        statement = diagnosis.conclusion.statement.lower()
        assert "no measurable compiler benefit" in statement
        assert "2 completed paired sample(s)" in statement

    def test_sample_count_is_visible_and_no_significance_is_claimed(self) -> None:
        diagnosis = self.diagnosis()
        assert diagnosis.completed_sample_pairs == 2
        words = diagnosis.conclusion.statement.lower()
        assert "significant" not in words
        codes = {warning.code for warning in diagnosis.warnings}
        assert "SMALL_SAMPLE" not in codes

    def test_evaluation_designation_produces_no_development_warning(self) -> None:
        diagnosis = self.diagnosis()
        codes = {warning.code for warning in diagnosis.warnings}
        assert "DEVELOPMENT_BENCHMARK" not in codes
        assert diagnosis.benchmark_designation == "EVALUATION"

    def test_demand_delta_is_zero_per_currency(self) -> None:
        diagnosis = self.diagnosis()
        deltas = diagnosis.demand_delta_by_currency
        assert len(deltas) == 1
        assert deltas[0].currency == CURRENCY
        assert deltas[0].captured_amount_minor == 0

    def test_interaction_metrics_are_reported_per_arm(self) -> None:
        diagnosis = self.diagnosis()
        raw = diagnosis.arms["RAW"]
        compiled = diagnosis.arms["COMPILED"]
        assert raw.model_invocations == compiled.model_invocations
        assert raw.tool_calls == compiled.tool_calls == 160


class TestOutcomeDifferences:
    def test_transitions_are_classified_by_direction(self) -> None:
        good = saturated_statuses()
        failed = ("buy-0", MissionRunStatus.FAILED)
        improved = (failed, *good[1:])
        worsened = (failed, *good[1:])
        samples = [
            sample("RAW", 1, statuses=improved),
            sample("COMPILED", 1, statuses=good),
            sample("RAW", 2, statuses=good),
            sample("COMPILED", 2, statuses=worsened),
        ]
        diagnosis = diagnose_experiment(facts(samples), engine_identity=engine_identity())
        directions = sorted(t.direction for t in diagnosis.mission_transitions)
        assert directions == [TRANSITION_COMPILED_GAIN, TRANSITION_COMPILED_LOSS]
        assert diagnosis.conclusion.kind is CONCLUSION_OUTCOME_DIFFERENCES

    def test_identical_failure_on_both_arms_is_not_a_transition(self) -> None:
        samples = [
            sample("RAW", 1, statuses=failed_status()),
            sample("COMPILED", 1, statuses=failed_status()),
        ]
        diagnosis = diagnose_experiment(facts(samples, pairs=1), engine_identity=engine_identity())
        assert diagnosis.mission_transitions == ()
        assert diagnosis.conclusion.kind is CONCLUSION_PARITY


class TestWarnings:
    def test_development_benchmark_warns(self) -> None:
        samples = [
            sample("RAW", 1, statuses=saturated_statuses()),
            sample("COMPILED", 1, statuses=saturated_statuses()),
            sample("RAW", 2, statuses=saturated_statuses()),
            sample("COMPILED", 2, statuses=saturated_statuses()),
        ]
        diagnosis = diagnose_experiment(
            facts(samples, designation="DEVELOPMENT"), engine_identity=engine_identity()
        )
        codes = {warning.code for warning in diagnosis.warnings}
        assert "DEVELOPMENT_BENCHMARK" in codes

    def test_provider_failures_warn_and_are_counted(self) -> None:
        samples = [
            sample("RAW", 1, statuses=saturated_statuses(), provider_failures=3),
            sample("COMPILED", 1, statuses=saturated_statuses(), provider_failures=1),
        ]
        diagnosis = diagnose_experiment(facts(samples, pairs=1), engine_identity=engine_identity())
        codes = {warning.code for warning in diagnosis.warnings}
        assert "PROVIDER_FAILURES_PRESENT" in codes
        assert "NOT_CAUSALLY_INTERPRETABLE" in codes
        raw = diagnosis.arms["RAW"]
        assert raw.provider_failure_missions == 3
        assert diagnosis.arms["COMPILED"].provider_failure_missions == 1

    def test_pre_counterbalanced_order_warns(self) -> None:
        samples = [
            sample("RAW", 1, statuses=saturated_statuses()),
            sample("COMPILED", 1, statuses=saturated_statuses()),
        ]
        diagnosis = diagnose_experiment(
            facts(samples, pair_order="raw_then_compiled", pairs=1),
            engine_identity=engine_identity(),
        )
        codes = {warning.code for warning in diagnosis.warnings}
        assert "NOT_COUNTERBALANCED" in codes
        assert "NOT_CAUSALLY_INTERPRETABLE" in codes

    def test_pre_boundary_implementation_warns(self) -> None:
        samples = [
            sample("RAW", 1, statuses=saturated_statuses(), implementation_version=2),
            sample("COMPILED", 1, statuses=saturated_statuses(), implementation_version=2),
        ]
        diagnosis = diagnose_experiment(facts(samples, pairs=1), engine_identity=engine_identity())
        codes = {warning.code for warning in diagnosis.warnings}
        # Version 2 predates both the boundary and retry pacing.
        assert "PRE_DISCOVERY_BOUNDARY" in codes
        assert "PRE_THROTTLE_RETRY" in codes

    def test_throttle_retry_era_implementation_warns_narrowly(self) -> None:
        samples = [
            sample("RAW", 1, statuses=saturated_statuses(), implementation_version=3),
            sample("COMPILED", 1, statuses=saturated_statuses(), implementation_version=3),
        ]
        diagnosis = diagnose_experiment(facts(samples, pairs=1), engine_identity=engine_identity())
        codes = {warning.code for warning in diagnosis.warnings}
        # Version 3 retries throttles but still predates the discovery boundary.
        assert "PRE_DISCOVERY_BOUNDARY" in codes
        assert "PRE_THROTTLE_RETRY" not in codes

    def test_resolved_model_mismatch_between_arms_warns(self) -> None:
        samples = [
            sample("RAW", 1, statuses=saturated_statuses()),
            sample(
                "COMPILED",
                1,
                statuses=saturated_statuses(),
                resolved=("gemini-x", "gemini-y"),
            ),
        ]
        diagnosis = diagnose_experiment(facts(samples, pairs=1), engine_identity=engine_identity())
        codes = {warning.code for warning in diagnosis.warnings}
        assert "RESOLVED_MODEL_MISMATCH" in codes

    def test_incomplete_pair_warns_and_blocks_parity(self) -> None:
        samples = [
            sample("RAW", 1, statuses=saturated_statuses()),
            sample("COMPILED", 1, completed=False),
        ]
        diagnosis = diagnose_experiment(facts(samples, pairs=1), engine_identity=engine_identity())
        codes = {warning.code for warning in diagnosis.warnings}
        assert "INCOMPLETE_PAIRS" in codes
        assert diagnosis.conclusion.kind is CONCLUSION_INCOMPLETE

    def test_small_sample_warns_for_one_pair_only(self) -> None:
        one_pair = [
            sample("RAW", 1, statuses=saturated_statuses()),
            sample("COMPILED", 1, statuses=saturated_statuses()),
        ]
        small = diagnose_experiment(facts(one_pair, pairs=1), engine_identity=engine_identity())
        assert "SMALL_SAMPLE" in {w.code for w in small.warnings}

        two_pairs = [
            *one_pair,
            sample("COMPILED", 2, statuses=saturated_statuses()),
            sample("RAW", 2, statuses=saturated_statuses()),
        ]
        adequate = diagnose_experiment(facts(two_pairs), engine_identity=engine_identity())
        assert "SMALL_SAMPLE" not in {w.code for w in adequate.warnings}

    def test_unknown_token_usage_warns(self) -> None:
        samples = [
            sample("RAW", 1, statuses=saturated_statuses(), tokens_reported=False),
            sample("COMPILED", 1, statuses=saturated_statuses()),
        ]
        diagnosis = diagnose_experiment(facts(samples, pairs=1), engine_identity=engine_identity())
        assert "TOKEN_USAGE_UNAVAILABLE" in {w.code for w in diagnosis.warnings}


class TestDemandDeltas:
    def test_deltas_are_per_currency_and_never_summed(self) -> None:
        from agentrank_api.diagnostics.experiment import CurrencyDelta

        samples = [
            sample("RAW", 1, statuses=saturated_statuses()),
            sample("COMPILED", 1, statuses=saturated_statuses()),
        ]
        diagnosis = diagnose_experiment(facts(samples, pairs=1), engine_identity=engine_identity())
        deltas = diagnosis.demand_delta_by_currency
        currencies = [delta.currency for delta in deltas]
        assert len(currencies) == len(set(currencies))
        for delta in deltas:
            assert isinstance(delta, CurrencyDelta)
            assert delta.captured_amount_minor == 0


class TestIdentity:
    def test_diagnosis_carries_engine_identity(self) -> None:
        samples = [
            sample("RAW", 1, statuses=saturated_statuses()),
            sample("COMPILED", 1, statuses=saturated_statuses()),
        ]
        diagnosis = diagnose_experiment(facts(samples, pairs=1), engine_identity=engine_identity())
        assert diagnosis.engine_identity == engine_identity()

    def test_same_facts_produce_same_output(self) -> None:
        built = facts(
            [
                sample("RAW", 1, statuses=saturated_statuses()),
                sample("COMPILED", 1, statuses=saturated_statuses()),
            ],
            pairs=1,
        )
        first = diagnose_experiment(built, engine_identity=engine_identity())
        second = diagnose_experiment(built, engine_identity=engine_identity())
        assert first == second
