"""Provider spending and provider capacity, against the real database that coordinates both.

Every property here is a property of PostgreSQL rather than of application care, which is why
none of it is tested against a fake. What a permit charges is a generated column. What a settled
permit may become is a trigger. Whether two workers can both take the last provider slot is an
advisory lock and a transaction boundary. A test that mocked any of those would be asserting the
comment above the code.

No provider credential is present and none is needed. Nothing in this module reaches a network:
the question is what AgentRank decides before it would.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from launch_support import build_initial_world, queue_launch, with_gemini, with_openai
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentrank_api.benchmark.capacity import (
    ExecutionBudget,
    PermitState,
    ProviderExecutionPermit,
    frozen_budget,
)
from agentrank_api.benchmark.evaluation_launch import (
    BenchmarkEvaluationLaunch,
    BuyerProfile,
    EvaluationLaunchStatus,
)
from agentrank_api.benchmark.execution import ExecutorIdentity
from agentrank_api.benchmark.launch import EvaluationLaunchWorkerService
from agentrank_api.benchmark.lifecycle import BenchmarkRunStatus, MissionRunStatus
from agentrank_api.benchmark.llm import (
    GEMINI_PROVIDER,
    OPENAI_PROVIDER,
    AgentConfiguration,
    executor_kind_for,
)
from agentrank_api.benchmark.models import AgentProviderUsage, AgentTraceEvent, AgentUsageKind
from agentrank_api.benchmark.permits import (
    ExecutionWaitReason,
    ProviderExecutionHaltedError,
    ProviderExecutionService,
    ProviderGrant,
)
from agentrank_api.benchmark.runner import BenchmarkRunService
from agentrank_api.config import Settings
from agentrank_api.errors import ConflictError

pytestmark = pytest.mark.anyio

# The frozen buyer a run records, because a run whose executor kind names a model must say which
# model it was. Nothing here reaches the provider it names.
BUYERS = {
    OPENAI_PROVIDER: AgentConfiguration(provider=OPENAI_PROVIDER, requested_model="gpt-5.6-terra"),
    GEMINI_PROVIDER: AgentConfiguration(
        provider=GEMINI_PROVIDER, requested_model="gemini-3.7-flash"
    ),
}

BUDGET = ExecutionBudget(
    policy_version=1,
    mission_count=4,
    max_model_turns=4,
    max_provider_requests=24,
    max_requests_per_mission=8,
)


async def a_run(session: AsyncSession, slug: str) -> tuple[uuid.UUID, uuid.UUID]:
    """One merchant with a benchmark world and one running benchmark run against it."""
    world = await build_initial_world(session, slug)
    started = await _start(session, world, OPENAI_PROVIDER)
    await session.commit()
    return world.merchant_id, started


async def reserve(
    session: AsyncSession,
    *,
    merchant_id: uuid.UUID,
    run_id: uuid.UUID,
    mission_key: str,
    provider: str = OPENAI_PROVIDER,
    budget: ExecutionBudget = BUDGET,
) -> ProviderGrant:
    return await ProviderExecutionService(session).reserve(
        merchant_id=merchant_id,
        launch_id=None,
        run_id=run_id,
        mission_key=mission_key,
        attempt=1,
        provider=provider,
        requested_model="gpt-5.6-terra",
        budget=budget,
    )


async def charged(session: AsyncSession, run_id: uuid.UUID) -> int:
    permits = (
        await session.execute(
            select(ProviderExecutionPermit).where(ProviderExecutionPermit.run_id == run_id)
        )
    ).scalars()
    return sum(permit.charged_requests for permit in permits)


# The policy an operator states, and the one a deployment that stated nothing runs under.


async def test_a_deployment_that_configured_nothing_runs_under_a_conservative_default(
    session: AsyncSession,
) -> None:
    """A default rather than a refusal, and reported as a default rather than as a choice.

    A deployment that could not run an evaluation until somebody wrote a policy row would be
    worse than one that runs under a narrow default, and an operator has to be able to tell a
    number nobody chose from a number somebody did.
    """
    policy = await ProviderExecutionService(session).policy(OPENAI_PROVIDER)

    assert policy.configured is False
    assert policy.enabled is True
    assert policy.max_concurrent_launches == 1
    assert policy.max_requests_per_window is None


async def test_writing_a_policy_bumps_its_version_every_time(session: AsyncSession) -> None:
    """The version is what a launch freezes, so an operator's decision is always pointable at."""
    service = ProviderExecutionService(session)

    first = await service.set_policy(OPENAI_PROVIDER, max_concurrent_launches=3)
    second = await service.set_policy(OPENAI_PROVIDER, enabled=False)

    assert (first.version, second.version) == (2, 3)
    assert second.max_concurrent_launches == 3
    assert second.configured is True


