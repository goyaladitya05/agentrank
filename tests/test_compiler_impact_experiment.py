"""Controlled compiler impact experiment protections."""

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from benchmark_support import VOLTEDGE, mission, suite
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.benchmark.environment import BenchmarkEnvironmentService
from agentrank_api.benchmark.execution import ExecutorIdentity
from agentrank_api.benchmark.experiment import (
    CompilerImpactExperiment,
    CompilerImpactExperimentService,
    CompilerImpactSample,
    RepresentationKind,
)
from agentrank_api.benchmark.lifecycle import BenchmarkRunStatus
from agentrank_api.benchmark.llm import GEMINI_PROVIDER, AgentConfiguration, mission_input
from agentrank_api.benchmark.models import BenchmarkEnvironment, BenchmarkRun, BenchmarkSuite
from agentrank_api.benchmark.repository import BenchmarkRunRepository, BenchmarkSuiteRepository
from agentrank_api.benchmark.wire import LLM_STRATEGY, MissionRequest
from agentrank_api.cli.benchmark import (
    _comparison_aggregates,
    _comparison_delta,
    _provider_usage_summary,
)
from agentrank_api.commerce.models import Merchant
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.compiler.service import MerchantCompilerService
from agentrank_api.representation.fixtures import read_ir, read_source
from agentrank_api.representation.models import CommerceRepresentation, MerchantSourceSnapshot
from agentrank_api.representation.service import MerchantRepresentationService

pytestmark = pytest.mark.anyio

SOURCE_PATH = Path("benchmarks/voltedge/source.json")
IR_PATH = Path("benchmarks/voltedge/commerce_ir.json")


async def prepared(
    session: AsyncSession,
) -> tuple[
    Merchant,
    MerchantSourceSnapshot,
    CommerceRepresentation,
    BenchmarkSuite,
    BenchmarkEnvironment,
]:
    source_definition = read_source(SOURCE_PATH)
    merchant = await MerchantRepository(session).create(slug="voltedge", name="VoltEdge")
    await session.commit()
    representations = MerchantRepresentationService(session)
    source = await representations.publish_source(source_definition)
    compiler = MerchantCompilerService(session)
    run = await compiler.run(merchant.id, source.id)
    compiled = await compiler.publish(merchant.id, run.id)
    stored_suite = await BenchmarkSuiteRepository(session).create(suite(merchant_slug="voltedge"))
    await session.commit()
    environment = await BenchmarkEnvironmentService(session).register(VOLTEDGE.fixture)
    return merchant, source, compiled, stored_suite, environment


async def test_experiment_freezes_paired_source_compiler_and_buyer_identities(
    session: AsyncSession,
) -> None:
    merchant, source, compiled, stored_suite, environment = await prepared(session)
    config = AgentConfiguration(provider="openai-responses", requested_model="test-model")
    service = CompilerImpactExperimentService(session)
    experiment = await service.create(
        merchant_id=merchant.id,
        suite_id=stored_suite.id,
        environment=environment,
        source_snapshot_id=source.id,
        compiled_representation_id=compiled.id,
        buyer_configuration=config.payload(),
        buyer_configuration_digest=config.configuration_digest,
        sample_count=2,
    )

    samples = await service.samples(merchant.id, experiment.id)

    # Counterbalanced by construction: odd pairs open raw, even pairs open compiled. The order
    # is frozen before any provider call and cannot be reordered after results are observed.
    assert [(sample.pair_ordinal, sample.representation_kind) for sample in samples] == [
        (1, RepresentationKind.RAW),
        (1, RepresentationKind.COMPILED),
        (2, RepresentationKind.COMPILED),
        (2, RepresentationKind.RAW),
    ]
    assert [sample.execution_ordinal for sample in samples] == [1, 2, 3, 4]
    assert all(
        sample.source_snapshot_id == source.id
        for sample in samples
        if sample.representation_kind is RepresentationKind.RAW
    )
    assert all(
        sample.representation_id == compiled.id
        for sample in samples
        if sample.representation_kind is RepresentationKind.COMPILED
    )
    assert experiment.buyer_configuration_digest == config.configuration_digest
    assert experiment.source_snapshot_id == compiled.source_snapshot_id
    assert compiled.compiler_run_id is not None
    assert experiment.methodology["pair_order"] == "counterbalanced"


