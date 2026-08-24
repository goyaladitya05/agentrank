"""Executing a queued launch in a process the browser has no part in.

These tests run the real dispatcher: a real loopback commerce endpoint, a real short lived
merchant credential bound to the run, real worker subprocesses with no database, and the real
payment kernel behind a deterministic fake provider. A dispatcher tested against fakes would
prove that the orchestration reads well, which is not the thing at risk.

What is asserted is what the launch row and the run row say afterwards, because those are the
only durable statements: that the run carries exactly the identity admission froze, that a
launch this process cannot execute exactly is failed by name rather than run with something
close, that a previous run and its diagnostics are untouched, and that nothing outside the
benchmark world moved.
"""

import uuid
from dataclasses import replace

import pytest
from reevaluation_support import LaunchWorld, build_launch_world, with_openai, without_providers
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentrank_api.benchmark.authored import AuthoredWorld
from agentrank_api.benchmark.dispatch import (
    FAILURE_NO_PROVIDER,
    FAILURE_WORLD_MISMATCH,
    execute_next_reevaluation,
)
from agentrank_api.benchmark.environment import BenchmarkEnvironmentService
from agentrank_api.benchmark.execution import REFERENCE_ISOLATED_KIND
from agentrank_api.benchmark.launch import MerchantReevaluationService, ReevaluationWorkerService
from agentrank_api.benchmark.lifecycle import BenchmarkRunStatus
from agentrank_api.benchmark.models import BenchmarkRun
from agentrank_api.benchmark.reevaluation import BenchmarkReevaluation, ReevaluationStatus
from agentrank_api.benchmark.runner import BenchmarkRunService
from agentrank_api.config import Settings
from agentrank_api.diagnostics.service import DiagnosticsService
from agentrank_api.mandates.models import SpendingMandate
from agentrank_api.payments.fake import FakePaymentProvider
from agentrank_api.payments.models import PaymentAttempt

pytestmark = pytest.mark.anyio


async def queue(
    session: AsyncSession, settings: Settings, world: LaunchWorld, *, request_key: str
) -> uuid.UUID:
    """One admitted launch, through the merchant-facing service the console calls."""
    launch = await MerchantReevaluationService(session, settings).request(
        world.merchant_id,
        representation_id=world.representation.id,
        request_key=request_key,
    )
    return launch.id


async def reload(session: AsyncSession, launch_id: uuid.UUID) -> BenchmarkReevaluation:
    """The launch as it now stands. Sessions here do not expire on commit, so a row loaded
    before the dispatcher ran would otherwise answer with what it said then."""
    row = await session.get(BenchmarkReevaluation, launch_id)
    assert row is not None
    await session.refresh(row)
    return row