async def test_a_window_ceiling_can_be_set_and_removed_without_disabling_the_provider(
    session: AsyncSession,
) -> None:
    """Null is no ceiling and zero would be a provider nobody may call, which `enabled` says."""
    service = ProviderExecutionService(session)

    await service.set_policy(OPENAI_PROVIDER, max_requests_per_window=50)
    cleared = await service.set_policy(OPENAI_PROVIDER, clear_window_cap=True)

    assert cleared.max_requests_per_window is None
    assert cleared.enabled is True


async def test_governance_refuses_to_reason_about_a_provider_it_does_not_know(
    session: AsyncSession,
) -> None:
    with pytest.raises(ValueError, match="does not know"):
        await ProviderExecutionService(session).policy("some-other-provider")


# What a reservation charges, and what settling it can and cannot do to that charge.


async def test_reserving_charges_the_whole_grant_before_any_call_can_be_made(
    session: AsyncSession,
) -> None:
    """Committed before the process that could spend it exists, and charged in full until it is
    settled. A reservation that charged nothing until a worker reported would be an allowance a
    crash could spend twice."""
    merchant_id, run_id = await a_run(session, "reserve-shop")

    grant = await reserve(session, merchant_id=merchant_id, run_id=run_id, mission_key="m-1")

    assert grant.granted_requests == BUDGET.max_requests_per_mission
    assert await charged(session, run_id) == BUDGET.max_requests_per_mission


async def test_reconciling_charges_what_the_worker_reported_and_releases_the_rest(
    session: AsyncSession,
) -> None:
    merchant_id, run_id = await a_run(session, "reconcile-shop")
    grant = await reserve(session, merchant_id=merchant_id, run_id=run_id, mission_key="m-1")

    await ProviderExecutionService(session).reconcile(grant.permit_id, consumed_requests=3)

    assert await charged(session, run_id) == 3


async def test_a_reported_count_above_the_grant_is_charged_as_the_grant(
    session: AsyncSession,
) -> None:
    """The allowance in the worker cannot let more through than it was given, so a larger number
    is a defect on the trusted side and charging the grant is the safe reading of one."""
    merchant_id, run_id = await a_run(session, "clamp-shop")
    grant = await reserve(session, merchant_id=merchant_id, run_id=run_id, mission_key="m-1")

    await ProviderExecutionService(session).reconcile(grant.permit_id, consumed_requests=999)

    assert await charged(session, run_id) == grant.granted_requests


async def test_an_unknown_outcome_charges_the_whole_grant_and_says_it_is_an_assumption(
    session: AsyncSession,
) -> None:
    """Crash boundary B: the worker may have reached the provider and nobody can establish it.

    Overcounting is the safe direction for a spending bound, and the state is what keeps the
    overcount visible rather than silently folded into a total.
    """
    merchant_id, run_id = await a_run(session, "assume-shop")
    grant = await reserve(session, merchant_id=merchant_id, run_id=run_id, mission_key="m-1")

    await ProviderExecutionService(session).assume_spent(grant.permit_id)

    permit = await session.get(ProviderExecutionPermit, grant.permit_id)
    assert permit is not None
    assert permit.state is PermitState.ASSUMED_SPENT
    assert permit.consumed_requests is None
    assert await charged(session, run_id) == grant.granted_requests


async def test_a_permit_that_was_assumed_spent_can_never_be_reconciled_down(
    session: AsyncSession,
) -> None:
    """The one accounting move this system must never make, refused by the database.

    Lowering an assumed charge would restore allowance for a request that may already have been
    paid for. The service returns without touching it and the trigger refuses even if a future
    recovery path were written in the wrong order.
    """
    merchant_id, run_id = await a_run(session, "terminal-shop")
    grant = await reserve(session, merchant_id=merchant_id, run_id=run_id, mission_key="m-1")
    service = ProviderExecutionService(session)
    await service.assume_spent(grant.permit_id)

    await service.reconcile(grant.permit_id, consumed_requests=0)

    assert await charged(session, run_id) == grant.granted_requests
    with pytest.raises(DBAPIError, match="never reopens"):
        await session.execute(
            update(ProviderExecutionPermit)
            .where(ProviderExecutionPermit.id == grant.permit_id)
            .values(state=PermitState.RECONCILED, consumed_requests=0)
        )
    await session.rollback()


