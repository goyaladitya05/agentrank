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
from launch_support import (
    build_initial_world,
    build_launch_world,
    queue_launch,
    with_openai,
    without_providers,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentrank_api.benchmark.authored import AuthoredWorld
from agentrank_api.benchmark.dispatch import (
    FAILURE_NO_PROVIDER,
    buyer_surface,
    execute_next_launch,
)
from agentrank_api.benchmark.environment import BenchmarkEnvironmentService
from agentrank_api.benchmark.evaluation_launch import (
    BenchmarkEvaluationLaunch,
    EvaluationLaunchStatus,
    EvaluationPurpose,
)
from agentrank_api.benchmark.execution import REFERENCE_ISOLATED_KIND
from agentrank_api.benchmark.launch import (
    EvaluationLaunchWorkerService,
    MerchantEvaluationLaunchService,
)
from agentrank_api.benchmark.lifecycle import BenchmarkRunStatus
from agentrank_api.benchmark.models import BenchmarkRun
from agentrank_api.benchmark.report import ExecutorReport
from agentrank_api.benchmark.runner import BenchmarkRunService
from agentrank_api.config import Settings
from agentrank_api.diagnostics.service import DiagnosticsService
from agentrank_api.errors import ConflictError
from agentrank_api.mandates.models import SpendingMandate
from agentrank_api.payments.fake import FakePaymentProvider
from agentrank_api.payments.models import PaymentAttempt
from agentrank_api.representation.models import MerchantSourceSnapshot
from agentrank_api.representation.projection import compiled_projection, raw_projection

pytestmark = pytest.mark.anyio


async def reload(session: AsyncSession, launch_id: uuid.UUID) -> BenchmarkEvaluationLaunch:
    """The launch as it now stands. Sessions here do not expire on commit, so a row loaded
    before the dispatcher ran would otherwise answer with what it said then."""
    row = await session.get(BenchmarkEvaluationLaunch, launch_id)
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

        outcome = await execute_next_launch(
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
        launch_id = await queue_launch(session, settings, world, request_key="dispatch-request")

        outcome = await execute_next_launch(
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
        assert settled.status is EvaluationLaunchStatus.COMPLETED
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
        await queue_launch(session, settings, world, request_key="once-request")
        first = await execute_next_launch(
            session,
            factory,
            world=world.authored,
            provider=FakePaymentProvider(),
            settings=settings,
        )
        assert first is not None

        second = await execute_next_launch(
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
        await queue_launch(session, settings, world, request_key="history-one")
        first = await execute_next_launch(
            session,
            factory,
            world=world.authored,
            provider=FakePaymentProvider(),
            settings=settings,
        )
        assert first is not None and first.run_id is not None
        diagnostics = DiagnosticsService(session)
        before = await diagnostics.run_diagnostics(first.run_id, merchant_id=world.merchant_id)

        await queue_launch(session, settings, world, request_key="history-two")
        second = await execute_next_launch(
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
        await queue_launch(session, settings, world, request_key="isolation-request")

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
        outcome = await execute_next_launch(
            session,
            factory,
            world=world.authored,
            provider=FakePaymentProvider(),
            settings=settings,
        )

        assert outcome is not None and outcome.status == "COMPLETED"
        assert await counts(bystander.merchant_id) == before


class TestFirstEvaluation:
    """A merchant's first measurement, executed by the same dispatcher and nothing else."""

    async def test_a_queued_first_evaluation_becomes_one_completed_run(
        self,
        catalog_settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        settings = without_providers(catalog_settings)
        world = await build_initial_world(session, "first-dispatch-shop")
        launch_id = await queue_launch(session, settings, world, request_key="first-dispatch")

        outcome = await execute_next_launch(
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
        assert settled.status is EvaluationLaunchStatus.COMPLETED
        assert settled.run_id == outcome.run_id
        assert settled.purpose is EvaluationPurpose.INITIAL

        run = await BenchmarkRunService(session).load(outcome.run_id, merchant_id=world.merchant_id)
        assert run.status is BenchmarkRunStatus.COMPLETED
        assert run.suite_id == world.suite_id
        assert run.environment_id == world.environment_id
        # Nothing was compiled, so the run pins nothing. A representation identifier here would
        # be a claim that an agent-ready surface was measured when none existed.
        assert run.representation_id is None

    async def test_a_first_evaluation_leaves_exactly_one_run_and_no_prior_one(
        self,
        catalog_settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """No synthetic before is written to give the first result something to sit beside."""
        settings = without_providers(catalog_settings)
        world = await build_initial_world(session, "first-only-run-shop")
        launch_id = await queue_launch(session, settings, world, request_key="first-only-run")

        await execute_next_launch(
            session,
            factory,
            world=world.authored,
            provider=FakePaymentProvider(),
            settings=settings,
        )

        runs = list(
            (
                await session.execute(
                    select(BenchmarkRun).where(BenchmarkRun.merchant_id == world.merchant_id)
                )
            ).scalars()
        )
        assert len(runs) == 1
        settled = await reload(session, launch_id)
        assert settled.baseline_run_id is None
        launches = list(
            (
                await session.execute(
                    select(BenchmarkEvaluationLaunch).where(
                        BenchmarkEvaluationLaunch.merchant_id == world.merchant_id
                    )
                )
            ).scalars()
        )
        assert len(launches) == 1

    async def test_a_completed_first_evaluation_reaches_ordinary_diagnostics(
        self,
        catalog_settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The same trusted engine, with no special path for a merchant's first result."""
        settings = without_providers(catalog_settings)
        world = await build_initial_world(session, "first-diagnostics-shop")
        await queue_launch(session, settings, world, request_key="first-diagnostics")

        outcome = await execute_next_launch(
            session,
            factory,
            world=world.authored,
            provider=FakePaymentProvider(),
            settings=settings,
        )

        assert outcome is not None and outcome.run_id is not None
        diagnosis = await DiagnosticsService(session).run_diagnostics(
            outcome.run_id, merchant_id=world.merchant_id
        )
        assert diagnosis.status == "COMPLETED"
        assert diagnosis.metrics.missions_total == len(world.authored.suite.missions)

    async def test_a_first_evaluation_does_not_move_unrelated_authoritative_state(
        self,
        catalog_settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The bootstrap path owns its own world and nothing else, like every other run."""
        settings = without_providers(catalog_settings)
        bystander = await build_launch_world(session, "first-bystander-shop")
        world = await build_initial_world(session, "first-isolated-shop")
        await queue_launch(session, settings, world, request_key="first-isolation")

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
        outcome = await execute_next_launch(
            session,
            factory,
            world=world.authored,
            provider=FakePaymentProvider(),
            settings=settings,
        )

        assert outcome is not None and outcome.status == "COMPLETED"
        assert await counts(bystander.merchant_id) == before


class TestBuyerSurface:
    """What the model buyer is shown, which is the whole methodology of the two commands."""

    async def test_a_first_evaluation_shows_the_ordinary_storefront(
        self, session: AsyncSession
    ) -> None:
        world = await build_initial_world(session, "surface-initial-shop")
        snapshot = await session.get(MerchantSourceSnapshot, world.source_snapshot_id)
        assert snapshot is not None

        surface = buyer_surface(representation=None, source=snapshot)

        # The storefront boundary publishes no typed attribute dictionary at all, and pins no
        # representation, because there is none to pin.
        assert surface.discovery == {"kind": "STOREFRONT"}
        assert surface.merchant_information == raw_projection(snapshot)
        assert "attributes" not in surface.discovery

    async def test_a_reevaluation_shows_the_representation_it_froze(
        self, session: AsyncSession
    ) -> None:
        world = await build_launch_world(session, "surface-compiled-shop")

        surface = buyer_surface(representation=world.representation, source=None)

        assert surface.discovery["kind"] == "AGENT_READY"
        assert surface.discovery["representation_id"] == str(world.representation_id)
        assert surface.merchant_information == compiled_projection(world.representation)

    async def test_a_buyer_is_never_shown_both_or_neither(self, session: AsyncSession) -> None:
        """Construction errors rather than a quietly weakened arm."""
        world = await build_launch_world(session, "surface-invalid-shop")
        snapshot = await session.get(MerchantSourceSnapshot, world.source_snapshot_id)
        assert snapshot is not None

        with pytest.raises(ValueError):
            buyer_surface(representation=world.representation, source=snapshot)
        with pytest.raises(ValueError):
            buyer_surface(representation=None, source=None)


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
        launch_id = await queue_launch(
            session, with_openai(catalog_settings), world, request_key="model-request"
        )

        outcome = await execute_next_launch(
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
        assert settled.status is EvaluationLaunchStatus.FAILED
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
        launch_id = await queue_launch(session, settings, world, request_key="mismatch-request")
        # The same merchant, a newer registered world. The launch froze the older one.
        other = replace(world.authored.fixture, version=world.authored.fixture.version + 1)
        await BenchmarkEnvironmentService(session).register(other)

        outcome = await execute_next_launch(
            session,
            factory,
            world=AuthoredWorld(fixture=other, suite=world.authored.suite),
            provider=FakePaymentProvider(),
            settings=settings,
        )

        assert outcome is None
        untouched = await reload(session, launch_id)
        assert untouched.status is EvaluationLaunchStatus.QUEUED
        assert untouched.run_id is None

        # And the worker that does hold the frozen world still runs it.
        served = await execute_next_launch(
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
        launch_id = await queue_launch(session, settings, world, request_key="contended-request")
        # An operator run already owns this merchant's world.
        await BenchmarkRunService(session).start_run(
            suite_key=world.suite_key,
            suite_version=world.suite_version,
            merchant_slug=world.merchant_slug,
        )

        outcome = await execute_next_launch(
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
        assert settled.status is EvaluationLaunchStatus.QUEUED
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
        launch_id = await queue_launch(session, settings, world, request_key="propagating-request")

        async def unaccounted(*arguments: object, **keywords: object) -> None:
            raise ConflictError(
                "payment_unaccounted", "a mission dispatched a payment nothing accounts for"
            )

        monkeypatch.setattr(BenchmarkRunService, "execute_started_suite", unaccounted)

        with pytest.raises(ConflictError) as refused:
            await execute_next_launch(
                session,
                factory,
                world=world.authored,
                provider=FakePaymentProvider(),
                settings=settings,
            )

        assert refused.value.reason == "payment_unaccounted"
        # The launch names the run an operator now has to close, rather than looking untouched.
        settled = await reload(session, launch_id)
        assert settled.status is EvaluationLaunchStatus.EXECUTING
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
        launch_id = await queue_launch(session, settings, world, request_key="recovery-request")
        runs = BenchmarkRunService(session)
        started = await runs.start_run(
            suite_key=world.suite_key,
            suite_version=world.suite_version,
            merchant_slug=world.merchant_slug,
        )
        run_id, merchant_id = started.id, world.merchant_id
        await EvaluationLaunchWorkerService(session).bind_run(launch_id, run_id)
        await runs.abort_run(run_id, merchant_id=merchant_id)

        settled = await EvaluationLaunchWorkerService(session).settle_for_terminal_run(run_id)

        assert settled is not None
        assert settled.status is EvaluationLaunchStatus.FAILED
        assert settled.failure_code == "run_aborted"
        # The merchant's one pending slot is free again.
        plan = await MerchantEvaluationLaunchService(session, settings).plan(merchant_id)
        assert plan.pending_launch_id is None

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
        launch_id = await queue_launch(session, settings, world, request_key="stranded-request")
        runs = BenchmarkRunService(session)
        started = await runs.start_run(
            suite_key=world.suite_key,
            suite_version=world.suite_version,
            merchant_slug=world.merchant_slug,
        )
        run_id, merchant_id = started.id, world.merchant_id
        worker = EvaluationLaunchWorkerService(session)
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
        assert settled.status is EvaluationLaunchStatus.COMPLETED
        assert settled.failure_code is None
        # Running the recovery twice is running it once.
        again = await worker.settle_for_terminal_run(run_id)
        assert again is not None
        assert again.status is EvaluationLaunchStatus.COMPLETED
        plan = await MerchantEvaluationLaunchService(session, settings).plan(merchant_id)
        assert plan.pending_launch_id is None

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
        await queue_launch(session, settings, world, request_key="bind-race-request")

        async def taken(*arguments: object, **keywords: object) -> None:
            raise ConflictError("launch_not_queued", "another worker got there first")

        monkeypatch.setattr(EvaluationLaunchWorkerService, "bind_run", taken)

        with pytest.raises(ConflictError):
            await execute_next_launch(
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
            await MerchantEvaluationLaunchService(session, settings).plan(world.merchant_id)
        ).pending_launch_id is not None
