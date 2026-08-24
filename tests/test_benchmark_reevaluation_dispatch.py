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
from agentrank_api.benchmark.dispatch import FAILURE_NO_PROVIDER, execute_next_reevaluation
from agentrank_api.benchmark.environment import BenchmarkEnvironmentService
from agentrank_api.benchmark.execution import REFERENCE_ISOLATED_KIND
from agentrank_api.benchmark.launch import MerchantReevaluationService, ReevaluationWorkerService
from agentrank_api.benchmark.lifecycle import BenchmarkRunStatus
from agentrank_api.benchmark.models import BenchmarkRun
from agentrank_api.benchmark.reevaluation import BenchmarkReevaluation, ReevaluationStatus
from agentrank_api.benchmark.report import ExecutorReport
from agentrank_api.benchmark.runner import BenchmarkRunService
from agentrank_api.config import Settings
from agentrank_api.diagnostics.service import DiagnosticsService
from agentrank_api.errors import ConflictError
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
        representation_id=world.representation_id,
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
        assert run.suite_id == world.suite_id
        assert run.environment_id == world.environment_id
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

    async def test_a_worker_holding_another_world_leaves_the_launch_alone(
        self,
        catalog_settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A worker that could only refuse a launch never takes it from one that could serve it.

        A settled launch is terminal and cannot be deleted, so claiming and failing a launch
        this worker cannot execute would destroy a merchant's request on a coin flip. The claim
        is scoped to the world this process holds instead, so such a launch is simply not seen.
        """
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

        assert outcome is None
        untouched = await reload(session, launch_id)
        assert untouched.status is ReevaluationStatus.QUEUED
        assert untouched.run_id is None

        # And the worker that does hold the frozen world still runs it.
        served = await execute_next_reevaluation(
            session,
            factory,
            world=world.authored,
            provider=FakePaymentProvider(),
            settings=settings,
        )
        assert served is not None
        assert served.status == "COMPLETED"


class TestWorldContention:
    async def test_a_world_another_run_owns_leaves_the_launch_queued(
        self,
        catalog_settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """An ordinary answer, not a failure: nothing was executed and it may execute later."""
        settings = without_providers(catalog_settings)
        world = await build_launch_world(session, "contended-shop")
        launch_id = await queue(session, settings, world, request_key="contended-request")
        # An operator run already owns this merchant's world.
        await BenchmarkRunService(session).start_run(
            suite_key=world.suite_key,
            suite_version=world.suite_version,
            merchant_slug=world.merchant_slug,
        )

        outcome = await execute_next_reevaluation(
            session,
            factory,
            world=world.authored,
            provider=FakePaymentProvider(),
            settings=settings,
        )

        assert outcome is not None
        assert outcome.status == "QUEUED"
        assert outcome.failure_code is None
        settled = await reload(session, launch_id)
        assert settled.status is ReevaluationStatus.QUEUED
        assert settled.run_id is None

    async def test_a_conflict_from_inside_execution_is_not_reported_as_queued(
        self,
        catalog_settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Only one conflict means nothing started.

        A mission whose payment cannot be accounted for raises a conflict from inside execution,
        with a run already running and money possibly moved. Reporting that as "still queued"
        would tell an operator nothing had started, so every other conflict propagates and the
        command exits with the evidence. The refusal is injected rather than staged, because
        producing a genuinely unaccounted payment on demand would mean breaking the payment
        kernel, and what is under test here is the dispatcher's error policy.
        """
        settings = without_providers(catalog_settings)
        world = await build_launch_world(session, "propagating-shop")
        launch_id = await queue(session, settings, world, request_key="propagating-request")

        async def unaccounted(*arguments: object, **keywords: object) -> None:
            raise ConflictError(
                "payment_unaccounted", "a mission dispatched a payment nothing accounts for"
            )

        monkeypatch.setattr(BenchmarkRunService, "execute_started_suite", unaccounted)

        with pytest.raises(ConflictError) as refused:
            await execute_next_reevaluation(
                session,
                factory,
                world=world.authored,
                provider=FakePaymentProvider(),
                settings=settings,
            )

        assert refused.value.reason == "payment_unaccounted"
        # The launch names the run an operator now has to close, rather than looking untouched.
        settled = await reload(session, launch_id)
        assert settled.status is ReevaluationStatus.EXECUTING
        assert settled.run_id is not None


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
            suite_key=world.suite_key,
            suite_version=world.suite_version,
            merchant_slug=world.merchant_slug,
        )
        run_id, merchant_id = started.id, world.merchant_id
        await ReevaluationWorkerService(session).bind_run(launch_id, run_id)
        await runs.abort_run(run_id, merchant_id=merchant_id)

        settled = await ReevaluationWorkerService(session).settle_for_terminal_run(run_id)

        assert settled is not None
        assert settled.status is ReevaluationStatus.FAILED
        assert settled.failure_code == "run_aborted"
        # The merchant's one pending slot is free again.
        plan = await MerchantReevaluationService(session, settings).plan(merchant_id)
        assert plan.pending_reevaluation_id is None

    async def test_a_launch_stranded_by_a_dead_worker_can_still_be_settled(
        self,
        catalog_settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A worker that dies between closing its run and settling its launch leaves one stuck.

        Nothing else can reach it: the dispatcher claims only queued launches, and the merchant's
        one pending slot is held against it. Settling against the already terminal run is the
        recovery, and it settles to whatever that run actually says.
        """
        settings = without_providers(catalog_settings)
        world = await build_launch_world(session, "stranded-shop")
        launch_id = await queue(session, settings, world, request_key="stranded-request")
        runs = BenchmarkRunService(session)
        started = await runs.start_run(
            suite_key=world.suite_key,
            suite_version=world.suite_version,
            merchant_slug=world.merchant_slug,
        )
        run_id, merchant_id = started.id, world.merchant_id
        worker = ReevaluationWorkerService(session)
        await worker.bind_run(launch_id, run_id)
        for key in ("buy-a-charger",):
            await runs.start_mission(run_id, key, merchant_id=merchant_id)
            await runs.record_result(
                run_id,
                key,
                ExecutorReport(merchant_id=merchant_id),
                merchant_id=merchant_id,
            )
        await runs.complete_run(run_id, merchant_id=merchant_id)
        # The worker died here, before settling.

        settled = await worker.settle_for_terminal_run(run_id)

        assert settled is not None
        assert settled.status is ReevaluationStatus.COMPLETED
        assert settled.failure_code is None
        # Running the recovery twice is running it once.
        again = await worker.settle_for_terminal_run(run_id)
        assert again is not None
        assert again.status is ReevaluationStatus.COMPLETED
        plan = await MerchantReevaluationService(session, settings).plan(merchant_id)
        assert plan.pending_reevaluation_id is None

    async def test_a_launch_that_loses_the_bind_race_closes_the_run_it_created(
        self,
        catalog_settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A run no launch names is the one state the schema cannot detect or repair.

        The claim's row lock is released before execution starts, so a second worker reaching
        `bind_run` first is possible in principle. The loser must not leave a run executing
        against a merchant with nothing to explain it, so it closes the run it just created.
        """
        settings = without_providers(catalog_settings)
        world = await build_launch_world(session, "bind-race-shop")
        await queue(session, settings, world, request_key="bind-race-request")

        async def taken(*arguments: object, **keywords: object) -> None:
            raise ConflictError("reevaluation_not_queued", "another worker got there first")

        monkeypatch.setattr(ReevaluationWorkerService, "bind_run", taken)

        with pytest.raises(ConflictError):
            await execute_next_reevaluation(
                session,
                factory,
                world=world.authored,
                provider=FakePaymentProvider(),
                settings=settings,
            )

        monkeypatch.undo()
        runs = (
            await session.execute(
                select(BenchmarkRun).where(BenchmarkRun.merchant_id == world.merchant_id)
            )
        ).scalars()
        statuses = [run.status for run in runs]
        assert statuses == [BenchmarkRunStatus.ABORTED]
        # And the merchant's world is free again, so the launch can still be executed.
        assert (
            await MerchantReevaluationService(session, settings).plan(world.merchant_id)
        ).pending_reevaluation_id is not None