class TestNothingToDo:
    async def test_no_queued_launch_is_an_ordinary_answer(
        self,
        catalog_settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        world = await build_launch_world(session, "idle-shop")

        outcome = await execute_next_reevaluation(
            session,
            factory,
            world=world.authored,
            provider=FakePaymentProvider(),
            settings=without_providers(catalog_settings),
        )

        assert outcome is None


class TestExecution:
    async def test_a_queued_launch_becomes_one_completed_run(
        self,
        catalog_settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        settings = without_providers(catalog_settings)
        world = await build_launch_world(session, "dispatch-shop")
        suite_id, environment_id = world.suite.id, world.environment.id
        launch_id = await queue(session, settings, world, request_key="dispatch-request")

        outcome = await execute_next_reevaluation(
            session,
            factory,
            world=world.authored,
            provider=FakePaymentProvider(),
            settings=settings,
        )

        assert outcome is not None
        assert outcome.status == "COMPLETED"
        assert outcome.failure_code is None
        assert outcome.run_id is not None

        settled = await reload(session, launch_id)
        assert settled.status is ReevaluationStatus.COMPLETED
        assert settled.run_id == outcome.run_id
        assert settled.started_at is not None
        assert settled.settled_at is not None

        run = await BenchmarkRunService(session).load(outcome.run_id, merchant_id=world.merchant_id)
        assert run.status is BenchmarkRunStatus.COMPLETED
        assert run.suite_id == suite_id
        assert run.environment_id == environment_id
        assert run.executor_kind == REFERENCE_ISOLATED_KIND
        # The reference buyer never receives a discovery view, so the run honestly pins no
        # representation: it did not test one.
        assert run.representation_id is None

    async def test_a_second_dispatch_finds_nothing_left_to_claim(
        self,
        catalog_settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        settings = without_providers(catalog_settings)
        world = await build_launch_world(session, "once-shop")
        await queue(session, settings, world, request_key="once-request")
        first = await execute_next_reevaluation(
            session,
            factory,
            world=world.authored,
            provider=FakePaymentProvider(),
            settings=settings,
        )
        assert first is not None

        second = await execute_next_reevaluation(
            session,
            factory,
            world=world.authored,
            provider=FakePaymentProvider(),
            settings=settings,
        )

        assert second is None
        runs = (
            await session.execute(
                select(BenchmarkRun).where(BenchmarkRun.merchant_id == world.merchant_id)
            )
        ).scalars()
        assert len(list(runs)) == 1

    async def test_previous_evidence_is_untouched_by_a_new_run(
        self,
        catalog_settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        settings = without_providers(catalog_settings)
        world = await build_launch_world(session, "history-shop")
        await queue(session, settings, world, request_key="history-one")
        first = await execute_next_reevaluation(
            session,
            factory,
            world=world.authored,
            provider=FakePaymentProvider(),
            settings=settings,
        )
        assert first is not None and first.run_id is not None
        diagnostics = DiagnosticsService(session)
        before = await diagnostics.run_diagnostics(first.run_id, merchant_id=world.merchant_id)

        await queue(session, settings, world, request_key="history-two")
        second = await execute_next_reevaluation(
            session,
            factory,
            world=world.authored,
            provider=FakePaymentProvider(),
            settings=settings,
        )

        assert second is not None and second.run_id is not None
        assert second.run_id != first.run_id
        after = await diagnostics.run_diagnostics(first.run_id, merchant_id=world.merchant_id)
        assert after.status == before.status
        assert after.completed_at == before.completed_at
        assert after.metrics == before.metrics
        assert [finding.key for finding in after.findings] == [
            finding.key for finding in before.findings
        ]

    async def test_execution_does_not_move_unrelated_authoritative_state(
        self,
        catalog_settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A benchmark owns its own world and nothing else.

        Another merchant's mandates and payments are the honest control: benchmark execution
        genuinely creates commerce state inside the world it prepared, and what must never move
        is anything belonging to a merchant nobody is measuring.
        """
        settings = without_providers(catalog_settings)
        bystander = await build_launch_world(session, "bystander-shop")
        world = await build_launch_world(session, "isolated-shop")
        await queue(session, settings, world, request_key="isolation-request")

        async def counts(merchant_id: uuid.UUID) -> tuple[int, int]:
            mandates = (
                await session.execute(
                    select(SpendingMandate).where(SpendingMandate.merchant_id == merchant_id)
                )
            ).scalars()
            payments = (
                await session.execute(
                    select(PaymentAttempt).where(PaymentAttempt.merchant_id == merchant_id)
                )
            ).scalars()
            return len(list(mandates)), len(list(payments))

        before = await counts(bystander.merchant_id)
        outcome = await execute_next_reevaluation(
            session,
            factory,
            world=world.authored,
            provider=FakePaymentProvider(),
            settings=settings,
        )

        assert outcome is not None and outcome.status == "COMPLETED"
        assert await counts(bystander.merchant_id) == before


class TestRefusals:
    async def test_a_frozen_model_buyer_without_a_credential_fails_by_name(
        self,
        catalog_settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """No substitution. A launch the merchant was shown as an AI run is not quietly
        downgraded to a deterministic one."""
        world = await build_launch_world(session, "unconfigured-shop")
        launch_id = await queue(
            session, with_openai(catalog_settings), world, request_key="model-request"
        )

        outcome = await execute_next_reevaluation(
            session,
            factory,
            world=world.authored,
            provider=FakePaymentProvider(),
            settings=without_providers(catalog_settings),
        )

        assert outcome is not None
        assert outcome.status == "FAILED"
        assert outcome.failure_code == FAILURE_NO_PROVIDER
        assert outcome.run_id is None
        settled = await reload(session, launch_id)
        assert settled.status is ReevaluationStatus.FAILED
        assert settled.failure_code == FAILURE_NO_PROVIDER
        runs = (
            await session.execute(
                select(BenchmarkRun).where(BenchmarkRun.merchant_id == world.merchant_id)
            )
        ).scalars()
        assert list(runs) == []

    async def test_a_worker_holding_another_world_refuses_the_launch(
        self,
        catalog_settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        settings = without_providers(catalog_settings)
        world = await build_launch_world(session, "matched-shop")
        launch_id = await queue(session, settings, world, request_key="mismatch-request")
        # The same merchant, a newer registered world. The launch froze the older one.
        other = replace(world.authored.fixture, version=world.authored.fixture.version + 1)
        await BenchmarkEnvironmentService(session).register(other)

        outcome = await execute_next_reevaluation(
            session,
            factory,
            world=AuthoredWorld(fixture=other, suite=world.authored.suite),
            provider=FakePaymentProvider(),
            settings=settings,
        )

        assert outcome is not None
        assert outcome.failure_code == FAILURE_WORLD_MISMATCH
        settled = await reload(session, launch_id)
        assert settled.status is ReevaluationStatus.FAILED
        assert settled.run_id is None


class TestOperatorRecovery:
    async def test_aborting_the_run_settles_the_launch_behind_it(
        self,
        catalog_settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        settings = without_providers(catalog_settings)
        world = await build_launch_world(session, "recovery-shop")
        launch_id = await queue(session, settings, world, request_key="recovery-request")
        runs = BenchmarkRunService(session)
        started = await runs.start_run(
            suite_key=world.suite.suite_key,
            suite_version=world.suite.version,
            merchant_slug=world.merchant_slug,
        )
        run_id, merchant_id = started.id, world.merchant_id
        await ReevaluationWorkerService(session).bind_run(launch_id, run_id)
        await runs.abort_run(run_id, merchant_id=merchant_id)

        settled = await ReevaluationWorkerService(session).settle_for_aborted_run(run_id)

        assert settled is not None
        assert settled.status is ReevaluationStatus.FAILED
        assert settled.failure_code == "run_aborted"
        # The merchant's one pending slot is free again.
        plan = await MerchantReevaluationService(session, settings).plan(merchant_id)
        assert plan.pending_reevaluation_id is None
