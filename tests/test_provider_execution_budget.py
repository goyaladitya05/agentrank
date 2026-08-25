"""What one evaluation is allowed to spend at a model provider, and who counts the spending.

No provider credential is available to any test in this module and none is needed. Every provider
is a deterministic fake, every failure is a scripted one, and the whole point of the layer under
test is that it decides before a network call rather than after one, so there is nothing here a
live model would prove that a scripted one does not.

Three questions are asked, in the order the system answers them:

```text
arithmetic   what allowance a launch would be admitted with, and why that number
enforcement  what the process making the calls can actually do with the allowance it was given
protocol     how the number reaches that process and how what it spent comes back
```
"""

import uuid
from dataclasses import replace
from typing import cast

import pytest
from benchmark_support import brief

from agentrank_api.benchmark import worker as benchmark_worker
from agentrank_api.benchmark.capacity import (
    DEFAULT_LAUNCH_RETRY_ALLOWANCE_PERCENT,
    CapacityPolicy,
    frozen_budget,
)
from agentrank_api.benchmark.discovery import storefront_view
from agentrank_api.benchmark.http_buyer import HttpBuyerCommerceSurface
from agentrank_api.benchmark.llm import (
    GEMINI_PROVIDER,
    OPENAI_PROVIDER,
    THROTTLE_RETRY_LIMIT,
    AgentConfiguration,
    LLMBuyer,
    ProviderAllowanceExhaustedError,
    ProviderRequestAllowance,
    ProviderResponse,
    ProviderThrottledError,
    ProviderToolCall,
    ProviderUnavailableError,
    ScriptedAgentProvider,
    executor_kind_for,
)
from agentrank_api.benchmark.report import ExecutorReport
from agentrank_api.benchmark.wire import (
    LLM_STRATEGY,
    REFERENCE_STRATEGY,
    MissionRequest,
    ProtocolError,
    provider_attempts_from_payload,
    worker_result_payload,
)

pytestmark = pytest.mark.anyio

TOKEN = "ar_dev_" + "0" * 32 + "_" + "0" * 64


def policy(**overrides: object) -> CapacityPolicy:
    """The default policy with one field moved, so a test states only what it is about."""
    return replace(CapacityPolicy.default_for(OPENAI_PROVIDER), **overrides)  # type: ignore[arg-type]


def allowance(*responses: object, granted: int) -> ProviderRequestAllowance:
    provider = ScriptedAgentProvider(list(responses))  # type: ignore[arg-type]
    return ProviderRequestAllowance(provider, granted_requests=granted)


def buyer(provider: ProviderRequestAllowance, **overrides: object) -> LLMBuyer:
    return LLMBuyer(
        provider,
        cast(HttpBuyerCommerceSurface, object()),
        mandate_id=uuid.uuid7(),
        configuration=AgentConfiguration(
            provider=OPENAI_PROVIDER,
            requested_model="gpt-5.6-terra",
            **overrides,  # type: ignore[arg-type]
        ),
        discovery=storefront_view(),
    )


# The arithmetic a merchant is shown, which is the only place the number comes from.


def test_a_launch_allowance_is_a_request_per_model_turn_plus_a_retry_allowance() -> None:
    """The ceiling counts retries, because a retry is a provider request that costs money.

    A number that only counted first attempts would understate what a launch may spend by
    exactly the amount a throttled provider costs, which on the one real pilot AgentRank has run
    was most of the traffic.
    """
    budget = frozen_budget(policy(), mission_count=10, max_model_turns=12)

    assert budget.max_provider_requests == 180
    assert DEFAULT_LAUNCH_RETRY_ALLOWANCE_PERCENT == 50
    assert budget.max_provider_requests == 10 * 12 * 3 // 2


def test_one_mission_may_never_be_allowed_more_than_the_whole_launch() -> None:
    """A per-mission ceiling above the total would read as a bound and not be one."""
    budget = frozen_budget(policy(mission_request_multiplier=8), mission_count=1, max_model_turns=4)

    assert budget.max_requests_per_mission <= budget.max_provider_requests


def test_a_wider_policy_produces_a_wider_allowance_and_says_which_policy_it_was() -> None:
    """The version travels with the numbers, so a historical launch can say what it ran under."""
    narrow = frozen_budget(policy(version=3), mission_count=4, max_model_turns=6)
    wide = frozen_budget(
        policy(version=4, launch_retry_allowance_percent=200),
        mission_count=4,
        max_model_turns=6,
    )

    assert (narrow.policy_version, wide.policy_version) == (3, 4)
    assert wide.max_provider_requests > narrow.max_provider_requests


