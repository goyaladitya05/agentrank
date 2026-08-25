"""A merchant with nothing but source evidence reaches a finished first evaluation.

This is the phase's exit criterion executed rather than described. A merchant is created, they
publish one source snapshot, their evaluation setup is built from it, and the existing Phase 4F
launch path carries a real benchmark run to COMPLETED against the world that setup generated.

Nothing bespoke exists for this merchant anywhere. There is no authored `benchmarks/<world>`
directory, no hand written catalog, no hand written suite and no row inserted by a fixture. The
world the dispatcher prepares is read out of the workspace row, and the workload it executes is
the generated suite.

No model provider is configured and none is reached. The buyer is the deterministic reference
executor, which is what makes this runnable in CI with no credential and no quota, and its result
is evidence that the benchmark path works rather than evidence about an autonomous agent.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from launch_support import without_providers
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from workspace_support import awkward, catalogued, plain, source

from agentrank_api.benchmark.dispatch import execute_next_launch
from agentrank_api.benchmark.evaluation_launch import (
    BenchmarkEvaluationLaunch,
    EvaluationLaunchStatus,
    EvaluationPurpose,
)
from agentrank_api.benchmark.launch import MerchantEvaluationLaunchService
from agentrank_api.benchmark.lifecycle import BenchmarkRunStatus
from agentrank_api.benchmark.models import BenchmarkRun
from agentrank_api.benchmark.runner import BenchmarkRunService
from agentrank_api.commerce.models import Variant
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.config import Settings
from agentrank_api.payments.fake import FakePaymentProvider
from agentrank_api.representation.definitions import MerchantSourceDefinition
from agentrank_api.representation.service import MerchantRepresentationService
from agentrank_api.workspace.service import MerchantEvaluationWorkspaceService
from agentrank_api.workspace.world import WorkspaceWorld, workspace_world

pytestmark = pytest.mark.anyio


@dataclass(frozen=True, slots=True)
class Bootstrapped:
    """One built setup, as plain values beside the world it generated.

    Identifiers rather than rows, for the reason the dispatcher's own plan states: executing a
    launch rolls the session back before anything long starts, which expires every instance
    loaded before it, and reading an attribute off an expired row afterwards is a lazy load with
    no transaction open and no greenlet to run it in.
    """

    merchant_id: uuid.UUID
    merchant_slug: str
    source_snapshot_id: uuid.UUID
    workspace_id: uuid.UUID
    environment_id: uuid.UUID
    suite_id: uuid.UUID
    world: WorkspaceWorld
    catalog_fixture: dict[str, Any]


async def bootstrapped(
    session: AsyncSession,
    slug: str,
    builder: Callable[[str], MerchantSourceDefinition] = catalogued,
) -> Bootstrapped:
    """A merchant whose entire history is one source snapshot and one built setup."""
    merchant = await MerchantRepository(session).create(slug=slug, name=slug.title())
    await session.commit()
    snapshot = await MerchantRepresentationService(session).publish_source(builder(slug))
    workspace = (
        await MerchantEvaluationWorkspaceService(session).bootstrap(
            merchant.id, source_snapshot_id=snapshot.id
        )
    ).workspace
    return Bootstrapped(
        merchant_id=merchant.id,
        merchant_slug=merchant.slug,
        source_snapshot_id=snapshot.id,
        workspace_id=workspace.id,
        environment_id=workspace.environment_id,
        suite_id=workspace.suite_id,
        world=workspace_world(workspace),
        catalog_fixture=dict(workspace.catalog_fixture),
    )


async def queue(session: AsyncSession, settings: Settings, merchant_id: uuid.UUID) -> uuid.UUID:
    """One admitted launch, through the merchant-facing service the console calls."""
    service = MerchantEvaluationLaunchService(session, settings)
    plan = await service.plan(merchant_id)
    assert plan.launchable, [blocker.code for blocker in plan.blockers]
    assert plan.purpose is EvaluationPurpose.INITIAL
    launch = await service.request(
        merchant_id,
        purpose=plan.purpose,
        representation_id=plan.representation_id,
        request_key="workspace-first-evaluation",
        plan_digest=plan.digest,
    )
    return launch.id


async def test_a_bootstrapped_merchant_reaches_a_completed_first_evaluation(
    catalog_settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Source evidence in, a finished benchmark result out, with no operator files anywhere."""
    settings = without_providers(catalog_settings)
    built = await bootstrapped(session, "first-eval-shop")
    launch_id = await queue(session, settings, built.merchant_id)

    outcome = await execute_next_launch(
        session,
        factory,
        world=built.world,
        provider=FakePaymentProvider(),
        settings=settings,
    )

    assert outcome is not None
    assert outcome.status == "COMPLETED"
    assert outcome.run_id is not None
    run = await session.get(BenchmarkRun, outcome.run_id)
    assert run is not None
    assert run.status is BenchmarkRunStatus.COMPLETED
    assert run.merchant_id == built.merchant_id
    assert run.environment_id == built.environment_id
    assert run.suite_id == built.suite_id
    launch = await session.get(BenchmarkEvaluationLaunch, launch_id)
    assert launch is not None
    await session.refresh(launch)
    assert launch.status is EvaluationLaunchStatus.COMPLETED
    assert launch.source_snapshot_id == built.source_snapshot_id


