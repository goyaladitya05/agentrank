"""Focused tests for the real-model boundary without calling a real provider."""

import json
import uuid
from typing import cast

import httpx2
import pytest
from benchmark_support import brief

from agentrank_api.benchmark import worker as benchmark_worker
from agentrank_api.benchmark.agent_trace import AgentExecutionEvidence, safe_payload
from agentrank_api.benchmark.http_buyer import HttpBuyerCommerceSurface
from agentrank_api.benchmark.llm import (
    GEMINI_PROVIDER,
    TOOL_SCHEMA_DIGEST,
    TOOL_SCHEMAS,
    AgentConfiguration,
    GeminiInteractionsProvider,
    LLMBuyer,
    ProviderResponse,
    ProviderToolCall,
    ScriptedAgentProvider,
    mission_input,
)
from agentrank_api.benchmark.wire import LLM_STRATEGY, MissionRequest
from agentrank_api.cli.benchmark import _worker_environment
from agentrank_api.config import Settings

pytestmark = pytest.mark.anyio


def test_agent_configuration_digest_is_semantic_and_secret_free() -> None:
    first = AgentConfiguration(provider="openai-responses", requested_model="gpt-5.6-terra")
    same = AgentConfiguration(provider="openai-responses", requested_model="gpt-5.6-terra")
    changed = AgentConfiguration(
        provider="openai-responses", requested_model="gpt-5.6-terra", max_tool_calls=25
    )
    assert first.configuration_digest == same.configuration_digest
    assert first.configuration_digest != changed.configuration_digest
    assert "key" not in json.dumps(first.payload()).lower()
    assert AgentConfiguration.from_payload(first.payload()) == first
    gemini = AgentConfiguration(provider=GEMINI_PROVIDER, requested_model="gemini-3.7-flash")
    assert gemini.configuration_digest != first.configuration_digest


async def test_gemini_adapter_maps_function_calls_and_continuations() -> None:
    requests: list[dict[str, object]] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx2.Response(
                200,
                json={
                    "id": "gemini-response-1",
                    "model": "gemini-3.7-flash-001",
                    "status": "requires_action",
                    "steps": [
                        {
                            "type": "function_call",
                            "id": "call-1",
                            "name": "abstain",
                            "arguments": {"reason": "none"},
                        }
                    ],
                    "usage": {"total_tokens": 7},
                },
            )
        return httpx2.Response(
            200,
            json={
                "id": "gemini-response-2",
                "model": "gemini-3.7-flash-001",
                "status": "completed",
                "steps": [{"type": "model_output", "content": [{"type": "text", "text": "done"}]}],
            },
        )

    configuration = AgentConfiguration(provider=GEMINI_PROVIDER, requested_model="gemini-3.7-flash")
    provider = GeminiInteractionsProvider(configuration, "test-key")
    await provider._client.aclose()
    provider._client = httpx2.AsyncClient(
        transport=httpx2.MockTransport(respond), base_url="https://example.test"
    )
    first = await provider.respond(previous_response_id=None, input_items=[mission_input(brief())])
    second = await provider.respond(
        previous_response_id=first.response_id,
        input_items=[
            {
                "type": "function_call_output",
                "call_id": first.tool_calls[0].call_id,
                "output": '{"ended":"abstained"}',
            }
        ],
    )
    await provider.aclose()

    assert first.tool_calls[0].name == "abstain"
    assert first.tool_calls[0].arguments == '{"reason":"none"}'
    assert first.usage == {"total_tokens": 7}
    assert first.provider_status == "requires_action"
    assert second.text == "done"
    assert isinstance(requests[0]["tools"], list)
    assert requests[0]["tools"][0]["type"] == "function"
    response_step = cast(list[dict[str, object]], requests[1]["input"])[0]
    assert response_step["call_id"] == "call-1"
    assert response_step["name"] == "abstain"


async def test_gemini_adapter_rejects_nonterminal_interaction_status() -> None:
    def respond(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"id": "gemini-failed", "status": "failed", "steps": []})

    configuration = AgentConfiguration(provider=GEMINI_PROVIDER, requested_model="gemini-3.7-flash")
    provider = GeminiInteractionsProvider(configuration, "test-key")
    await provider._client.aclose()
    provider._client = httpx2.AsyncClient(
        transport=httpx2.MockTransport(respond), base_url="https://example.test"
    )
    with pytest.raises(RuntimeError, match="status"):
        await provider.respond(previous_response_id=None, input_items=[mission_input(brief())])
    await provider.aclose()