def test_an_allowance_is_never_smaller_than_one_request_per_model_turn() -> None:
    """A policy with no retry allowance at all still funds the turns the buyer is given."""
    budget = frozen_budget(
        policy(launch_retry_allowance_percent=0), mission_count=3, max_model_turns=5
    )

    assert budget.max_provider_requests == 15


def test_a_budget_needs_a_mission_and_a_turn_to_be_a_budget_at_all() -> None:
    with pytest.raises(ValueError, match="at least one mission"):
        frozen_budget(policy(), mission_count=0, max_model_turns=12)


def test_the_executor_kind_a_provider_produces_has_exactly_one_mapping() -> None:
    """Capacity counts executing launches by executor kind, which only works while this holds."""
    assert executor_kind_for(OPENAI_PROVIDER) == "llm-openai"
    assert executor_kind_for(GEMINI_PROVIDER) == "llm-gemini"
    with pytest.raises(ValueError, match="not supported"):
        executor_kind_for("some-other-provider")


# What the process making the calls can do with what it was given.


async def test_an_allowance_admits_exactly_what_it_was_granted_and_then_refuses() -> None:
    granted = allowance(ProviderResponse("r1", "m"), ProviderResponse("r2", "m"), granted=2)

    await granted.respond(previous_response_id=None, input_items=[])
    await granted.respond(previous_response_id=None, input_items=[])

    assert (granted.attempts, granted.remaining) == (2, 0)
    with pytest.raises(ProviderAllowanceExhaustedError) as refused:
        await granted.respond(previous_response_id=None, input_items=[])
    assert (refused.value.granted, refused.value.attempted) == (2, 2)


async def test_a_request_that_times_out_still_spends_its_allowance() -> None:
    """Counted before the call, because a request the provider received still costs money.

    An allowance that decremented on success would be one a timing-out provider could drain
    without limit, which is exactly the failure mode the real pilot produced.
    """
    granted = allowance(ProviderUnavailableError("TimeoutException"), granted=1)

    with pytest.raises(ProviderUnavailableError):
        await granted.respond(previous_response_id=None, input_items=[])

    assert granted.remaining == 0


async def test_a_throttled_request_spends_its_allowance_exactly_like_a_successful_one() -> None:
    granted = allowance(ProviderThrottledError("http_429"), granted=1)

    with pytest.raises(ProviderThrottledError):
        await granted.respond(previous_response_id=None, input_items=[])

    assert granted.attempts == 1


async def test_exhaustion_is_never_reported_as_a_provider_being_unavailable() -> None:
    """The provider is fine. AgentRank declined to pay for another call, which is not an outage.

    A buyer loop that caught this alongside a provider failure would publish an outage that
    never happened, attributed to whichever provider the launch was frozen for.
    """
    granted = allowance(ProviderResponse("r1", "m"), granted=1)
    await granted.respond(previous_response_id=None, input_items=[])

    with pytest.raises(ProviderAllowanceExhaustedError) as refused:
        await granted.respond(previous_response_id=None, input_items=[])

    assert not isinstance(refused.value, ProviderUnavailableError)


def test_an_allowance_cannot_be_built_from_a_number_that_funds_nothing() -> None:
    for size in (0, -1):
        with pytest.raises(ValueError, match="positive number of requests"):
            ProviderRequestAllowance(ScriptedAgentProvider([]), granted_requests=size)


async def test_retries_consume_the_allowance_and_a_mission_stops_when_it_runs_out() -> None:
    """Retry amplification reaching the bound, which is the behaviour the bound exists for.

    Two turns' worth of allowance and a provider that throttles every time: the buyer retries
    inside its own deadline, the retries spend the allowance, and the mission ends because
    AgentRank stopped paying rather than because the provider stopped answering.
    """
    granted = allowance(
        ProviderThrottledError("http_429", retry_after_seconds=0),
        ProviderThrottledError("http_429", retry_after_seconds=0),
        ProviderThrottledError("http_429", retry_after_seconds=0),
        granted=2,
    )
    assert THROTTLE_RETRY_LIMIT >= 3  # the retry loop must outlast the allowance for this to bite

    with pytest.raises(ProviderAllowanceExhaustedError):
        await buyer(granted).execute(brief(), merchant_id=uuid.uuid7())

    assert granted.attempts == 2


