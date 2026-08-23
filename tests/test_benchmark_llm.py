"""Focused tests for the real-model boundary without calling a real provider."""

import json
import uuid
from datetime import UTC, datetime
from typing import cast

import httpx2
import pytest
from benchmark_support import brief

from agentrank_api.benchmark import llm as llm_module
from agentrank_api.benchmark import worker as benchmark_worker
from agentrank_api.benchmark.agent_trace import AgentExecutionEvidence, safe_payload
from agentrank_api.benchmark.discovery import (
    BuyerDiscoveryView,
    DiscoveryKind,
    agent_ready_view,
    storefront_view,
)
from agentrank_api.benchmark.http_buyer import HttpBuyerCommerceSurface
from agentrank_api.benchmark.llm import (
    GEMINI_PROVIDER,
    THROTTLE_RETRY_LIMIT,
    TOOL_SCHEMA_DIGEST,
    TOOL_SCHEMAS,
    AgentConfiguration,
    GeminiInteractionsProvider,
    LLMBuyer,
    ProviderResponse,
    ProviderThrottledError,
    ProviderToolCall,
    ProviderUnavailableError,
    ScriptedAgentProvider,
    mission_input,
)
from agentrank_api.benchmark.report import ExecutorReport, ReportedError
from agentrank_api.benchmark.wire import LLM_STRATEGY, MissionRequest
from agentrank_api.checkout.models import CheckoutStatus
from agentrank_api.checkout.schemas import CheckoutLineView, CheckoutView
from agentrank_api.cli.benchmark import _worker_environment
from agentrank_api.commerce.schemas import (
    MerchantSummary,
    ProductDetail,
    ProductSearchResponse,
    ProductSearchResult,
    VariantView,
)
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
        discovery=storefront_view(),
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


async def test_provider_failure_trace_retains_the_safe_failure_detail() -> None:
    provider = ScriptedAgentProvider([ProviderUnavailableError("http_429")])
    buyer = LLMBuyer(
        provider,
        cast(HttpBuyerCommerceSurface, object()),
        mandate_id=uuid.uuid7(),
        configuration=AgentConfiguration(
            provider="openai-responses", requested_model="gpt-5.6-terra"
        ),
        discovery=storefront_view(),
    )

    report = await buyer.execute(brief(), merchant_id=uuid.uuid7())

    assert report.error is not None
    assert buyer.evidence.events[-2].payload == {
        "invocation_sequence": 1,
        "kind": "ProviderUnavailableError",
        "detail": "http_429",
    }


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
        discovery=storefront_view(),
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
        discovery=storefront_view(),
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
            discovery={"kind": "STOREFRONT"},
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
        discovery={"kind": "STOREFRONT"},
    )
    report = await benchmark_worker.execute(request)
    assert observed == [frozen]
    assert report.error is not None


def _buyer(provider: ScriptedAgentProvider, **overrides: object) -> LLMBuyer:
    configuration = AgentConfiguration(
        provider="openai-responses",
        requested_model="gpt-5.6-terra",
        **overrides,  # type: ignore[arg-type]
    )
    return LLMBuyer(
        provider,
        cast(HttpBuyerCommerceSurface, object()),
        mandate_id=uuid.uuid7(),
        configuration=configuration,
        discovery=storefront_view(),
    )


async def test_a_throttled_invocation_retries_without_spending_the_turn_budget() -> None:
    provider = ScriptedAgentProvider(
        [
            ProviderThrottledError("http_429", retry_after_seconds=0),
            ProviderThrottledError("http_429"),
            ProviderResponse(
                "resp_1",
                "gpt-5.6-terra",
                (ProviderToolCall("call_1", "abstain", '{"reason":"none"}'),),
            ),
        ]
    )
    buyer = _buyer(provider, max_model_turns=1)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(llm_module, "THROTTLE_BASE_WAIT_SECONDS", 0.0)
    try:
        report = await buyer.execute(brief(), merchant_id=uuid.uuid7())
    finally:
        monkeypatch.undo()

    assert report.abstention is not None
    assert len(provider.requests) == 3
    assert [event.event_type for event in buyer.evidence.events] == [
        "MODEL_REQUEST",
        "PROVIDER_ERROR",
        "MODEL_REQUEST",
        "PROVIDER_ERROR",
        "MODEL_REQUEST",
        "MODEL_RESPONSE",
        "TOOL_CALL",
        "TOOL_RESULT",
        "AGENT_FINAL",
    ]
    first_throttle = buyer.evidence.events[1].payload
    assert first_throttle["detail"] == "http_429"
    assert first_throttle["attempt"] == 1
    assert first_throttle["retry_after_seconds"] == 0
    second_throttle = buyer.evidence.events[3].payload
    assert second_throttle["attempt"] == 2
    assert second_throttle["retry_after_seconds"] is None
    # The usage still names the one model response this turn produced.
    assert [usage.invocation_sequence for usage in buyer.evidence.usages] == [1]
    assert buyer.evidence.usages[0].trace_sequence == 6