def test_selected_worker_provider_key_excludes_other_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "parent-openai")
    monkeypatch.setenv("GEMINI_API_KEY", "parent-gemini")
    settings = Settings(
        postgres_password="test-password",
        openai_api_key="selected-openai",
        gemini_api_key="selected-gemini",
    )  # type: ignore[call-arg]

    gemini = _worker_environment(settings, GEMINI_PROVIDER)
    openai = _worker_environment(settings, "openai-responses")

    assert gemini["GEMINI_API_KEY"] == "selected-gemini"
    assert "OPENAI_API_KEY" not in gemini
    assert openai["OPENAI_API_KEY"] == "selected-openai"
    assert "GEMINI_API_KEY" not in openai


def test_agent_configuration_rejects_an_inconsistent_snapshot() -> None:
    configuration = AgentConfiguration(provider="openai-responses", requested_model="gpt-5.6-terra")
    snapshot = configuration.payload()
    snapshot["prompt_digest"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="does not match"):
        AgentConfiguration.from_payload(snapshot)


def test_tool_schema_is_closed_and_digest_changes_for_meaningful_content() -> None:
    names = {tool["name"] for tool in TOOL_SCHEMAS}
    assert names == {
        "search_products",
        "get_product",
        "create_checkout",
        "inspect_checkout",
        "prepare_checkout",
        "complete_checkout",
        "abstain",
    }
    assert TOOL_SCHEMA_DIGEST.startswith("sha256:")
    assert not names & {"shell", "filesystem", "http", "authorize_spending", "state_requirements"}
    assert TOOL_SCHEMAS[0]["parameters"]["required"] == [
        "query",
        "max_price_amount_minor",
        "currency",
    ]


def test_evidence_redacts_and_rejects_non_response_usage() -> None:
    payload = safe_payload({"OPENAI_API_KEY": "nope", "AuthorizationHeader": "nope"})
    assert payload == {"OPENAI_API_KEY": "[redacted]", "AuthorizationHeader": "[redacted]"}
    with pytest.raises(ValueError, match="model response"):
        AgentExecutionEvidence.from_payload(
            {
                "events": [{"event_type": "TOOL_RESULT", "payload": {}}],
                "usages": [
                    {
                        "invocation_sequence": 1,
                        "trace_sequence": 1,
                        "provider": "openai-responses",
                        "requested_model": "gpt-5.6-terra",
                        "actual_model": None,
                        "provider_request_id": None,
                        "provider_latency_ms": 0,
                        "usage": {},
                    }
                ],
            }
        )


def test_final_provider_input_has_no_oracle() -> None:
    mission = brief()
    serialized = json.dumps(mission_input(mission), sort_keys=True)
    assert mission.objective in serialized
    for forbidden in ("expected_outcome", "simulated_value", "MissionOracle", "failure_reason"):
        assert forbidden not in serialized


async def test_structured_abstention_is_the_only_model_end_signal() -> None:
    provider = ScriptedAgentProvider(
        [
            ProviderResponse(
                "resp_1",
                "gpt-5.6-terra",
                (ProviderToolCall("call_1", "abstain", '{"reason":"no safe option"}'),),
            ),
        ]
    )
    buyer = LLMBuyer(
        provider,
        cast(HttpBuyerCommerceSurface, object()),
        mandate_id=uuid.uuid7(),
        configuration=AgentConfiguration(
            provider="openai-responses", requested_model="gpt-5.6-terra"
        ),
    )
    report = await buyer.execute(brief(), merchant_id=uuid.uuid7())
    assert report.abstention is not None
    assert report.payment is None
    assert [event.event_type for event in buyer.evidence.events] == [
        "MODEL_REQUEST",
        "MODEL_RESPONSE",
        "TOOL_CALL",
        "TOOL_RESULT",
        "AGENT_FINAL",
    ]
    assert buyer.evidence.usages[0].trace_sequence == 2