async def test_treatment_order_follows_the_frozen_counterbalanced_plan(
    session: AsyncSession,
) -> None:
    merchant, source, compiled, stored_suite, environment = await prepared(session)
    config = AgentConfiguration(provider="openai-responses", requested_model="test-model")
    service = CompilerImpactExperimentService(session)
    experiment = await service.create(
        merchant_id=merchant.id,
        suite_id=stored_suite.id,
        environment=environment,
        source_snapshot_id=source.id,
        compiled_representation_id=compiled.id,
        buyer_configuration=config.payload(),
        buyer_configuration_digest=config.configuration_digest,
        sample_count=2,
    )
    observed: list[RepresentationKind] = []
    for _ in range(4):
        treatment = await service.next_treatment(merchant.id, experiment.id)
        observed.append(treatment.sample.representation_kind)
        benchmark_run = await BenchmarkRunRepository(session).create(
            merchant=merchant,
            suite=stored_suite,
            environment=environment,
            executor=ExecutorIdentity(
                kind="llm-openai", version=1, revision=config.configuration_digest
            ),
            agent_configuration=config.payload(),
            catalog_hash="sha256:" + "0" * 64,
            evaluator_version="sha256:" + "0" * 64,
            representation=treatment.representation,
        )
        benchmark_run.status = BenchmarkRunStatus.RUNNING
        benchmark_run.started_at = datetime.now(UTC)
        await session.commit()
        await service.bind_run(treatment, benchmark_run.id)
        # Close each bound sample so the one-run-per-world claim never blocks the next.
        bound = await session.get(BenchmarkRun, benchmark_run.id)
        assert bound is not None
        bound.status = BenchmarkRunStatus.COMPLETED
        bound.completed_at = datetime.now(UTC)
        await session.commit()

    assert observed == [
        RepresentationKind.RAW,
        RepresentationKind.COMPILED,
        RepresentationKind.COMPILED,
        RepresentationKind.RAW,
    ]


def test_historical_raw_first_experiments_still_validate_their_own_plan() -> None:
    config = AgentConfiguration(provider="openai-responses", requested_model="test-model").payload()
    legacy = CompilerImpactExperiment(
        id=uuid.uuid7(),
        merchant_id=uuid.uuid7(),
        suite_id=uuid.uuid7(),
        environment_id=uuid.uuid7(),
        source_snapshot_id=uuid.uuid7(),
        compiled_representation_id=uuid.uuid7(),
        buyer_configuration_digest="sha256:" + "0" * 64,
        buyer_configuration=config,
        methodology={},
        sample_count=2,
    )
    even_pair_compiled_second = CompilerImpactSample(
        experiment_id=legacy.id,
        merchant_id=legacy.merchant_id,
        pair_ordinal=2,
        execution_ordinal=4,
        representation_kind=RepresentationKind.COMPILED,
        representation_id=legacy.compiled_representation_id,
    )
    CompilerImpactExperimentService._validate_sample_identity(legacy, even_pair_compiled_second)

    reordered = CompilerImpactSample(
        experiment_id=legacy.id,
        merchant_id=legacy.merchant_id,
        pair_ordinal=2,
        execution_ordinal=3,
        representation_kind=RepresentationKind.COMPILED,
        representation_id=legacy.compiled_representation_id,
    )
    with pytest.raises(ValueError, match="order does not match"):
        CompilerImpactExperimentService._validate_sample_identity(legacy, reordered)


def test_counterbalanced_validation_accepts_only_its_own_declared_order() -> None:
    config = AgentConfiguration(provider=GEMINI_PROVIDER, requested_model="test-model").payload()
    counterbalanced = CompilerImpactExperiment(
        id=uuid.uuid7(),
        merchant_id=uuid.uuid7(),
        suite_id=uuid.uuid7(),
        environment_id=uuid.uuid7(),
        source_snapshot_id=uuid.uuid7(),
        compiled_representation_id=uuid.uuid7(),
        buyer_configuration_digest="sha256:" + "0" * 64,
        buyer_configuration=config,
        methodology={"pair_order": "counterbalanced"},
        sample_count=3,
    )
    even_pair_opens_compiled = CompilerImpactSample(
        experiment_id=counterbalanced.id,
        merchant_id=counterbalanced.merchant_id,
        pair_ordinal=2,
        execution_ordinal=3,
        representation_kind=RepresentationKind.COMPILED,
        representation_id=counterbalanced.compiled_representation_id,
    )
    CompilerImpactExperimentService._validate_sample_identity(
        counterbalanced, even_pair_opens_compiled
    )

    legacy_position = CompilerImpactSample(
        experiment_id=counterbalanced.id,
        merchant_id=counterbalanced.merchant_id,
        pair_ordinal=2,
        execution_ordinal=4,
        representation_kind=RepresentationKind.COMPILED,
        representation_id=counterbalanced.compiled_representation_id,
    )
    with pytest.raises(ValueError, match="order does not match"):
        CompilerImpactExperimentService._validate_sample_identity(counterbalanced, legacy_position)