async def test_throttle_retries_are_bounded_and_end_as_a_provider_failure() -> None:
    provider = ScriptedAgentProvider(
        [ProviderThrottledError("http_429", retry_after_seconds=0) for _ in range(10)]
    )
    buyer = _buyer(provider)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(llm_module, "THROTTLE_BASE_WAIT_SECONDS", 0.0)
    try:
        report = await buyer.execute(brief(), merchant_id=uuid.uuid7())
    finally:
        monkeypatch.undo()

    assert report.error is not None
    assert report.abstention is None
    assert len(provider.requests) == THROTTLE_RETRY_LIMIT
    throttles = [event for event in buyer.evidence.events if event.event_type == "PROVIDER_ERROR"]
    # One recorded throttle per attempt, then the terminal failure the buyer ends the mission
    # with once the retries run out.
    assert len(throttles) == THROTTLE_RETRY_LIMIT + 1
    assert [event.payload["attempt"] for event in throttles[:THROTTLE_RETRY_LIMIT]] == [1, 2, 3]
    assert "attempt" not in throttles[-1].payload
    assert [event.event_type for event in buyer.evidence.events[-2:]] == [
        "PROVIDER_ERROR",
        "AGENT_ABORT",
    ]
    assert buyer.evidence.events[-1].payload["reason"] == "provider_unavailable"


async def test_a_wait_past_the_mission_deadline_is_never_taken() -> None:
    provider = ScriptedAgentProvider([ProviderThrottledError("http_429")])
    buyer = _buyer(provider, deadline_seconds=30.0)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(llm_module, "THROTTLE_BASE_WAIT_SECONDS", 60.0)
    try:
        report = await buyer.execute(brief(), merchant_id=uuid.uuid7())
    finally:
        monkeypatch.undo()

    assert report.error is not None
    # The one request was never retried: the bounded wait would have reached past the deadline.
    assert len(provider.requests) == 1