async def test_an_invalid_provider_response_cannot_reach_a_tool() -> None:
    provider = ScriptedAgentProvider(
        [ProviderResponse(None, "gpt-5.6-terra", (ProviderToolCall("call", "abstain", "{}"),))]
    )
    buyer = LLMBuyer(
        provider,
        cast(HttpBuyerCommerceSurface, object()),
        mandate_id=uuid.uuid7(),
        configuration=AgentConfiguration(
            provider="openai-responses", requested_model="gpt-5.6-terra"
        ),
    )
    report = await buyer.execute(brief(), merchant_id=uuid.uuid7())
    assert report.error is not None
    assert [event.event_type for event in buyer.evidence.events] == [
        "MODEL_REQUEST",
        "MODEL_RESPONSE",
        "AGENT_ABORT",
    ]


async def test_unknown_tool_is_returned_to_the_model_and_cannot_execute() -> None:
    provider = ScriptedAgentProvider(
        [
            ProviderResponse(
                "resp_1", "gpt-5.6-terra", (ProviderToolCall("call_1", "shell", "{}"),)
            ),
            ProviderResponse(
                "resp_2",
                "gpt-5.6-terra",
                (ProviderToolCall("call_2", "abstain", '{"reason":"stopped"}'),),
            ),
        ]
    )
    buyer = LLMBuyer(
        provider,
        cast(HttpBuyerCommerceSurface, object()),
        mandate_id=uuid.uuid7(),
        configuration=AgentConfiguration(
            provider="openai-responses", requested_model="gpt-5.6-terra"
        ),
    )
    report = await buyer.execute(brief(), merchant_id=uuid.uuid7())
    assert report.abstention is not None
    assert len(provider.requests) == 2
    assert "unknown tool: shell" in provider.requests[1][1][0]["output"]


async def test_worker_uses_the_frozen_configuration_delivered_over_the_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen = AgentConfiguration(
        provider="openai-responses", requested_model="frozen-model-b", max_model_turns=7
    )
    observed: list[AgentConfiguration] = []

    class FakeProvider:
        def __init__(self, configuration: AgentConfiguration, api_key: str) -> None:
            assert api_key == "test-provider-key"
            observed.append(configuration)

        async def respond(
            self, *, previous_response_id: str | None, input_items: list[dict[str, object]]
        ) -> ProviderResponse:
            del previous_response_id, input_items
            return ProviderResponse("response", "resolved-model-b")

        async def aclose(self) -> None:
            return None

    monkeypatch.setenv("OPENAI_API_KEY", "test-provider-key")
    monkeypatch.setattr(benchmark_worker, "OpenAIResponsesProvider", FakeProvider)
    request = MissionRequest.from_payload(
        MissionRequest(
            brief=brief(),
            merchant_id=uuid.uuid7(),
            base_url="http://127.0.0.1:1",
            token="ar_dev_00000000000000000000000000000000_" + "0" * 64,
            strategy=LLM_STRATEGY,
            mandate_id=uuid.uuid7(),
            agent_configuration=frozen.payload(),
            merchant_information={"products": []},
        ).to_payload()
    )
    report = await benchmark_worker.execute(request)
    assert observed == [frozen]
    assert report.error is not None


async def test_worker_selects_gemini_from_the_frozen_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen = AgentConfiguration(provider=GEMINI_PROVIDER, requested_model="gemini-3.7-flash")
    observed: list[AgentConfiguration] = []

    class FakeProvider:
        def __init__(self, configuration: AgentConfiguration, api_key: str) -> None:
            assert api_key == "test-gemini-key"
            observed.append(configuration)

        async def respond(
            self, *, previous_response_id: str | None, input_items: list[dict[str, object]]
        ) -> ProviderResponse:
            del previous_response_id, input_items
            return ProviderResponse("response", "gemini-3.7-flash")

        async def aclose(self) -> None:
            return None

    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setattr(benchmark_worker, "GeminiInteractionsProvider", FakeProvider)
    request = MissionRequest(
        brief=brief(),
        merchant_id=uuid.uuid7(),
        base_url="http://127.0.0.1:1",
        token="token",
        strategy=LLM_STRATEGY,
        mandate_id=uuid.uuid7(),
        agent_configuration=frozen.payload(),
        merchant_information={"products": []},
    )
    report = await benchmark_worker.execute(request)
    assert observed == [frozen]
    assert report.error is not None