async def test_a_released_permit_charges_nothing_and_gives_the_allowance_back(
    session: AsyncSession,
) -> None:
    """Crash boundary A: the process died before the provider could have been reached.

    Written only where trusted evidence establishes that, which is the two worker exits that
    happen before a mission is read and a process that could not be started at all.
    """
    merchant_id, run_id = await a_run(session, "release-shop")
    grant = await reserve(session, merchant_id=merchant_id, run_id=run_id, mission_key="m-1")

    await ProviderExecutionService(session).release(grant.permit_id)

    assert await charged(session, run_id) == 0


async def test_reserving_twice_for_one_intended_attempt_yields_one_permit(
    session: AsyncSession,
) -> None:
    """AgentRank's own retry after a lost database answer must not buy a second grant."""
    merchant_id, run_id = await a_run(session, "idempotent-shop")

    first = await reserve(session, merchant_id=merchant_id, run_id=run_id, mission_key="m-1")
    second = await reserve(session, merchant_id=merchant_id, run_id=run_id, mission_key="m-1")

    assert first.permit_id == second.permit_id
    assert await charged(session, run_id) == first.granted_requests


async def test_reserving_again_after_a_permit_settled_is_refused_rather_than_granted(
    session: AsyncSession,
) -> None:
    """A settled attempt is finished. Re-granting it would be a second mission's worth of money
    charged to an identity that already has an answer."""
    merchant_id, run_id = await a_run(session, "settled-shop")
    grant = await reserve(session, merchant_id=merchant_id, run_id=run_id, mission_key="m-1")
    await ProviderExecutionService(session).reconcile(grant.permit_id, consumed_requests=2)

    with pytest.raises(ConflictError) as refused:
        await reserve(session, merchant_id=merchant_id, run_id=run_id, mission_key="m-1")
    assert refused.value.reason == "permit_already_settled"


async def test_a_permit_is_spending_evidence_and_cannot_be_deleted(
    session: AsyncSession,
) -> None:
    merchant_id, run_id = await a_run(session, "undeletable-shop")
    grant = await reserve(session, merchant_id=merchant_id, run_id=run_id, mission_key="m-1")

    with pytest.raises(DBAPIError, match="cannot be deleted"):
        await session.execute(
            text("DELETE FROM provider_execution_permit WHERE id = :id"),
            {"id": grant.permit_id},
        )
    await session.rollback()


async def test_a_permit_is_never_written_already_settled(session: AsyncSession) -> None:
    """A row inserted closed would be spending nobody reserved and nobody could have observed."""
    merchant_id, run_id = await a_run(session, "insert-shop")

    with pytest.raises(DBAPIError, match="written reserved"):
        await session.execute(
            text(
                "INSERT INTO provider_execution_permit (id, merchant_id, run_id, mission_key,"
                " attempt, attempt_key, provider, requested_model, policy_version,"
                " granted_requests, consumed_requests, state, closed_at)"
                " VALUES (gen_random_uuid(), :merchant, :run, 'm-1', 1, 'k', 'openai-responses',"
                " 'gpt-5.6-terra', 1, 4, 4, 'RECONCILED', now())"
            ),
            {"merchant": merchant_id, "run": run_id},
        )
    await session.rollback()


# When AgentRank stops paying, and why it says it stopped.