async def test_evaluation_designation_is_frozen_separately_from_development(
    session: AsyncSession,
) -> None:
    merchant, source, compiled, stored_suite, environment = await prepared(session)
    config = AgentConfiguration(provider="openai-responses", requested_model="test-model")
    experiment = await CompilerImpactExperimentService(session).create(
        merchant_id=merchant.id,
        suite_id=stored_suite.id,
        environment=environment,
        source_snapshot_id=source.id,
        compiled_representation_id=compiled.id,
        buyer_configuration=config.payload(),
        buyer_configuration_digest=config.configuration_digest,
        sample_count=1,
        development_benchmark=False,
    )

    assert experiment.methodology["benchmark_designation"] == "EVALUATION"


async def test_manual_ir_cannot_become_the_compiled_treatment(session: AsyncSession) -> None:
    merchant, source, _compiled, stored_suite, environment = await prepared(session)
    manual = await MerchantRepresentationService(session).publish_ir(read_ir(IR_PATH))
    config = AgentConfiguration(provider="openai-responses", requested_model="test-model")

    with pytest.raises(ValueError, match="compiler-produced"):
        await CompilerImpactExperimentService(session).create(
            merchant_id=merchant.id,
            suite_id=stored_suite.id,
            environment=environment,
            source_snapshot_id=source.id,
            compiled_representation_id=manual.id,
            buyer_configuration=config.payload(),
            buyer_configuration_digest=config.configuration_digest,
            sample_count=1,
        )


async def test_projections_keep_raw_free_of_compiler_facts_and_hide_compiler_metadata(
    session: AsyncSession,
) -> None:
    merchant, source, compiled, stored_suite, environment = await prepared(session)
    config = AgentConfiguration(provider="openai-responses", requested_model="test-model")
    experiment = await CompilerImpactExperimentService(session).create(
        merchant_id=merchant.id,
        suite_id=stored_suite.id,
        environment=environment,
        source_snapshot_id=source.id,
        compiled_representation_id=compiled.id,
        buyer_configuration=config.payload(),
        buyer_configuration_digest=config.configuration_digest,
        sample_count=1,
    )
    service = CompilerImpactExperimentService(session)
    raw = await service.next_treatment(merchant.id, experiment.id)

    assert raw.sample.representation_kind is RepresentationKind.RAW
    assert raw.projection["policy_text"] == source.payload["policy_text"]
    assert "merchant_metadata" not in str(raw.projection)
    assert "wattage" not in str(raw.projection)

    benchmark_run = await BenchmarkRunRepository(session).create(
        merchant=merchant,
        suite=stored_suite,
        environment=environment,
        executor=ExecutorIdentity(
            kind="llm-openai", version=1, revision=config.configuration_digest
        ),
        agent_configuration=config.payload(),
        catalog_hash="sha256:" + "0" * 64,
        evaluator_version="sha256:" + "0" * 64,
    )
    benchmark_run.status = BenchmarkRunStatus.RUNNING
    benchmark_run.started_at = datetime.now(UTC)
    await session.commit()
    await service.bind_run(raw, benchmark_run.id)
    treatment = await service.next_treatment(merchant.id, experiment.id)

    assert treatment.sample.representation_kind is RepresentationKind.COMPILED
    serialized = str(treatment.projection)
    assert "wattage" in serialized
    assert "provenance" not in serialized
    assert "review_state" not in serialized
    assert "confidence" not in serialized
    assert treatment.representation is not None
    assert treatment.representation.id == compiled.id


async def test_experiment_and_sample_identity_cannot_be_rewritten(session: AsyncSession) -> None:
    merchant, source, compiled, stored_suite, environment = await prepared(session)
    config = AgentConfiguration(provider="openai-responses", requested_model="test-model")
    service = CompilerImpactExperimentService(session)
    experiment = await service.create(
        merchant_id=merchant.id,
        suite_id=stored_suite.id,
        environment=environment,
        source_snapshot_id=source.id,
        compiled_representation_id=compiled.id,
        buyer_configuration=config.payload(),
        buyer_configuration_digest=config.configuration_digest,
        sample_count=1,
    )
    sample = (await service.samples(merchant.id, experiment.id))[0]
    sample_id = sample.id

    with pytest.raises(DBAPIError, match="immutable"):
        await session.execute(
            text("UPDATE compiler_impact_experiment SET sample_count = 3 WHERE id = :id"),
            {"id": experiment.id},
        )
    await session.rollback()
    with pytest.raises(DBAPIError, match="immutable"):
        await session.execute(
            text("UPDATE compiler_impact_sample SET pair_ordinal = 9 WHERE id = :id"),
            {"id": sample_id},
        )
    await session.rollback()


