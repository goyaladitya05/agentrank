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
    InitialWorld,
    LaunchWorld,
    build_initial_world,
    build_launch_world,
    queue_launch,
    with_both_providers,
    with_gemini,
    with_openai,
    without_providers,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentrank_api.benchmark.authored import AuthoredWorld
from agentrank_api.benchmark.capacity import ProviderExecutionPermit
from agentrank_api.benchmark.dispatch import (
    FAILURE_NO_PROVIDER,
    UNSERVICEABLE,
    LaunchDispatchError,
    _dispatch_plan,
    _halted,
    _Plan,
    buyer_surface,
    execute_next_launch,
    measured_documents,
)
from agentrank_api.benchmark.environment import BenchmarkEnvironmentService
from agentrank_api.benchmark.evaluation_launch import (
    BenchmarkEvaluationLaunch,
    EvaluationLaunchStatus,
    EvaluationPurpose,
)
from agentrank_api.benchmark.execution import REFERENCE_ISOLATED_KIND, ExecutorIdentity
from agentrank_api.benchmark.launch import (
    CANCELLED_BY_OPERATOR,
    EvaluationLaunchWorkerService,
    MerchantEvaluationLaunchService,
    worker_executor_kinds,
)
from agentrank_api.benchmark.lifecycle import BenchmarkRunStatus, MissionRunStatus
from agentrank_api.benchmark.llm import GEMINI_PROVIDER, OPENAI_PROVIDER
from agentrank_api.benchmark.models import BenchmarkRun
from agentrank_api.benchmark.permits import (
    ExecutionWaitReason,
    ProviderExecutionHaltedError,
    ProviderExecutionService,
)
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


class TestPlanSurvivesTheClaim:
    """What execution reads after the claim's transaction is gone.

    The dispatcher rolls back the claim before the loopback server starts, so the row lock is
    not held across a server boot. That rollback expires every instance loaded inside the claim,
    and the model buyer path is the only one that reads a document afterwards, so it is the only
    path where carrying an ORM row on the plan would fail. It fails a long way from the cause:
    the first attribute read is a lazy load with no transaction open and no greenlet context, and
    the launch is left queued with no run and no failure code.

    Executing this path end to end would call a model provider, so what is asserted is exactly
    the boundary the defect lived on: the plan, the rollback, and then the documents.
    """

    async def _claimed_plan(
        self, session: AsyncSession, world: LaunchWorld | InitialWorld, settings: Settings
    ) -> _Plan:
        environment = await BenchmarkEnvironmentService(session).require_registered(world.fixture)
        launch = await EvaluationLaunchWorkerService(session).claim_next(
            world.merchant_id,
            environment_id=environment.id,
            executor_kinds=worker_executor_kinds(settings),
        )
        assert launch is not None
        return await _dispatch_plan(session, launch, environment=environment, settings=settings)

    async def test_a_re_evaluations_representation_is_readable_after_the_rollback(
        self, catalog_settings: Settings, session: AsyncSession
    ) -> None:
        settings = with_openai(catalog_settings)
        world = await build_launch_world(session, "survive-compiled-shop")
        await queue_launch(session, settings, world, request_key="survive-compiled")
        plan = await self._claimed_plan(session, world, settings)

        await session.rollback()
        representation, source = await measured_documents(session, plan)

        assert source is None
        assert representation is not None
        assert representation.id == world.representation_id
        surface = buyer_surface(representation=representation, source=source)
        assert surface.discovery["kind"] == "AGENT_READY"

    async def test_a_first_evaluations_source_is_readable_after_the_rollback(
        self, catalog_settings: Settings, session: AsyncSession
    ) -> None:
        settings = with_openai(catalog_settings)
        world = await build_initial_world(session, "survive-initial-shop")
        await queue_launch(session, settings, world, request_key="survive-initial")
        plan = await self._claimed_plan(session, world, settings)

        await session.rollback()
        representation, source = await measured_documents(session, plan)

        assert representation is None
        assert source is not None
        assert source.id == world.source_snapshot_id
        surface = buyer_surface(representation=representation, source=source)
        assert surface.discovery == {"kind": "STOREFRONT"}
        assert surface.merchant_information == raw_projection(source)

    async def test_a_reference_buyer_reads_no_document_at_all(
        self, catalog_settings: Settings, session: AsyncSession
    ) -> None:
        settings = without_providers(catalog_settings)
        world = await build_initial_world(session, "survive-reference-shop")
        await queue_launch(session, settings, world, request_key="survive-reference")
        plan = await self._claimed_plan(session, world, settings)

        await session.rollback()

        assert await measured_documents(session, plan) == (None, None)


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