@pytest.mark.parametrize("builder", [catalogued, plain, awkward])
async def test_the_commerce_runtime_agrees_with_every_generated_oracle(
    catalog_settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    builder: Callable[[str], MerchantSourceDefinition],
) -> None:
    """The independent confirmation that a generated answer key is right.

    Two separate things could be wrong with a generated suite and neither would be visible from
    the generator alone. A mission the oracle calls purchasable could be one the commerce runtime
    refuses, and a mission it calls impossible could be one a buyer completes. Both are checked
    here by the runtime rather than by the code that wrote the oracle.

    Every purchasable mission is carried through a real mandate, quote, stock hold and payment by
    the deterministic executor, so a wrong `PURCHASE_AVAILABLE` would fail at the semantic
    authorization gate. Every control mission is declined. And `oracle_disagreements` is the
    run's own recomputation of ground truth against the catalog it actually executed against,
    which is the check that catches an answer key that was right when it was written and is not
    right now.
    """
    settings = without_providers(catalog_settings)
    built = await bootstrapped(session, "oracle-eval-shop", builder)
    await queue(session, settings, built.merchant_id)

    outcome = await execute_next_launch(
        session,
        factory,
        world=built.world,
        provider=FakePaymentProvider(),
        settings=settings,
    )

    assert outcome is not None and outcome.run_id is not None
    metrics = await BenchmarkRunService(session).metrics(
        outcome.run_id, merchant_id=built.merchant_id
    )
    assert metrics.oracle_disagreements == 0
    assert metrics.oracle_unchecked == 0
    # Every mission the catalog said was purchasable was bought, and every one it said was not
    # was declined. A generated suite nothing could complete would report the same run status.
    assert metrics.missions_succeeded > 0
    assert metrics.missions_abstained > 0
    assert metrics.task_completion_rate == 1.0
    assert metrics.correct_abstention_rate == 1.0
    assert metrics.missions_failed == 0
    assert metrics.missions_errored == 0
    assert metrics.unsafe_attempts == 0
    assert metrics.unsafe_completions == 0


