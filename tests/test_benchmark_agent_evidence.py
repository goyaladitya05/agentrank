"""Trusted persistence for bounded LLM execution evidence."""

import uuid

import pytest
from benchmark_support import mission, suite
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.benchmark.agent_trace import AgentExecutionEvidence, ProviderUsage
from agentrank_api.benchmark.models import AgentProviderUsage, AgentTraceEvent
from agentrank_api.benchmark.repository import (
    AgentEvidenceRepository,
    BenchmarkRunRepository,
    BenchmarkSuiteRepository,
)
from agentrank_api.commerce.repository import MerchantRepository

pytestmark = pytest.mark.anyio


async def _mission_context(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    stored = await BenchmarkSuiteRepository(session).create(suite(mission("evidence")))
    merchant = await MerchantRepository(session).create(slug="test-merchant", name="Test")
    run = await BenchmarkRunRepository(session).create(merchant=merchant, suite=stored)
    await session.commit()
    return run.id, run.merchant_id, run.mission_runs[0].id


async def test_evidence_is_ordered_owned_and_append_only(session: AsyncSession) -> None:
    run_id, merchant_id, mission_run_id = await _mission_context(session)
    evidence = AgentExecutionEvidence()
    evidence.add("MODEL_REQUEST", {"input": "buy a charger"})
    response = evidence.add("MODEL_RESPONSE", {"response_id": "resp_1"})
    evidence.add("TOOL_CALL", {"name": "abstain", "arguments": "{}"})
    evidence.add("TOOL_RESULT", {"ended": "abstained"})
    evidence.add("AGENT_FINAL", {"reason": "structured_abstention"})
    evidence.usages.append(
        ProviderUsage(
            invocation_sequence=1,
            trace_sequence=response,
            provider="openai-responses",
            requested_model="gpt-5.6-terra",
            actual_model="gpt-5.6-terra",
            provider_request_id="req_1",
            provider_latency_ms=17,
            usage={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
        )
    )
    await AgentEvidenceRepository(session).append(
        evidence, mission_run_id=mission_run_id, run_id=run_id, merchant_id=merchant_id
    )
    await session.commit()

    events = list(
        (
            await session.execute(select(AgentTraceEvent).order_by(AgentTraceEvent.sequence))
        ).scalars()
    )
    usage = (await session.execute(select(AgentProviderUsage))).scalar_one()
    assert [event.sequence for event in events] == [1, 2, 3, 4, 5]
    assert {event.mission_run_id for event in events} == {mission_run_id}
    assert usage.trace_event_id == events[1].id
    assert usage.total_tokens == 14

    with pytest.raises(DBAPIError, match="append only"):
        await session.execute(
            text("UPDATE agent_trace_event SET payload = '{}' WHERE id = :id"), {"id": events[0].id}
        )
        await session.commit()
    await session.rollback()


async def test_evidence_cannot_be_bound_to_another_run(session: AsyncSession) -> None:
    run_id, merchant_id, mission_run_id = await _mission_context(session)
    evidence = AgentExecutionEvidence()
    evidence.add("MODEL_RESPONSE", {"response_id": "resp_1"})
    with pytest.raises(DBAPIError, match="fk_agent_trace_event_mission_run"):
        await AgentEvidenceRepository(session).append(
            evidence,
            mission_run_id=mission_run_id,
            run_id=uuid.uuid7(),
            merchant_id=merchant_id,
        )
        await session.commit()
    await session.rollback()
    assert run_id != uuid.UUID(int=0)