class TestWorkerCapability:
    """What a worker may claim, when its configuration cannot run what a merchant asked for.

    A settled launch is terminal and cannot be deleted, so the question is not academic: a
    worker that claims a launch it can only refuse destroys a merchant's request that a
    differently configured worker could have served.
    """

    async def test_capability_is_derived_from_configuration_and_nothing_else(
        self, catalog_settings: Settings
    ) -> None:
        """The reference executor needs no credential, and each provider adds exactly its own."""
        assert worker_executor_kinds(without_providers(catalog_settings)) == {
            REFERENCE_ISOLATED_KIND
        }
        assert worker_executor_kinds(with_openai(catalog_settings)) == {
            REFERENCE_ISOLATED_KIND,
            "llm-openai",
        }
        assert worker_executor_kinds(with_gemini(catalog_settings)) == {
            REFERENCE_ISOLATED_KIND,
            "llm-gemini",
        }
        assert worker_executor_kinds(with_both_providers(catalog_settings)) == {
            REFERENCE_ISOLATED_KIND,
            "llm-openai",
            "llm-gemini",
        }

    async def test_a_worker_without_the_frozen_provider_leaves_the_launch_queued(
        self,
        catalog_settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The launch survives a worker that cannot run it, and the worker says so.

        No substitution either: nothing about the frozen identity moves, so the launch a
        merchant was shown as an AI run is still an AI run when a capable worker takes it.
        """
        world = await build_launch_world(session, "unconfigured-shop")
        launch_id = await queue_launch(
            session, with_openai(catalog_settings), world, request_key="model-request"
        )
        frozen = await reload(session, launch_id)
        kind, digest = frozen.executor_kind, frozen.buyer_configuration_digest

        outcome = await execute_next_launch(
            session,
            factory,
            world=world.authored,
            provider=FakePaymentProvider(),
            settings=without_providers(catalog_settings),
        )

        assert outcome is not None
        assert outcome.status == UNSERVICEABLE
        assert outcome.failure_code is None
        assert outcome.run_id is None
        assert outcome.detail is not None
        assert "llm-openai" in outcome.detail

        untouched = await reload(session, launch_id)
        assert untouched.status is EvaluationLaunchStatus.QUEUED
        assert untouched.run_id is None
        assert untouched.settled_at is None
        assert untouched.executor_kind == kind
        assert untouched.buyer_configuration_digest == digest
        runs = (
            await session.execute(
                select(BenchmarkRun).where(BenchmarkRun.merchant_id == world.merchant_id)
            )
        ).scalars()
        assert list(runs) == []

    async def test_a_worker_holding_the_frozen_provider_claims_what_the_other_left(
        self,
        catalog_settings: Settings,
        session: AsyncSession,
    ) -> None:
        """The claim predicate itself, asserted without spending a model call.

        Executing this launch end to end would call a provider, so what is checked is the one
        thing that decides whether a capable worker ever gets it: the claim.
        """
        configured = with_openai(catalog_settings)
        world = await build_launch_world(session, "capable-shop")
        launch_id = await queue_launch(session, configured, world, request_key="capable-request")
        registered = await BenchmarkEnvironmentService(session).require_registered(world.fixture)
        # A plain value: the rollbacks below expire every loaded instance, and reading an
        # attribute off one afterwards is database IO with no greenlet for it.
        environment_id = registered.id
        worker = EvaluationLaunchWorkerService(session)

        incapable = worker_executor_kinds(without_providers(catalog_settings))
        assert (
            await worker.claim_next(
                world.merchant_id, environment_id=environment_id, executor_kinds=incapable
            )
            is None
        )
        waiting = await worker.unclaimable_next(
            world.merchant_id, environment_id=environment_id, executor_kinds=incapable
        )
        assert waiting is not None
        assert waiting.id == launch_id
        await session.rollback()

        capable = worker_executor_kinds(configured)
        claimed = await worker.claim_next(
            world.merchant_id, environment_id=environment_id, executor_kinds=capable
        )
        assert claimed is not None
        assert claimed.id == launch_id
        assert (
            await worker.unclaimable_next(
                world.merchant_id, environment_id=environment_id, executor_kinds=capable
            )
            is None
        )
        await session.rollback()

    async def test_a_capable_worker_is_not_told_work_is_unserviceable(
        self,
        catalog_settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Nothing queued stays the ordinary None, so the two answers cannot be confused."""
        settings = without_providers(catalog_settings)
        world = await build_launch_world(session, "quiet-capable-shop")
        await queue_launch(session, settings, world, request_key="quiet-request")
        served = await execute_next_launch(
            session,
            factory,
            world=world.authored,
            provider=FakePaymentProvider(),
            settings=settings,
        )
        assert served is not None
        assert served.status == "COMPLETED"

        assert (
            await execute_next_launch(
                session,
                factory,
                world=world.authored,
                provider=FakePaymentProvider(),
                settings=settings,
            )
            is None
        )


class TestCancellingWhatNobodyCanRun:
    """The remedy the capability-aware claim needs, and why it needs one.

    Leaving an unrunnable launch queued is right: a worker that settled it would destroy a
    request a capable worker could serve. What that trades into is a launch nobody is configured
    for sitting queued forever, holding the merchant's one pending slot and blocking every future
    evaluation they could ask for. Without an operator remedy that is worse than what it replaced.
    """

    async def test_a_queued_launch_can_be_cancelled_and_frees_the_merchants_slot(
        self,
        catalog_settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        world = await build_launch_world(session, "stuck-shop")
        launch_id = await queue_launch(
            session, with_openai(catalog_settings), world, request_key="stuck-request"
        )
        settings = without_providers(catalog_settings)
        # Nothing here can run it, which is the state this exists for.
        outcome = await execute_next_launch(
            session,
            factory,
            world=world.authored,
            provider=FakePaymentProvider(),
            settings=settings,
        )
        assert outcome is not None and outcome.status == UNSERVICEABLE
        # And while it is queued the merchant cannot ask for anything else.
        blocked = await MerchantEvaluationLaunchService(session, settings).plan(world.merchant_id)
        assert blocked.launchable is False

        cancelled = await EvaluationLaunchWorkerService(session).cancel_queued(launch_id)

        assert cancelled.status is EvaluationLaunchStatus.FAILED
        assert cancelled.failure_code == CANCELLED_BY_OPERATOR
        assert cancelled.run_id is None
        assert cancelled.settled_at is not None
        settled = await reload(session, launch_id)
        assert settled.status is EvaluationLaunchStatus.FAILED

        # The slot is free and the merchant can ask again.
        recovered = await MerchantEvaluationLaunchService(session, settings).plan(world.merchant_id)
        assert recovered.launchable is True
        runs = (
            await session.execute(
                select(BenchmarkRun).where(BenchmarkRun.merchant_id == world.merchant_id)
            )
        ).scalars()
        assert list(runs) == [], "cancelling never invents or destroys a run"

    async def test_an_executing_launch_is_refused_rather_than_closed_from_here(
        self,
        catalog_settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """An executing launch has a run that may have moved money and consumed stock."""
        settings = without_providers(catalog_settings)
        world = await build_launch_world(session, "executing-shop")
        launch_id = await queue_launch(session, settings, world, request_key="executing-request")
        # Bound without claiming first: the claim only takes a row lock this test would then have
        # to release, and what is under test is the state a bound launch is in.
        run = await BenchmarkRunService(session).start_run(
            suite_key=world.suite_key,
            suite_version=world.suite_version,
            merchant_slug=world.merchant_slug,
            environment=world.environment,
        )
        worker = EvaluationLaunchWorkerService(session)
        await worker.bind_run(launch_id, run.id)

        with pytest.raises(ConflictError) as refused:
            await worker.cancel_queued(launch_id)

        assert refused.value.reason == "launch_not_queued"
        still = await reload(session, launch_id)
        assert still.status is EvaluationLaunchStatus.EXECUTING

    async def test_a_settled_launch_cannot_be_cancelled_twice(
        self,
        catalog_settings: Settings,
        session: AsyncSession,
    ) -> None:
        settings = without_providers(catalog_settings)
        world = await build_launch_world(session, "twice-shop")
        launch_id = await queue_launch(session, settings, world, request_key="twice-request")
        worker = EvaluationLaunchWorkerService(session)
        await worker.cancel_queued(launch_id)

        with pytest.raises(ConflictError):
            await worker.cancel_queued(launch_id)


class TestRefusals:
    async def test_the_provider_credential_backstop_still_refuses_by_name(
        self, catalog_settings: Settings, session: AsyncSession
    ) -> None:
        """The check under the claim predicate, exercised where the claim cannot reach it.

        The claim is filtered by executor kind, so a worker never reaches this in a correct
        build. It stays because a claim predicate and a capability derivation that disagreed
        would otherwise run a launch with no credential for its frozen provider, and the
        direction to fail in is closed.
        """
        world = await build_launch_world(session, "backstop-shop")
        launch_id = await queue_launch(
            session, with_openai(catalog_settings), world, request_key="backstop-request"
        )
        environment = await BenchmarkEnvironmentService(session).require_registered(world.fixture)
        launch = await reload(session, launch_id)

        with pytest.raises(LaunchDispatchError) as refused:
            await _dispatch_plan(
                session,
                launch,
                environment=environment,
                settings=without_providers(catalog_settings),
            )
        assert refused.value.failure_code == FAILURE_NO_PROVIDER

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

        The claim's row lock is released before execution starts, so a launch can be settled
        between the claim and the bind: another worker reaching `bind_run` first, an operator
        cancelling it, or its own merchant withdrawing it from the console. The loser must not
        leave a run executing against a merchant with nothing to explain it, so it closes the run
        it just created.

        It reports rather than raises. Nothing failed: no mission ran, and the work this worker
        claimed stopped being work. An unhandled conflict here would make an ordinary merchant
        click exit an operator's dispatch command non zero.
        """
        settings = without_providers(catalog_settings)
        world = await build_launch_world(session, "bind-race-shop")
        await queue_launch(session, settings, world, request_key="bind-race-request")

        async def taken(*arguments: object, **keywords: object) -> None:
            raise ConflictError("launch_not_queued", "another worker got there first")

        monkeypatch.setattr(EvaluationLaunchWorkerService, "bind_run", taken)

        outcome = await execute_next_launch(
            session,
            factory,
            world=world.authored,
            provider=FakePaymentProvider(),
            settings=settings,
        )

        monkeypatch.undo()
        assert outcome is not None
        assert outcome.status == "SETTLED"
        assert outcome.failure_code is None
        assert outcome.run_id is None
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


class TestProviderExecutionGovernance:
    """A dispatch that stops before spending, and says whose decision that was.

    Nothing here executes a model mission. That is the point: every one of these is about a
    decision AgentRank makes before a provider could have been reached, so the assertion is on
    what the launch row says afterwards and on the fact that no run was created at all.
    """

    async def test_a_paused_provider_leaves_the_launch_queued_and_says_so(
        self,
        catalog_settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Pausing destroys nothing. The work waits and the reason is AgentRank's own."""
        configured = with_openai(catalog_settings)
        world = await build_launch_world(session, "paused-dispatch-shop")
        launch_id = await queue_launch(session, configured, world, request_key="paused-request")
        await ProviderExecutionService(session).set_policy(OPENAI_PROVIDER, enabled=False)

        outcome = await execute_next_launch(
            session,
            factory,
            world=world.authored,
            provider=FakePaymentProvider(),
            settings=configured,
        )

        assert outcome is not None
        assert outcome.status == "QUEUED"
        assert outcome.wait_reason is ExecutionWaitReason.PROVIDER_PAUSED
        assert outcome.failure_code is None
        waiting = await reload(session, launch_id)
        assert waiting.status is EvaluationLaunchStatus.QUEUED
        runs = (
            await session.execute(
                select(BenchmarkRun).where(BenchmarkRun.merchant_id == world.merchant_id)
            )
        ).scalars()
        assert list(runs) == []

    async def test_a_provider_with_no_free_slot_leaves_the_launch_queued(
        self,
        catalog_settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Ordinary contention rather than a failure, and the launch keeps its place.

        The other evaluation here is a real EXECUTING launch for a different merchant, because
        that is what the capacity count actually reads.
        """
        configured = with_openai(catalog_settings)
        busy = await build_launch_world(session, "busy-shop")
        busy_launch = await queue_launch(session, configured, busy, request_key="busy-request")
        frozen = await reload(session, busy_launch)
        started = await BenchmarkRunService(session).start_suite(
            suite_key=busy.suite_key,
            suite_version=busy.suite_version,
            fixture=busy.fixture,
            executor=ExecutorIdentity(
                kind=frozen.executor_kind,
                version=1,
                revision=frozen.buyer_configuration_digest,
            ),
            agent_configuration=frozen.buyer_configuration,
        )
        await EvaluationLaunchWorkerService(session).bind_run(busy_launch, started.id)

        waiting_world = await build_launch_world(session, "waiting-shop")
        waiting_launch = await queue_launch(
            session, configured, waiting_world, request_key="waiting-request"
        )

        outcome = await execute_next_launch(
            session,
            factory,
            world=waiting_world.authored,
            provider=FakePaymentProvider(),
            settings=configured,
        )

        assert outcome is not None
        assert outcome.status == "QUEUED"
        assert outcome.wait_reason is ExecutionWaitReason.PROVIDER_CAPACITY_OCCUPIED
        still_queued = await reload(session, waiting_launch)
        assert still_queued.status is EvaluationLaunchStatus.QUEUED

    async def test_a_reference_buyer_never_waits_on_a_paused_model_provider(
        self,
        catalog_settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The deterministic buyer calls no provider, so a provider's policy is not about it."""
        world = await build_launch_world(session, "reference-shop")
        await queue_launch(
            session,
            without_providers(catalog_settings),
            world,
            request_key="reference-request",
        )
        await ProviderExecutionService(session).set_policy(OPENAI_PROVIDER, enabled=False)
        await ProviderExecutionService(session).set_policy(GEMINI_PROVIDER, enabled=False)

        outcome = await execute_next_launch(
            session,
            factory,
            world=world.authored,
            provider=FakePaymentProvider(),
            settings=without_providers(catalog_settings),
        )

        assert outcome is not None
        assert outcome.status == "COMPLETED"
        assert outcome.wait_reason is None

    async def test_a_reference_launch_reserves_no_provider_requests_at_all(
        self,
        catalog_settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A permit for a buyer that calls no provider would be a row describing work that never
        touched one, and an execution ledger that described it would be wrong about what ran."""
        world = await build_launch_world(session, "unspent-shop")
        await queue_launch(
            session, without_providers(catalog_settings), world, request_key="unspent-request"
        )

        await execute_next_launch(
            session,
            factory,
            world=world.authored,
            provider=FakePaymentProvider(),
            settings=without_providers(catalog_settings),
        )

        permits = (
            await session.execute(
                select(ProviderExecutionPermit).where(
                    ProviderExecutionPermit.merchant_id == world.merchant_id
                )
            )
        ).scalars()
        assert list(permits) == []


class TestASettledGovernanceHalt:
    """What a launch looks like after a suite stopped part way through for want of allowance."""

    async def test_a_launch_stopped_mid_suite_settles_failed_against_its_aborted_run(
        self, catalog_settings: Settings, session: AsyncSession
    ) -> None:
        """The database only accepts this settlement when the run already agrees, which is the
        whole reason the run is aborted before the launch is settled rather than after.

        Asserted on the state a halt actually produces rather than by spending a real allowance:
        an executing launch, its run closed as incomplete, and the settlement that follows.
        """
        configured = with_openai(catalog_settings)
        world = await build_launch_world(session, "stopped-launch-shop")
        launch_id = await queue_launch(session, configured, world, request_key="stopped-request")
        frozen = await reload(session, launch_id)
        runs = BenchmarkRunService(session)
        started = await runs.start_suite(
            suite_key=world.suite_key,
            suite_version=world.suite_version,
            fixture=world.fixture,
            executor=ExecutorIdentity(
                kind=frozen.executor_kind,
                version=1,
                revision=frozen.buyer_configuration_digest,
            ),
            agent_configuration=frozen.buyer_configuration,
        )
        worker = EvaluationLaunchWorkerService(session)
        await worker.bind_run(launch_id, started.id)
        await runs.abort_run(started.id, merchant_id=world.merchant_id)

        outcome = await _halted(
            session,
            worker,
            launch_id=launch_id,
            halted=ProviderExecutionHaltedError(ExecutionWaitReason.LAUNCH_BUDGET_EXHAUSTED),
        )

        assert outcome.status == "FAILED"
        assert outcome.failure_code == "provider_budget_exhausted"
        assert outcome.wait_reason is ExecutionWaitReason.LAUNCH_BUDGET_EXHAUSTED
        settled = await reload(session, launch_id)
        assert settled.status is EvaluationLaunchStatus.FAILED
        assert settled.failure_code == "provider_budget_exhausted"
        assert settled.run_id == started.id

    async def test_a_launch_stopped_before_it_was_bound_stays_queued_and_settles_nothing(
        self, catalog_settings: Settings, session: AsyncSession
    ) -> None:
        """Capacity frees, a worker comes back, and the merchant's evaluation still runs."""
        configured = with_openai(catalog_settings)
        world = await build_launch_world(session, "unbound-shop")
        launch_id = await queue_launch(session, configured, world, request_key="unbound-request")

        outcome = await _halted(
            session,
            EvaluationLaunchWorkerService(session),
            launch_id=launch_id,
            halted=ProviderExecutionHaltedError(ExecutionWaitReason.PROVIDER_CAPACITY_OCCUPIED),
        )

        assert outcome.status == "QUEUED"
        assert outcome.failure_code is None
        assert (await reload(session, launch_id)).status is EvaluationLaunchStatus.QUEUED


class TestProviderBudgetStopsARun:
    """What happens to a suite when AgentRank declines to pay for the next mission.

    Driven through the run service with an executor whose admission refuses, because that is
    exactly the shape a real exhaustion takes and because producing a real one would mean
    spending until a provider allowance ran out.
    """

    async def test_a_refused_mission_stops_the_suite_before_it_is_recorded_as_started(
        self, session: AsyncSession
    ) -> None:
        """A mission nobody will pay for must never be left RUNNING and never be marked failed.

        Admission happens before the mission run row exists, so the suite stops with nothing
        recorded about a mission that was never attempted, and the run is closed as the
        incomplete thing it is.
        """
        world = await build_initial_world(session, "halted-suite-shop")
        runs = BenchmarkRunService(session)
        started = await runs.start_suite(
            suite_key=world.suite_key,
            suite_version=world.suite_version,
            fixture=world.fixture,
            executor=ExecutorIdentity(kind=REFERENCE_ISOLATED_KIND, version=1),
        )

        with pytest.raises(ProviderExecutionHaltedError) as halted:
            await runs.execute_started_suite(
                started.id,
                _HaltingExecutor(),
                merchant_id=world.merchant_id,
                fixture=world.fixture,
            )

        assert halted.value.reason is ExecutionWaitReason.LAUNCH_BUDGET_EXHAUSTED
        assert halted.value.failure_code == "provider_budget_exhausted"
        aborted = await runs.abort_run(started.id, merchant_id=world.merchant_id)
        assert aborted.status is BenchmarkRunStatus.ABORTED
        # Every mission still PENDING, which is the honest record of a suite that stopped before
        # its first one: nothing was attempted, so nothing is marked failed and nothing is left
        # RUNNING for an operator to resolve.
        assert {result.status for result in aborted.mission_runs} == {MissionRunStatus.PENDING}


class _HaltingExecutor:
    """An executor whose provider requests nobody will pay for.

    It has an `admit` and deliberately nothing else that works: if the runner ever called it
    after admission refused, this would fail loudly rather than quietly measure something.
    """

    identity = ExecutorIdentity(kind=REFERENCE_ISOLATED_KIND, version=1)

    async def admit(self, mission_key: str) -> None:
        del mission_key
        raise ProviderExecutionHaltedError(ExecutionWaitReason.LAUNCH_BUDGET_EXHAUSTED)

    async def __call__(self, brief: object, *, merchant_id: uuid.UUID) -> ExecutorReport:
        raise AssertionError("a mission nothing would pay for must never be executed")