async def test_a_refused_request_is_never_written_into_the_trace_as_one_that_was_made() -> None:
    """Evidence describes what happened, so a request nobody sent leaves no MODEL_REQUEST."""
    granted = allowance(
        ProviderThrottledError("http_429", retry_after_seconds=0),
        ProviderThrottledError("http_429", retry_after_seconds=0),
        granted=1,
    )
    model = buyer(granted)

    with pytest.raises(ProviderAllowanceExhaustedError):
        await model.execute(brief(), merchant_id=uuid.uuid7())

    requests = [event for event in model.evidence.events if event.event_type == "MODEL_REQUEST"]
    assert len(requests) == granted.attempts == 1


async def test_a_mission_inside_its_allowance_is_untouched_by_the_bound() -> None:
    """The bound is a ceiling and not a shape. A mission that fits behaves exactly as before."""
    granted = allowance(
        ProviderResponse(
            "r1", "m", (ProviderToolCall("c1", "abstain", '{"reason":"nothing compliant"}'),)
        ),
        granted=8,
    )

    report = await buyer(granted).execute(brief(), merchant_id=uuid.uuid7())

    assert report.abstention is not None
    assert granted.attempts == 1


# How the number crosses to the process that spends it, and how the spending comes back.


def test_a_model_mission_request_without_a_reserved_grant_is_refused() -> None:
    """No live provider call without a trusted permit, enforced where the document is built.

    Defaulting the grant here would be the worker's side of the boundary inventing a budget the
    trusted side never committed to, which is the one thing this protocol must not allow.
    """
    with pytest.raises(ValueError, match="reserved provider request grant"):
        MissionRequest(
            brief=brief(),
            merchant_id=uuid.uuid7(),
            base_url="http://127.0.0.1:1",
            token=TOKEN,
            strategy=LLM_STRATEGY,
            mandate_id=uuid.uuid7(),
            agent_configuration=AgentConfiguration(
                provider=OPENAI_PROVIDER, requested_model="gpt-5.6-terra"
            ).payload(),
            merchant_information={"products": []},
            discovery={"kind": "STOREFRONT"},
        )


def test_a_buyer_that_calls_no_provider_may_not_carry_an_allowance() -> None:
    """A reference mission with a grant would be a reservation nothing could ever spend."""
    with pytest.raises(ValueError, match="only an LLM mission request"):
        MissionRequest(
            brief=brief(),
            merchant_id=uuid.uuid7(),
            base_url="http://127.0.0.1:1",
            token=TOKEN,
            strategy=REFERENCE_STRATEGY,
            provider_request_grant=4,
        )


def test_the_grant_survives_the_round_trip_the_worker_actually_reads_it_through() -> None:
    request = MissionRequest(
        brief=brief(),
        merchant_id=uuid.uuid7(),
        base_url="http://127.0.0.1:1",
        token=TOKEN,
        strategy=LLM_STRATEGY,
        mandate_id=uuid.uuid7(),
        agent_configuration=AgentConfiguration(
            provider=OPENAI_PROVIDER, requested_model="gpt-5.6-terra"
        ).payload(),
        merchant_information={"products": []},
        discovery={"kind": "STOREFRONT"},
        provider_request_grant=7,
    )

    assert MissionRequest.from_payload(request.to_payload()).provider_request_grant == 7


def test_a_worker_that_said_nothing_about_its_spending_is_unknown_and_never_zero() -> None:
    """Null and zero decide different money, so the protocol keeps them apart.

    A worker that never said is one whose consumption the trusted side cannot establish, and its
    whole reservation stays charged. A worker that said zero made no call, and its reservation is
    released to nothing.
    """
    silent = worker_result_payload(ExecutorReport(merchant_id=uuid.uuid7()))
    spoke = worker_result_payload(ExecutorReport(merchant_id=uuid.uuid7()), None, 0)

    assert provider_attempts_from_payload(silent) is None
    assert provider_attempts_from_payload(spoke) == 0


def test_a_reported_attempt_count_that_is_not_a_count_is_refused() -> None:
    payload = worker_result_payload(ExecutorReport(merchant_id=uuid.uuid7()), None, 3)

    with pytest.raises(ProtocolError, match="non negative integer"):
        provider_attempts_from_payload({**payload, "provider_attempts": -1})


def test_a_spent_allowance_leaves_the_worker_process_by_its_own_exit_code() -> None:
    """Not the failure code, because the buyer did not fail and the provider did not fail.

    The trusted side reads the number rather than the text, so a code shared with an ordinary
    failure would have AgentRank's own spending decision recorded against the model.
    """
    codes = {
        benchmark_worker.EXIT_NOT_ISOLATED,
        benchmark_worker.EXIT_PROTOCOL,
        benchmark_worker.EXIT_FAILED,
        benchmark_worker.EXIT_ALLOWANCE_EXHAUSTED,
    }

    assert len(codes) == 4