async def test_the_database_enforces_each_declared_pair_order(session: AsyncSession) -> None:
    merchant, source, compiled, stored_suite, environment = await prepared(session)
    config = AgentConfiguration(provider="openai-responses", requested_model="test-model")
    await CompilerImpactExperimentService(session).create(
        merchant_id=merchant.id,
        suite_id=stored_suite.id,
        environment=environment,
        source_snapshot_id=source.id,
        compiled_representation_id=compiled.id,
        buyer_configuration=config.payload(),
        buyer_configuration_digest=config.configuration_digest,
        sample_count=1,
    )
    # A raw SQL experiment keeps the insert guard independent of this application's own
    # validation, which is what makes the check a database invariant rather than a convention.
    experiment_id = uuid.uuid7()
    await session.execute(
        text(
            "INSERT INTO compiler_impact_experiment"
            " (id, merchant_id, suite_id, environment_id, source_snapshot_id,"
            "  compiled_representation_id, buyer_configuration_digest, buyer_configuration,"
            "  methodology, sample_count)"
            " VALUES (:id, :merchant, :suite, :environment, :source, :representation,"
            '  :digest, :configuration, \'{"pair_order": "counterbalanced"}\'::jsonb, 2)'
        ),
        {
            "id": experiment_id,
            "merchant": merchant.id,
            "suite": stored_suite.id,
            "environment": environment.id,
            "source": source.id,
            "representation": compiled.id,
            "digest": "sha256:" + "0" * 64,
            "configuration": json.dumps(config.payload()),
        },
    )
    # The even pair opens compiled at the pair's first slot.
    await session.execute(
        text(
            "INSERT INTO compiler_impact_sample"
            " (id, experiment_id, merchant_id, pair_ordinal, execution_ordinal,"
            "  representation_kind, representation_id)"
            " VALUES (:id, :experiment, :merchant, 2, 3, 'COMPILED', :representation)"
        ),
        {
            "id": uuid.uuid7(),
            "experiment": experiment_id,
            "merchant": merchant.id,
            "representation": compiled.id,
        },
    )
    await session.commit()
    # The compiled arm cannot take the even pair's closing slot under the counterbalanced plan.
    with pytest.raises(DBAPIError, match="outside its experiment plan"):
        await session.execute(
            text(
                "INSERT INTO compiler_impact_sample"
                " (id, experiment_id, merchant_id, pair_ordinal, execution_ordinal,"
                "  representation_kind, representation_id)"
                " VALUES (:id, :experiment, :merchant, 2, 4, 'COMPILED', :representation)"
            ),
            {
                "id": uuid.uuid7(),
                "experiment": experiment_id,
                "merchant": merchant.id,
                "representation": compiled.id,
            },
        )
    await session.rollback()


async def test_the_database_still_enforces_the_legacy_raw_first_plan(
    session: AsyncSession,
) -> None:
    """Experiments that declare no pair order keep their historical raw first guard."""
    merchant, source, compiled, stored_suite, environment = await prepared(session)
    config = AgentConfiguration(provider="openai-responses", requested_model="test-model")
    await CompilerImpactExperimentService(session).create(
        merchant_id=merchant.id,
        suite_id=stored_suite.id,
        environment=environment,
        source_snapshot_id=source.id,
        compiled_representation_id=compiled.id,
        buyer_configuration=config.payload(),
        buyer_configuration_digest=config.configuration_digest,
        sample_count=1,
    )
    experiment_id = uuid.uuid7()
    await session.execute(
        text(
            "INSERT INTO compiler_impact_experiment"
            " (id, merchant_id, suite_id, environment_id, source_snapshot_id,"
            "  compiled_representation_id, buyer_configuration_digest, buyer_configuration,"
            "  methodology, sample_count)"
            " VALUES (:id, :merchant, :suite, :environment, :source, :representation,"
            "  :digest, :configuration, '{}'::jsonb, 2)"
        ),
        {
            "id": experiment_id,
            "merchant": merchant.id,
            "suite": stored_suite.id,
            "environment": environment.id,
            "source": source.id,
            "representation": compiled.id,
            "digest": "sha256:" + "0" * 64,
            "configuration": json.dumps(config.payload()),
        },
    )
    # The even pair still opens raw, exactly as every pre counterbalancing experiment ran.
    await session.execute(
        text(
            "INSERT INTO compiler_impact_sample"
            " (id, experiment_id, merchant_id, pair_ordinal, execution_ordinal,"
            "  representation_kind, source_snapshot_id)"
            " VALUES (:id, :experiment, :merchant, 2, 3, 'RAW', :source)"
        ),
        {
            "id": uuid.uuid7(),
            "experiment": experiment_id,
            "merchant": merchant.id,
            "source": source.id,
        },
    )
    await session.commit()
    # The compiled arm cannot take the even pair's opening slot in a legacy experiment.
    with pytest.raises(DBAPIError, match="outside its experiment plan"):
        await session.execute(
            text(
                "INSERT INTO compiler_impact_sample"
                " (id, experiment_id, merchant_id, pair_ordinal, execution_ordinal,"
                "  representation_kind, representation_id)"
                " VALUES (:id, :experiment, :merchant, 2, 3, 'COMPILED', :representation)"
            ),
            {
                "id": uuid.uuid7(),
                "experiment": experiment_id,
                "merchant": merchant.id,
                "representation": compiled.id,
            },
        )
    await session.rollback()