async def test_gemini_adapter_reports_the_provider_retry_guidance_on_a_429() -> None:
    def respond(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(429, headers={"Retry-After": "7"}, json={"error": {}})

    configuration = AgentConfiguration(provider=GEMINI_PROVIDER, requested_model="gemini-3.7-flash")
    provider = GeminiInteractionsProvider(configuration, "test-key")
    await provider._client.aclose()
    provider._client = httpx2.AsyncClient(
        transport=httpx2.MockTransport(respond), base_url="https://example.test"
    )
    with pytest.raises(ProviderThrottledError) as throttled:
        await provider.respond(previous_response_id=None, input_items=[mission_input(brief())])
    await provider.aclose()

    assert str(throttled.value) == "http_429"
    assert throttled.value.retry_after_seconds == 7.0


async def test_gemini_adapter_ignores_an_unusable_retry_header() -> None:
    def respond(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(429, headers={"Retry-After": "soon"}, json={"error": {}})

    configuration = AgentConfiguration(provider=GEMINI_PROVIDER, requested_model="gemini-3.7-flash")
    provider = GeminiInteractionsProvider(configuration, "test-key")
    await provider._client.aclose()
    provider._client = httpx2.AsyncClient(
        transport=httpx2.MockTransport(respond), base_url="https://example.test"
    )
    with pytest.raises(ProviderThrottledError) as throttled:
        await provider.respond(previous_response_id=None, input_items=[mission_input(brief())])
    await provider.aclose()

    assert throttled.value.retry_after_seconds is None


def _search_surface(result: object) -> object:
    class StubSurface:
        async def search_products(self, request: object) -> object:
            return result

    return StubSurface()


def _agent_ready() -> BuyerDiscoveryView:
    return agent_ready_view(
        uuid.uuid7(),
        {"VE-CHG-100-BLK": ({"key": "wattage", "kind": "MEASUREMENT", "unit": "W", "value": 100},)},
    )


async def test_a_storefront_buyer_cannot_read_typed_attributes_from_discovery_tools() -> None:
    variant = VariantView(
        id=uuid.uuid7(),
        sku="VE-CHG-100-BLK",
        label="Black",
        attributes={"color": "black", "wattage": 100},
        price_amount_minor=499900,
        currency="INR",
        inventory_quantity=20,
        is_active=True,
    )
    search = ProductSearchResponse(
        results=[
            ProductSearchResult(
                id=uuid.uuid7(),
                external_id="VE-CHG-100",
                title="100W Multi-Port Charger",
                description="100W three-port USB-C charger.",
                category="chargers",
                is_active=True,
                merchant=MerchantSummary(id=uuid.uuid7(), slug="voltedge", name="VoltEdge"),
                eligible_variants=[variant],
            )
        ],
        count=1,
        limit=20,
    )
    provider = ScriptedAgentProvider(
        [
            ProviderResponse(
                "resp_1",
                "gpt-5.6-terra",
                (
                    ProviderToolCall(
                        "call_1",
                        "search_products",
                        '{"query":"charger","max_price_amount_minor":null,"currency":null}',
                    ),
                ),
            ),
            ProviderResponse(
                "resp_2",
                "gpt-5.6-terra",
                (ProviderToolCall("call_2", "abstain", '{"reason":"none"}'),),
            ),
        ]
    )
    buyer = LLMBuyer(
        provider,
        cast(HttpBuyerCommerceSurface, _search_surface(search)),
        mandate_id=uuid.uuid7(),
        configuration=AgentConfiguration(
            provider="openai-responses", requested_model="gpt-5.6-terra"
        ),
        discovery=storefront_view(),
    )

    report = await buyer.execute(brief(), merchant_id=uuid.uuid7())

    assert report.abstention is not None
    tool_result = next(
        event.payload for event in buyer.evidence.events if event.event_type == "TOOL_RESULT"
    )
    serialized = json.dumps(tool_result)
    assert "attributes" not in serialized
    assert "wattage" not in serialized
    assert (
        tool_result["result"]["results"][0]["eligible_variants"][0]["price_amount_minor"] == 499900
    )


async def test_an_agent_ready_buyer_reads_only_the_representation_facts() -> None:
    variant = VariantView(
        id=uuid.uuid7(),
        sku="VE-CHG-100-BLK",
        label="Black",
        attributes={"color": "black", "wattage": 100},
        price_amount_minor=499900,
        currency="INR",
        inventory_quantity=20,
        is_active=True,
    )
    search = ProductSearchResponse(
        results=[
            ProductSearchResult(
                id=uuid.uuid7(),
                external_id="VE-CHG-100",
                title="100W Multi-Port Charger",
                description="100W three-port USB-C charger.",
                category="chargers",
                is_active=True,
                merchant=MerchantSummary(id=uuid.uuid7(), slug="voltedge", name="VoltEdge"),
                eligible_variants=[variant],
            )
        ],
        count=1,
        limit=20,
    )

    class StubSurface:
        async def search_products(self, request: object) -> object:
            del request
            return search

    provider = ScriptedAgentProvider(
        [
            ProviderResponse(
                "resp_1",
                "gpt-5.6-terra",
                (
                    ProviderToolCall(
                        "call_1",
                        "search_products",
                        '{"query":"charger","max_price_amount_minor":null,"currency":null}',
                    ),
                ),
            ),
            ProviderResponse(
                "resp_2",
                "gpt-5.6-terra",
                (ProviderToolCall("call_2", "abstain", '{"reason":"none"}'),),
            ),
        ]
    )
    buyer = LLMBuyer(
        provider,
        cast(HttpBuyerCommerceSurface, StubSurface()),
        mandate_id=uuid.uuid7(),
        configuration=AgentConfiguration(
            provider="openai-responses", requested_model="gpt-5.6-terra"
        ),
        discovery=_agent_ready(),
    )

    report = await buyer.execute(brief(), merchant_id=uuid.uuid7())

    assert report.abstention is not None
    tool_result = next(
        event.payload for event in buyer.evidence.events if event.event_type == "TOOL_RESULT"
    )
    attributes = tool_result["result"]["results"][0]["eligible_variants"][0]["attributes"]
    assert attributes == [{"key": "wattage", "kind": "MEASUREMENT", "unit": "W", "value": 100}]


async def test_the_quote_channel_hides_attribute_snapshots_for_both_arms_alike() -> None:
    instant = datetime.now(UTC)
    checkout_id = uuid.uuid7()
    line = CheckoutLineView(
        id=uuid.uuid7(),
        variant_id=uuid.uuid7(),
        quantity=1,
        unit_price_amount_minor=499900,
        line_amount_minor=499900,
        currency="INR",
        product_category="chargers",
        variant_attributes={"color": "black", "wattage": 100},
    )
    quote = CheckoutView(
        id=checkout_id,
        merchant_id=uuid.uuid7(),
        mandate_id=uuid.uuid7(),
        currency="INR",
        lines=[line],
        total_quantity=1,
        subtotal_amount_minor=499900,
        shipping_amount_minor=0,
        discount_amount_minor=0,
        total_amount_minor=499900,
        status=CheckoutStatus.OPEN,
        created_at=instant,
        expires_at=instant,
        cancelled_at=None,
        paid_at=None,
    )

    class StubSurface:
        async def create_checkout(self, request: object) -> object:
            del request
            return quote

        async def get_checkout(self, checkout_id: uuid.UUID) -> object:
            return quote

    for view in (storefront_view(), _agent_ready()):
        provider = ScriptedAgentProvider(
            [
                ProviderResponse(
                    "resp_1",
                    "gpt-5.6-terra",
                    (
                        ProviderToolCall(
                            "call_1",
                            "create_checkout",
                            json.dumps(
                                {"items": [{"variant_id": str(uuid.uuid7()), "quantity": 1}]}
                            ),
                        ),
                    ),
                ),
                ProviderResponse(
                    "resp_2",
                    "gpt-5.6-terra",
                    (
                        ProviderToolCall(
                            "call_2",
                            "inspect_checkout",
                            json.dumps({"checkout_id": str(checkout_id)}),
                        ),
                    ),
                ),
                ProviderResponse(
                    "resp_3",
                    "gpt-5.6-terra",
                    (ProviderToolCall("call_3", "abstain", '{"reason":"none"}'),),
                ),
            ]
        )
        buyer = LLMBuyer(
            provider,
            cast(HttpBuyerCommerceSurface, StubSurface()),
            mandate_id=uuid.uuid7(),
            configuration=AgentConfiguration(
                provider="openai-responses", requested_model="gpt-5.6-terra"
            ),
            discovery=view,
        )

        await buyer.execute(brief(), merchant_id=uuid.uuid7())

        results = [
            event.payload for event in buyer.evidence.events if event.event_type == "TOOL_RESULT"
        ]
        quotes = [
            payload for payload in results if "lines" in json.dumps(payload) and "result" in payload
        ]
        assert len(quotes) == 2
        for payload in quotes:
            assert "variant_attributes" not in json.dumps(payload)
            assert payload["result"]["lines"][0]["line_amount_minor"] == 499900


async def test_the_worker_binds_exactly_the_discovery_view_it_was_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[object] = []

    class RecordingBuyer:
        def __init__(
            self,
            provider: object,
            surface: object,
            *,
            mandate_id: object,
            configuration: object,
            discovery: object,
        ) -> None:
            del provider, surface, mandate_id, configuration
            captured.append(discovery)

        async def execute(self, brief: object, **kwargs: object) -> ExecutorReport:
            del kwargs
            self.evidence = AgentExecutionEvidence()
            return ExecutorReport(uuid.uuid7(), error=ReportedError(detail="stopped"))

    frozen = AgentConfiguration(provider=GEMINI_PROVIDER, requested_model="gemini-3.7-flash")
    sent = {
        "kind": "AGENT_READY",
        "representation_id": str(uuid.uuid7()),
        "attributes": [
            {"sku": "SKU-1", "key": "wattage", "kind": "MEASUREMENT", "unit": "W", "value": 100}
        ],
    }
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setattr(benchmark_worker, "LLMBuyer", RecordingBuyer)
    request = MissionRequest(
        brief=brief(),
        merchant_id=uuid.uuid7(),
        base_url="http://127.0.0.1:1",
        token="token",
        strategy=LLM_STRATEGY,
        mandate_id=uuid.uuid7(),
        agent_configuration=frozen.payload(),
        merchant_information={"products": []},
        discovery=sent,
    )
    await benchmark_worker.execute(request)

    assert len(captured) == 1
    view = cast(BuyerDiscoveryView, captured[0])
    assert view.kind is DiscoveryKind.AGENT_READY
    assert view.attributes_by_sku["SKU-1"][0]["value"] == 100


async def test_a_storefront_buyer_cannot_read_typed_attributes_from_product_detail() -> None:
    variant = VariantView(
        id=uuid.uuid7(),
        sku="VE-CHG-100-BLK",
        label="Black",
        attributes={"color": "black", "wattage": 100},
        price_amount_minor=499900,
        currency="INR",
        inventory_quantity=20,
        is_active=True,
    )
    detail = ProductDetail(
        id=uuid.uuid7(),
        external_id="VE-CHG-100",
        title="100W Multi-Port Charger",
        description=None,
        category="chargers",
        is_active=True,
        merchant=MerchantSummary(id=uuid.uuid7(), slug="voltedge", name="VoltEdge"),
        variants=[variant],
    )

    class StubSurface:
        async def get_product(self, product_id: uuid.UUID) -> object:
            del product_id
            return detail

    provider = ScriptedAgentProvider(
        [
            ProviderResponse(
                "resp_1",
                "gpt-5.6-terra",
                (
                    ProviderToolCall(
                        "call_1", "get_product", json.dumps({"product_id": str(uuid.uuid7())})
                    ),
                ),
            ),
            ProviderResponse(
                "resp_2",
                "gpt-5.6-terra",
                (ProviderToolCall("call_2", "abstain", '{"reason":"none"}'),),
            ),
        ]
    )
    buyer = LLMBuyer(
        provider,
        cast(HttpBuyerCommerceSurface, StubSurface()),
        mandate_id=uuid.uuid7(),
        configuration=AgentConfiguration(
            provider="openai-responses", requested_model="gpt-5.6-terra"
        ),
        discovery=storefront_view(),
    )

    report = await buyer.execute(brief(), merchant_id=uuid.uuid7())

    assert report.abstention is not None
    tool_result = next(
        event.payload
        for event in buyer.evidence.events
        if event.event_type == "TOOL_RESULT" and event.payload.get("name") == "get_product"
    )
    assert "attributes" not in json.dumps(tool_result)
    assert tool_result["result"]["variants"][0]["inventory_quantity"] == 20


async def test_authorization_answers_never_name_the_catalog_values_they_compared() -> None:
    """A raw buyer probing refusals cannot read typed facts out of violation details."""
    preparation = {
        "checkout_id": str(uuid.uuid7()),
        "ready": False,
        "authorization": {
            "allowed": False,
            "intent_authorization": {
                "allowed": False,
                "violations": [
                    {
                        # The value is a real IntentViolationCode member.
                        "code": "REQUIRED_ATTRIBUTE_MISMATCH",
                        "constraint_id": None,
                        "line_id": None,
                        "variant_id": str(uuid.uuid7()),
                        "attribute": "wattage",
                        "operator": "GTE",
                        "expected": 100,
                        "actual": {"wattage": 45, "color": "black"},
                    }
                ],
            },
        },
    }

    class StubSurface:
        async def prepare_checkout(self, checkout_id: uuid.UUID) -> object:
            del checkout_id

            class View:
                def model_dump(self, mode: str) -> dict[str, object]:
                    del mode
                    return preparation

            return View()

    for view in (storefront_view(), _agent_ready()):
        provider = ScriptedAgentProvider(
            [
                ProviderResponse(
                    "resp_1",
                    "gpt-5.6-terra",
                    (
                        ProviderToolCall(
                            "call_1",
                            "prepare_checkout",
                            json.dumps({"checkout_id": str(uuid.uuid7())}),
                        ),
                    ),
                ),
                ProviderResponse(
                    "resp_2",
                    "gpt-5.6-terra",
                    (ProviderToolCall("call_2", "abstain", '{"reason":"none"}'),),
                ),
            ]
        )
        buyer = LLMBuyer(
            provider,
            cast(HttpBuyerCommerceSurface, StubSurface()),
            mandate_id=uuid.uuid7(),
            configuration=AgentConfiguration(
                provider="openai-responses", requested_model="gpt-5.6-terra"
            ),
            discovery=view,
        )

        report = await buyer.execute(brief(), merchant_id=uuid.uuid7())

        assert report.abstention is not None
        tool_result = next(
            event.payload
            for event in buyer.evidence.events
            if event.event_type == "TOOL_RESULT" and event.payload.get("name") == "prepare_checkout"
        )
        serialized = json.dumps(tool_result)
        # The catalog value the refusal compared against is gone for both arms alike. (The
        # evidence sanitizer also masks the whole authorization object by key name, but this
        # stripping happens before that, at the tool boundary the model itself reads.)
        assert "actual" not in serialized
        # The catalog's own values for the quoted variant are gone, not merely renamed.
        assert "black" not in serialized
        assert '"wattage": 45' not in serialized
