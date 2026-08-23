"""Controlled compiler impact experiment protections."""

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from benchmark_support import VOLTEDGE, suite
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.benchmark.environment import BenchmarkEnvironmentService
from agentrank_api.benchmark.execution import ExecutorIdentity
from agentrank_api.benchmark.experiment import (
    CompilerImpactExperimentService,
    RepresentationKind,
)
from agentrank_api.benchmark.lifecycle import BenchmarkRunStatus
from agentrank_api.benchmark.llm import AgentConfiguration, mission_input
from agentrank_api.benchmark.models import BenchmarkEnvironment, BenchmarkSuite
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

    assert [sample.representation_kind for sample in samples] == [
        RepresentationKind.RAW,
        RepresentationKind.COMPILED,
        RepresentationKind.RAW,
        RepresentationKind.COMPILED,
    ]
    assert [sample.execution_ordinal for sample in samples] == [1, 2, 3, 4]
    assert all(sample.source_snapshot_id == source.id for sample in samples[::2])
    assert all(sample.representation_id == compiled.id for sample in samples[1::2])
    assert experiment.buyer_configuration_digest == config.configuration_digest
    assert experiment.source_snapshot_id == compiled.source_snapshot_id
    assert compiled.compiler_run_id is not None


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