def test_merchant_information_reaches_the_model_without_an_oracle() -> None:
    input_item = mission_input(
        suite().missions[0].brief,
        {"products": [{"title": "Ordinary merchant information"}]},
    )
    text_payload = input_item["content"][0]["text"]

    assert "merchant_information" in text_payload
    assert "expected_outcome" not in text_payload
    assert "simulated_value" not in text_payload

    with pytest.raises(ValueError, match="oracle"):
        MissionRequest(
            brief=suite().missions[0].brief,
            merchant_id=uuid.uuid7(),
            base_url="http://127.0.0.1:1",
            token="token",
            strategy=LLM_STRATEGY,
            mandate_id=uuid.uuid7(),
            agent_configuration=AgentConfiguration(
                provider="openai-responses", requested_model="test-model"
            ).payload(),
            merchant_information={"products": [], "expected_outcome": "PURCHASE_AVAILABLE"},
        )


def test_simulated_demand_comparison_aggregates_and_deltas_stay_per_currency() -> None:
    base_metrics = {
        "missions_total": 8,
        "missions_succeeded": 0,
        "missions_failed": 0,
        "missions_abstained": 0,
        "missions_errored": 0,
        "missions_unfinished": 0,
        "correct_abstentions": 0,
        "incorrect_abstentions": 0,
        "unsafe_attempts": 0,
        "unverified_attempts": 0,
        "unsafe_completions": 0,
        "oracle_disagreements": 0,
        "primary_failure_counts": {},
    }
    reports = [
        {
            "representation_kind": "RAW",
            "run": {
                "status": "COMPLETED",
                "metrics": base_metrics
                | {"task_completion_rate": 0.5, "missions_succeeded": 4, "unsafe_attempts": 1},
                "simulated_demand": [
                    {
                        "currency": "INR",
                        "potential_amount_minor": 800,
                        "captured_amount_minor": 400,
                        "lost_amount_minor": 400,
                        "not_measured_amount_minor": 0,
                    }
                ],
            },
        },
        {
            "representation_kind": "COMPILED",
            "run": {
                "status": "COMPLETED",
                "metrics": base_metrics | {"task_completion_rate": 0.75, "missions_succeeded": 6},
                "simulated_demand": [
                    {
                        "currency": "INR",
                        "potential_amount_minor": 800,
                        "captured_amount_minor": 600,
                        "lost_amount_minor": 200,
                        "not_measured_amount_minor": 0,
                    }
                ],
            },
        },
    ]

    aggregates = _comparison_aggregates(reports)
    delta = _comparison_delta(aggregates)

    assert aggregates["RAW"]["simulated_demand_by_currency"][0]["captured_amount_minor"] == 400
    assert aggregates["COMPILED"]["simulated_demand_by_currency"][0]["captured_amount_minor"] == 600
    assert delta["task_completion_rate_mean"] == 0.25
    assert delta["simulated_demand_by_currency"] == [
        {
            "currency": "INR",
            "potential_amount_minor": 0,
            "captured_amount_minor": 200,
            "lost_amount_minor": -200,
            "not_measured_amount_minor": 0,
        }
    ]


def test_unreported_provider_tokens_render_as_unknown() -> None:
    summary = _provider_usage_summary([("gemini-3.5-flash-lite", None, None, None, None, 125)])

    assert summary == {
        "invocations": 1,
        "input_tokens": None,
        "output_tokens": None,
        "reasoning_tokens": None,
        "total_tokens": None,
        "provider_latency_ms": 125,
    }