async def test_a_first_evaluation_of_a_generated_world_stays_raw(
    catalog_settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """An initial evaluation pins no representation and is compared with nothing.

    A generated world changes what a run is executed against and nothing about what a first
    evaluation means: no Commerce IR is constructed, no compiler is required and there is no
    prior run to be read against.
    """
    settings = without_providers(catalog_settings)
    built = await bootstrapped(session, "raw-eval-shop")
    launch_id = await queue(session, settings, built.merchant_id)

    outcome = await execute_next_launch(
        session,
        factory,
        world=built.world,
        provider=FakePaymentProvider(),
        settings=settings,
    )

    assert outcome is not None and outcome.run_id is not None
    run = await session.get(BenchmarkRun, outcome.run_id)
    assert run is not None
    assert run.representation_id is None
    launch = await session.get(BenchmarkEvaluationLaunch, launch_id)
    assert launch is not None
    await session.refresh(launch)
    assert launch.purpose is EvaluationPurpose.INITIAL
    assert launch.representation_id is None
    assert launch.compiler_run_id is None
    assert launch.baseline_run_id is None


async def test_the_run_is_executed_against_the_generated_catalog(
    catalog_settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Preparation materializes the world the workspace describes, and nothing else.

    Before the run this merchant has no commerce row at all, because building a setup writes
    none. Afterwards the shelf holds exactly the SKUs the merchant's own source snapshot named.
    """
    settings = without_providers(catalog_settings)
    built = await bootstrapped(session, "prepared-eval-shop")
    before = await session.scalar(
        select(func.count()).select_from(Variant).where(Variant.merchant_id == built.merchant_id)
    )
    assert before == 0

    await queue(session, settings, built.merchant_id)
    await execute_next_launch(
        session,
        factory,
        world=built.world,
        provider=FakePaymentProvider(),
        settings=settings,
    )

    stocked = set(
        (
            await session.execute(
                select(Variant.sku).where(Variant.merchant_id == built.merchant_id)
            )
        ).scalars()
    )
    expected = {
        variant["sku"]
        for product in built.catalog_fixture["products"]
        for variant in product["variants"]
    }
    assert stocked == expected


async def test_a_newer_workspace_leaves_the_earlier_run_pointed_at_what_it_measured(
    catalog_settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Refreshing source and building a second setup rewrites no history.

    The finished run still names the world and the workload it executed, and both of those rows
    are immutable, so a report read after the merchant moved on still means what it meant.
    """
    settings = without_providers(catalog_settings)
    first = await bootstrapped(session, "history-eval-shop")
    await queue(session, settings, first.merchant_id)
    outcome = await execute_next_launch(
        session,
        factory,
        world=first.world,
        provider=FakePaymentProvider(),
        settings=settings,
    )
    assert outcome is not None and outcome.run_id is not None

    newer = await MerchantRepresentationService(session).publish_source(
        source(*plain(first.merchant_slug).products, slug=first.merchant_slug, version=2)
    )
    second = (
        await MerchantEvaluationWorkspaceService(session).bootstrap(
            first.merchant_id, source_snapshot_id=newer.id
        )
    ).workspace

    run = await session.get(BenchmarkRun, outcome.run_id)
    assert run is not None
    await session.refresh(run)
    assert run.environment_id == first.environment_id
    assert run.suite_id == first.suite_id
    assert second.environment_id != first.environment_id
    assert second.suite_id != first.suite_id


async def test_no_model_provider_is_reached(
    catalog_settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The whole workflow runs on the deterministic reference buyer.

    Stated as an assertion rather than left to configuration, because "this phase needs no model
    quota" is a claim about the code rather than about whichever key happens to be in a
    developer's environment.
    """
    settings = without_providers(catalog_settings)
    assert settings.openai is None
    assert settings.gemini is None
    built = await bootstrapped(session, "no-provider-shop")
    plan = await MerchantEvaluationLaunchService(session, settings).plan(built.merchant_id)

    assert plan.provider is None
    assert plan.requested_model is None
    assert plan.buyer_profile.value == "REFERENCE_BUYER"

    await queue(session, settings, built.merchant_id)
    outcome = await execute_next_launch(
        session,
        factory,
        world=built.world,
        provider=FakePaymentProvider(),
        settings=settings,
    )
    assert outcome is not None
    assert outcome.status == "COMPLETED"