async def test_a_run_that_has_spent_its_budget_is_refused_another_permit(
    session: AsyncSession,
) -> None:
    """Retry amplification reaching the bound, and stopping rather than spending past it."""
    merchant_id, run_id = await a_run(session, "exhausted-shop")
    for ordinal in range(BUDGET.max_provider_requests // BUDGET.max_requests_per_mission):
        await reserve(session, merchant_id=merchant_id, run_id=run_id, mission_key=f"m-{ordinal}")

    with pytest.raises(ProviderExecutionHaltedError) as halted:
        await reserve(session, merchant_id=merchant_id, run_id=run_id, mission_key="m-last")

    assert halted.value.reason is ExecutionWaitReason.LAUNCH_BUDGET_EXHAUSTED
    assert halted.value.failure_code == "provider_budget_exhausted"


async def test_a_mission_that_cannot_be_given_a_request_per_turn_stops_the_run(
    session: AsyncSession,
) -> None:
    """Exactly at the limit rather than past it. A mission funded for fewer requests than the
    turns it is allowed is not the mission the merchant was shown, so nothing runs it."""
    merchant_id, run_id = await a_run(session, "floor-shop")
    budget = ExecutionBudget(
        policy_version=1,
        mission_count=2,
        max_model_turns=4,
        max_provider_requests=7,
        max_requests_per_mission=4,
    )

    first = await reserve(
        session, merchant_id=merchant_id, run_id=run_id, mission_key="m-1", budget=budget
    )
    assert first.granted_requests == 4

    with pytest.raises(ProviderExecutionHaltedError) as halted:
        await reserve(
            session, merchant_id=merchant_id, run_id=run_id, mission_key="m-2", budget=budget
        )
    assert halted.value.reason is ExecutionWaitReason.LAUNCH_BUDGET_EXHAUSTED


async def test_reconciled_allowance_comes_back_and_lets_the_rest_of_a_suite_run(
    session: AsyncSession,
) -> None:
    """The reason reservation is conservative rather than final: a mission that used two of its
    eight leaves six for the missions after it."""
    merchant_id, run_id = await a_run(session, "returned-shop")
    grant = await reserve(session, merchant_id=merchant_id, run_id=run_id, mission_key="m-1")
    await ProviderExecutionService(session).reconcile(grant.permit_id, consumed_requests=2)

    for ordinal in range(2, 5):
        await reserve(session, merchant_id=merchant_id, run_id=run_id, mission_key=f"m-{ordinal}")

    # Three more missions fit where two would have, and the last of them is granted what is left
    # rather than a full share: the allowance is a ceiling on the launch, not a per-mission quota.
    assert await charged(session, run_id) == BUDGET.max_provider_requests


async def test_a_paused_provider_admits_no_reservation_at_all(session: AsyncSession) -> None:
    """Paused is AgentRank's decision and destroys nothing: the reason says so in those words."""
    merchant_id, run_id = await a_run(session, "paused-shop")
    await ProviderExecutionService(session).set_policy(OPENAI_PROVIDER, enabled=False)

    with pytest.raises(ProviderExecutionHaltedError) as halted:
        await reserve(session, merchant_id=merchant_id, run_id=run_id, mission_key="m-1")

    assert halted.value.reason is ExecutionWaitReason.PROVIDER_PAUSED
    assert halted.value.failure_code == "provider_execution_paused"
    assert await charged(session, run_id) == 0


async def test_resuming_a_paused_provider_admits_work_again(session: AsyncSession) -> None:
    merchant_id, run_id = await a_run(session, "resumed-shop")
    service = ProviderExecutionService(session)
    await service.set_policy(OPENAI_PROVIDER, enabled=False)
    await service.set_policy(OPENAI_PROVIDER, enabled=True)

    grant = await reserve(session, merchant_id=merchant_id, run_id=run_id, mission_key="m-1")

    assert grant.granted_requests > 0


async def test_a_deployment_window_ceiling_stops_reservations_at_its_boundary(
    session: AsyncSession,
) -> None:
    """The operator's own safety cap, measured over a rolling window from the database clock."""
    merchant_id, run_id = await a_run(session, "window-shop")
    await ProviderExecutionService(session).set_policy(OPENAI_PROVIDER, max_requests_per_window=10)

    first = await reserve(session, merchant_id=merchant_id, run_id=run_id, mission_key="m-1")
    assert first.granted_requests == 8

    # Two of the ten are left, which is fewer than one request per model turn, so the next
    # mission is refused rather than started on an allowance that could not carry it.
    with pytest.raises(ProviderExecutionHaltedError) as halted:
        await reserve(session, merchant_id=merchant_id, run_id=run_id, mission_key="m-2")
    assert halted.value.reason is ExecutionWaitReason.PROVIDER_WINDOW_CAP_REACHED
    assert halted.value.failure_code == "provider_window_cap_reached"


async def test_spending_older_than_the_window_stops_counting_against_the_ceiling(
    session: AsyncSession,
) -> None:
    """A rolling window rather than a counter something resets: there is nothing to schedule and
    nothing to get wrong, because a permit stops counting when it becomes older than the window.
    """
    merchant_id, run_id = await a_run(session, "rolling-shop")
    service = ProviderExecutionService(session)
    await service.set_policy(OPENAI_PROVIDER, max_requests_per_window=10, window_seconds=3600)
    # Written directly with an old timestamp, because the permit guard freezes `opened_at` once
    # a reservation exists and that is exactly the property that makes this window trustworthy.
    await session.execute(
        text(
            "INSERT INTO provider_execution_permit (id, merchant_id, run_id, mission_key,"
            " attempt, attempt_key, provider, requested_model, policy_version, granted_requests,"
            " state, opened_at)"
            " VALUES (gen_random_uuid(), :merchant, :run, 'old', 1, 'old-key',"
            " 'openai-responses', 'gpt-5.6-terra', 1, 10, 'RESERVED', :opened)"
        ),
        {
            "merchant": merchant_id,
            "run": run_id,
            "opened": datetime.now(UTC) - timedelta(hours=2),
        },
    )
    await session.commit()

    revived = await reserve(session, merchant_id=merchant_id, run_id=run_id, mission_key="m-2")

    assert revived.granted_requests == BUDGET.max_requests_per_mission


async def test_reserving_against_one_provider_never_charges_another(
    session: AsyncSession,
) -> None:
    """A window ceiling belongs to the provider it was configured for and to nothing else."""
    merchant_id, run_id = await a_run(session, "separate-shop")
    await ProviderExecutionService(session).set_policy(OPENAI_PROVIDER, max_requests_per_window=1)

    grant = await reserve(
        session,
        merchant_id=merchant_id,
        run_id=run_id,
        mission_key="m-1",
        provider=GEMINI_PROVIDER,
    )

    assert grant.granted_requests == BUDGET.max_requests_per_mission


# Usage evidence, which is measured and never estimated.


async def test_usage_separates_request_attempts_from_provider_responses(
    session: AsyncSession, settings: Settings
) -> None:
    """An attempt is what AgentRank counted before the call; a response is what came back.

    They are different numbers and are never added together, because a request that timed out is
    an attempt with no response and still cost whatever it cost.
    """
    world = await build_initial_world(session, "usage-shop")
    launch_id = await queue_launch(
        session, with_openai(settings), world, request_key="usage-request"
    )
    started = await _start(session, world, OPENAI_PROVIDER)
    await EvaluationLaunchWorkerService(session).bind_run(launch_id, started)
    service = ProviderExecutionService(session)
    grant = await service.reserve(
        merchant_id=world.merchant_id,
        launch_id=launch_id,
        run_id=started,
        mission_key="m-1",
        attempt=1,
        provider=OPENAI_PROVIDER,
        requested_model="gpt-5.6-terra",
        budget=BUDGET,
    )
    await service.reconcile(grant.permit_id, consumed_requests=5)

    usage = await service.launch_usage(launch_id)

    assert usage.max_provider_requests is not None
    assert usage.requests_charged == 5
    assert usage.requests_remaining == usage.max_provider_requests - 5
    assert usage.provider_responses == 0
    assert usage.permits_reconciled == 1
    assert usage.has_ambiguous_consumption is False


async def test_usage_keeps_a_missing_token_count_unknown_rather_than_calling_it_zero(
    session: AsyncSession, settings: Settings
) -> None:
    """Gemini has returned responses with no token counters at all, and a sum that treated those
    as zero would be a confident understatement of what was consumed."""
    world = await build_initial_world(session, "tokens-shop")
    launch_id = await queue_launch(
        session, with_openai(settings), world, request_key="tokens-request"
    )
    started = await _start(session, world, OPENAI_PROVIDER)
    await EvaluationLaunchWorkerService(session).bind_run(launch_id, started)
    mission_run_id = await BenchmarkRunService(session).start_mission(
        started, world.authored.suite.missions[0].key, merchant_id=world.merchant_id
    )
    await _usage_row(
        session,
        merchant_id=world.merchant_id,
        run_id=started,
        mission_run_id=mission_run_id,
        sequence=1,
        input_tokens=100,
    )
    await _usage_row(
        session,
        merchant_id=world.merchant_id,
        run_id=started,
        mission_run_id=mission_run_id,
        sequence=2,
        input_tokens=None,
    )
    await session.commit()

    usage = await ProviderExecutionService(session).launch_usage(launch_id)

    assert usage.provider_responses == 2
    assert usage.unknown_usage_invocations == 1
    assert usage.input_tokens is None
    assert usage.total_tokens is None


async def _usage_row(
    session: AsyncSession,
    *,
    merchant_id: uuid.UUID,
    run_id: uuid.UUID,
    mission_run_id: uuid.UUID,
    sequence: int,
    input_tokens: int | None,
) -> None:
    """One provider response's recorded usage, written through the rows the worker's evidence
    lands in so the aggregate is read from real shapes."""
    event = AgentTraceEvent(
        merchant_id=merchant_id,
        run_id=run_id,
        mission_run_id=mission_run_id,
        sequence=sequence,
        event_type="MODEL_RESPONSE",
        payload={},
    )
    session.add(event)
    await session.flush()
    session.add(
        AgentProviderUsage(
            merchant_id=merchant_id,
            run_id=run_id,
            mission_run_id=mission_run_id,
            trace_event_id=event.id,
            invocation_sequence=sequence,
            measurement_kind=AgentUsageKind.PROVIDER_REPORTED,
            provider=OPENAI_PROVIDER,
            requested_model="gpt-5.6-terra",
            input_tokens=input_tokens,
        )
    )
    await session.flush()


# The frozen budget a launch is admitted with, which nothing may move afterwards.


async def test_a_launch_freezes_the_allowance_it_was_admitted_with(
    session: AsyncSession, settings: Settings
) -> None:
    merchant_id_settings = with_openai(settings)
    world = await build_initial_world(session, "frozen-shop")
    launch_id = await queue_launch(
        session, merchant_id_settings, world, request_key="frozen-request"
    )

    launch = await session.get(BenchmarkEvaluationLaunch, launch_id)
    assert launch is not None
    assert launch.buyer_profile is BuyerProfile.AI_BUYER
    expected = frozen_budget(
        await ProviderExecutionService(session).policy(OPENAI_PROVIDER),
        mission_count=len(world.authored.suite.missions),
        max_model_turns=12,
    )
    assert launch.max_provider_requests == expected.max_provider_requests
    assert launch.max_requests_per_mission == expected.max_requests_per_mission
    assert launch.execution_budget_version == expected.policy_version


async def test_widening_the_policy_afterwards_never_rewrites_a_historical_launch(
    session: AsyncSession, settings: Settings
) -> None:
    """A launch describes the allowance it actually ran under, whatever the policy says today."""
    world = await build_initial_world(session, "history-shop")
    launch_id = await queue_launch(
        session, with_openai(settings), world, request_key="history-request"
    )
    launch = await session.get(BenchmarkEvaluationLaunch, launch_id)
    assert launch is not None
    before = launch.max_provider_requests

    await ProviderExecutionService(session).set_policy(
        OPENAI_PROVIDER, launch_retry_allowance_percent=400
    )
    await session.refresh(launch)

    assert launch.max_provider_requests == before


async def test_a_frozen_allowance_cannot_be_moved_by_anything(
    session: AsyncSession, settings: Settings
) -> None:
    """A trigger rather than application care, because the value is what a merchant committed to
    and an update path written next year would not remember."""
    world = await build_initial_world(session, "immutable-shop")
    launch_id = await queue_launch(
        session, with_openai(settings), world, request_key="immutable-request"
    )

    with pytest.raises(DBAPIError, match="frozen at admission"):
        await session.execute(
            update(BenchmarkEvaluationLaunch)
            .where(BenchmarkEvaluationLaunch.id == launch_id)
            .values(max_provider_requests=99_999)
        )
    await session.rollback()


async def test_a_model_launch_cannot_be_admitted_without_an_allowance(
    session: AsyncSession, settings: Settings
) -> None:
    """The insert rule, asserted against the database rather than against the service.

    An application path that forgot to freeze one would otherwise admit a model launch nothing
    bounded, and the merchant would be shown a ceiling that no row records.
    """
    world = await build_initial_world(session, "unbounded-shop")
    launch_id = await queue_launch(
        session, with_openai(settings), world, request_key="unbounded-request"
    )
    launch = await session.get(BenchmarkEvaluationLaunch, launch_id)
    assert launch is not None
    payload = launch.buyer_configuration

    with pytest.raises(DBAPIError, match="admitted with an execution budget"):
        await session.execute(
            text(
                "INSERT INTO benchmark_evaluation_launch (id, merchant_id, request_key, purpose,"
                " source_snapshot_id, suite_id, environment_id, buyer_profile,"
                " buyer_configuration, buyer_configuration_digest, executor_kind, status)"
                " VALUES (gen_random_uuid(), :merchant, 'no-budget', 'INITIAL', :source, :suite,"
                " :environment, 'AI_BUYER', CAST(:configuration AS jsonb), :digest,"
                " 'llm-openai', 'QUEUED')"
            ),
            {
                "merchant": world.merchant_id,
                "source": world.source_snapshot_id,
                "suite": world.suite_id,
                "environment": world.environment_id,
                "configuration": json.dumps(payload),
                "digest": launch.buyer_configuration_digest,
            },
        )
    await session.rollback()


# Provider capacity across workers, which is the one thing a single session cannot prove.


async def test_two_workers_cannot_both_take_the_last_provider_slot(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    """Two merchants, two independent connections, one free slot, and one of them loses.

    The admission runs inside the transaction that makes a launch one of the executing ones,
    which is what makes this correct: a check outside that transaction would let both believe
    they held the last slot, and both would call the provider.
    """
    first = await _bindable_launch(session, settings, "race-one", "race-one-key")
    second = await _bindable_launch(session, settings, "race-two", "race-two-key")
    await session.commit()

    async with factory() as one, factory() as two:
        await _admit_and_bind(one, first)
        with pytest.raises(ProviderExecutionHaltedError) as halted:
            await _admit_and_bind(two, second)

    assert halted.value.reason is ExecutionWaitReason.PROVIDER_CAPACITY_OCCUPIED
    executing = (
        await session.execute(
            select(BenchmarkEvaluationLaunch).where(
                BenchmarkEvaluationLaunch.status == EvaluationLaunchStatus.EXECUTING
            )
        )
    ).scalars()
    assert len(list(executing)) == 1


async def test_a_wider_policy_lets_both_workers_through(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    """The cap is the operator's number and not a hard-coded one."""
    await ProviderExecutionService(session).set_policy(OPENAI_PROVIDER, max_concurrent_launches=2)
    first = await _bindable_launch(session, settings, "wide-one", "wide-one-key")
    second = await _bindable_launch(session, settings, "wide-two", "wide-two-key")
    await session.commit()

    async with factory() as one, factory() as two:
        await _admit_and_bind(one, first)
        await _admit_and_bind(two, second)

    executing = (
        await session.execute(
            select(BenchmarkEvaluationLaunch).where(
                BenchmarkEvaluationLaunch.status == EvaluationLaunchStatus.EXECUTING
            )
        )
    ).scalars()
    assert len(list(executing)) == 2


async def test_an_evaluation_on_one_provider_never_occupies_another_provider(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    """Coordination is per provider, so an OpenAI evaluation does not make a Gemini one wait."""
    first = await _bindable_launch(session, settings, "cross-one", "cross-one-key")
    second = await _bindable_launch(
        session, settings, "cross-two", "cross-two-key", provider=GEMINI_PROVIDER
    )
    await session.commit()

    async with factory() as one, factory() as two:
        await _admit_and_bind(one, first)
        await _admit_and_bind(two, second, provider=GEMINI_PROVIDER)

    executing = (
        await session.execute(
            select(BenchmarkEvaluationLaunch).where(
                BenchmarkEvaluationLaunch.status == EvaluationLaunchStatus.EXECUTING
            )
        )
    ).scalars()
    assert len(list(executing)) == 2


async def test_a_paused_provider_admits_no_launch_and_leaves_it_queued(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    """Nothing is settled and nothing is lost. Queued work stays queued for whoever resumes it."""
    await ProviderExecutionService(session).set_policy(OPENAI_PROVIDER, enabled=False)
    launch = await _bindable_launch(session, settings, "halted-shop", "halted-key")
    await session.commit()

    async with factory() as worker:
        with pytest.raises(ProviderExecutionHaltedError) as halted:
            await _admit_and_bind(worker, launch)

    assert halted.value.reason is ExecutionWaitReason.PROVIDER_PAUSED
    refreshed = await session.get(BenchmarkEvaluationLaunch, launch[0])
    assert refreshed is not None
    await session.refresh(refreshed)
    assert refreshed.status is EvaluationLaunchStatus.QUEUED


async def test_the_capacity_report_names_what_a_queue_is_waiting_on(
    session: AsyncSession, settings: Settings
) -> None:
    """The operator's answer to "why is nothing happening", from the rows that decide it."""
    service = ProviderExecutionService(session)
    await service.set_policy(OPENAI_PROVIDER, enabled=False)

    status = await service.status(OPENAI_PROVIDER)

    assert status.admits_new_work is False
    assert status.wait_reason is ExecutionWaitReason.PROVIDER_PAUSED
    assert [row.provider for row in await service.policies()] == sorted(
        {OPENAI_PROVIDER, GEMINI_PROVIDER}
    )


async def _bindable_launch(
    session: AsyncSession,
    settings: Settings,
    slug: str,
    request_key: str,
    *,
    provider: str = OPENAI_PROVIDER,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A queued launch and a started run for it, ready for one worker to bind together."""
    configured = with_openai(settings) if provider == OPENAI_PROVIDER else with_gemini(settings)
    world = await build_initial_world(session, slug)
    launch_id = await queue_launch(session, configured, world, request_key=request_key)
    started = await _start(session, world, provider)
    return launch_id, started, world.merchant_id


async def _start(session: AsyncSession, world: object, provider: str) -> uuid.UUID:
    """One running benchmark run for this world, recording the buyer its kind names."""
    configuration = BUYERS[provider]
    started = await BenchmarkRunService(session).start_suite(
        suite_key=world.suite_key,  # type: ignore[attr-defined]
        suite_version=world.suite_version,  # type: ignore[attr-defined]
        fixture=world.fixture,  # type: ignore[attr-defined]
        executor=ExecutorIdentity(
            kind=executor_kind_for(provider),
            version=1,
            revision=configuration.configuration_digest,
        ),
        agent_configuration=configuration.payload(),
    )
    return started.id


async def _admit_and_bind(
    worker: AsyncSession,
    launch: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    *,
    provider: str = OPENAI_PROVIDER,
) -> None:
    """Exactly what a dispatcher does: admit inside the transaction that starts the launch."""
    launch_id, run_id, _merchant_id = launch
    service = ProviderExecutionService(worker)

    async def admit(row: BenchmarkEvaluationLaunch) -> None:
        await service.admit_launch(provider, excluding=row.id)

    try:
        await EvaluationLaunchWorkerService(worker).bind_run(launch_id, run_id, admit=admit)
    except ProviderExecutionHaltedError, IntegrityError:
        await worker.rollback()
        raise


# Cancellation and interpretation, which must both stay honest about spending.


async def test_cancelling_a_queued_launch_has_no_provider_spending_to_release(
    session: AsyncSession, settings: Settings
) -> None:
    """A queued launch has no run, and a permit belongs to a run, so there is nothing in flight.

    That is the structural reason cancellation is safe rather than a rule somebody remembers: a
    launch only reaches a provider after a run exists, and cancellation is refused once one does.
    """
    world = await build_initial_world(session, "cancel-shop")
    launch_id = await queue_launch(
        session, with_openai(settings), world, request_key="cancel-request"
    )

    cancelled = await EvaluationLaunchWorkerService(session).cancel_queued(launch_id)

    assert cancelled.status is EvaluationLaunchStatus.FAILED
    permits = (
        await session.execute(
            select(ProviderExecutionPermit).where(
                ProviderExecutionPermit.merchant_id == world.merchant_id
            )
        )
    ).scalars()
    assert list(permits) == []
    usage = await ProviderExecutionService(session).launch_usage(launch_id)
    assert usage.requests_charged == 0


async def test_a_run_stopped_by_governance_is_closed_as_incomplete_rather_than_finished(
    session: AsyncSession, settings: Settings
) -> None:
    """The state a stopped evaluation leaves behind, which is what every reader downstream sees.

    ABORTED rather than COMPLETED, with the missions that never ran still PENDING. That is what
    makes the existing methodology safeguard apply unchanged to this new way of stopping: the
    comparison engine raises RUN_NOT_COMPLETED for a run in this state and refuses to draw a
    before and after from it, which `tests/test_diagnostics_comparison.py` asserts directly.
    """
    world = await build_initial_world(session, "stopped-shop")
    runs = BenchmarkRunService(session)
    started = await _start(session, world, OPENAI_PROVIDER)

    aborted = await runs.abort_run(started, merchant_id=world.merchant_id)

    assert aborted.status is BenchmarkRunStatus.ABORTED
    assert all(result.status is MissionRunStatus.PENDING for result in aborted.mission_runs)
