"""The evaluation workspace command line, run for real against PostgreSQL.

Nothing is mocked away. `main` is called with the arguments an operator would type, it opens its
own engine against the test database, and every command reaches the real bootstrap service.

The last test is the one the phase turns on: an operator builds a merchant's setup and then
dispatches their queued evaluation naming only the merchant, with no authored world directory on
disk anywhere. It runs on the deterministic reference buyer and reaches no model provider.
"""

import asyncio
import json
import uuid
from dataclasses import dataclass
from io import StringIO

import pytest
from launch_support import without_providers
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from workspace_support import catalogued, plain, product, source, variant

from agentrank_api.benchmark.evaluation_launch import (
    BenchmarkEvaluationLaunch,
    EvaluationLaunchStatus,
)
from agentrank_api.benchmark.launch import MerchantEvaluationLaunchService
from agentrank_api.benchmark.lifecycle import BenchmarkRunStatus
from agentrank_api.benchmark.models import BenchmarkRun
from agentrank_api.cli import ExitCode, main
from agentrank_api.commerce.models import Merchant
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.config import Settings
from agentrank_api.payments.fake import FakePaymentProvider
from agentrank_api.representation.definitions import MerchantSourceDefinition
from agentrank_api.representation.service import MerchantRepresentationService

pytestmark = pytest.mark.anyio


@dataclass(frozen=True, slots=True)
class Run:
    """What one command invocation produced: its exit code and both streams."""

    code: int
    out: str
    err: str

    def json(self) -> dict[str, object]:
        parsed: dict[str, object] = json.loads(self.out)
        return parsed


@pytest.fixture
def provider() -> FakePaymentProvider:
    return FakePaymentProvider()


async def cli(settings: Settings, provider: FakePaymentProvider, *arguments: str) -> Run:
    """Invoke the command line exactly as a shell would, and capture both streams.

    In a thread, because `main` owns its event loop: it calls `asyncio.run`, which is what a
    process entry point should do and what cannot be done from inside the loop a test is already
    running on.
    """
    out, err = StringIO(), StringIO()
    code = await asyncio.to_thread(
        main, list(arguments), settings=settings, provider=provider, out=out, err=err
    )
    return Run(code=code, out=out.getvalue(), err=err.getvalue())


@pytest.fixture(autouse=True)
async def isolated(session: AsyncSession) -> AsyncSession:
    """Every test here leaves the database empty behind it.

    The commands open their own engine against the same database, so a test that did not take
    the session fixture would leave its rows for the next one to find.
    """
    return session


async def merchant_with(
    session: AsyncSession, slug: str, definition: MerchantSourceDefinition | None = None
) -> Merchant:
    merchant = await MerchantRepository(session).create(slug=slug, name=slug.title())
    await session.commit()
    await MerchantRepresentationService(session).publish_source(definition or catalogued(slug))
    return merchant


async def test_show_reports_what_a_bootstrap_would_build(
    catalog_settings: Settings, provider: FakePaymentProvider, session: AsyncSession
) -> None:
    """Composition and mission count before anything is built, so the scale is never a surprise."""
    await merchant_with(session, "cli-plan-shop")

    result = await cli(
        catalog_settings, provider, "workspace", "show", "--merchant-slug", "cli-plan-shop"
    )

    assert result.code == ExitCode.OK
    assert "planned" in result.out
    assert "CATEGORY_PURCHASE" in result.out
    assert "workspace   none built for this merchant" in result.out


async def test_show_names_the_blocker_for_a_merchant_with_no_source(
    catalog_settings: Settings, provider: FakePaymentProvider, session: AsyncSession
) -> None:
    await MerchantRepository(session).create(slug="cli-bare-shop", name="Bare")
    await session.commit()

    result = await cli(
        catalog_settings,
        provider,
        "workspace",
        "show",
        "--merchant-slug",
        "cli-bare-shop",
        "--json",
    )

    assert result.code == ExitCode.REFUSED
    payload = result.json()
    assert payload["buildable"] is False
    blockers = payload["blockers"]
    assert isinstance(blockers, list)
    assert [entry["code"] for entry in blockers] == ["merchant_source_unavailable"]


async def test_an_unknown_merchant_is_not_found(
    catalog_settings: Settings, provider: FakePaymentProvider, session: AsyncSession
) -> None:
    result = await cli(catalog_settings, provider, "workspace", "show", "--merchant-slug", "nobody")

    assert result.code == ExitCode.NOT_FOUND
    assert "not found" in result.err


async def test_bootstrap_builds_a_world_and_a_workload(
    catalog_settings: Settings, provider: FakePaymentProvider, session: AsyncSession
) -> None:
    await merchant_with(session, "cli-build-shop")

    result = await cli(
        catalog_settings,
        provider,
        "workspace",
        "bootstrap",
        "--merchant-slug",
        "cli-build-shop",
        "--json",
    )

    assert result.code == ExitCode.OK
    payload = result.json()
    assert payload["created"] is True
    assert payload["mission_count"]
    assert str(payload["environment_label"]).startswith("cli-build-shop-workspace-catalog@")
    assert str(payload["suite_label"]).startswith("cli-build-shop-workspace-suite@")


async def test_bootstrap_run_twice_reports_one_workspace(
    catalog_settings: Settings, provider: FakePaymentProvider, session: AsyncSession
) -> None:
    """Safe to repeat, which is what makes it safe to put in a runbook."""
    await merchant_with(session, "cli-repeat-shop")
    arguments = (
        "workspace",
        "bootstrap",
        "--merchant-slug",
        "cli-repeat-shop",
        "--json",
    )

    first = await cli(catalog_settings, provider, *arguments)
    second = await cli(catalog_settings, provider, *arguments)

    assert first.json()["created"] is True
    assert second.json()["created"] is False
    assert first.json()["workspace_id"] == second.json()["workspace_id"]


async def test_a_mission_budget_is_part_of_the_identity(
    catalog_settings: Settings, provider: FakePaymentProvider, session: AsyncSession
) -> None:
    await merchant_with(session, "cli-budget-shop")

    default = await cli(
        catalog_settings,
        provider,
        "workspace",
        "bootstrap",
        "--merchant-slug",
        "cli-budget-shop",
        "--json",
    )
    smaller = await cli(
        catalog_settings,
        provider,
        "workspace",
        "bootstrap",
        "--merchant-slug",
        "cli-budget-shop",
        "--missions",
        "4",
        "--json",
    )

    assert smaller.json()["mission_count"] == 4
    assert default.json()["workspace_id"] != smaller.json()["workspace_id"]
    assert default.json()["configuration_digest"] != smaller.json()["configuration_digest"]


async def test_a_source_that_supports_nothing_is_refused_by_name(
    catalog_settings: Settings, provider: FakePaymentProvider, session: AsyncSession
) -> None:
    await merchant_with(
        session,
        "cli-empty-shop",
        source(product("P1", variant("P1-A", stock=0)), slug="cli-empty-shop"),
    )

    result = await cli(
        catalog_settings,
        provider,
        "workspace",
        "bootstrap",
        "--merchant-slug",
        "cli-empty-shop",
    )

    assert result.code == ExitCode.REFUSED
    assert "no_purchasable_variant" in result.err


async def test_history_lists_every_workspace_newest_first(
    catalog_settings: Settings, provider: FakePaymentProvider, session: AsyncSession
) -> None:
    merchant = await merchant_with(session, "cli-history-shop")
    await cli(
        catalog_settings,
        provider,
        "workspace",
        "bootstrap",
        "--merchant-slug",
        merchant.slug,
        "--json",
    )
    await MerchantRepresentationService(session).publish_source(
        source(*plain(merchant.slug).products, slug=merchant.slug, version=2)
    )
    await cli(
        catalog_settings,
        provider,
        "workspace",
        "bootstrap",
        "--merchant-slug",
        merchant.slug,
        "--json",
    )

    result = await cli(
        catalog_settings,
        provider,
        "workspace",
        "history",
        "--merchant-slug",
        merchant.slug,
        "--json",
    )

    workspaces = result.json()["workspaces"]
    assert isinstance(workspaces, list)
    assert [entry["source_snapshot_label"] for entry in workspaces] == [
        "merchant-source@2",
        "merchant-source@1",
    ]


async def test_an_operator_bootstraps_and_dispatches_without_any_authored_world(
    catalog_settings: Settings, provider: FakePaymentProvider, session: AsyncSession
) -> None:
    """The private beta operator loop, end to end, with no files and no model provider.

    Build the setup, let the merchant queue their first evaluation, and dispatch it naming only
    the merchant. `benchmark dispatch --merchant-slug` reads the world out of the workspace row,
    so nothing here passes a `--world` directory and none exists for this merchant.
    """
    settings = without_providers(catalog_settings)
    merchant = await merchant_with(session, "cli-dispatch-shop")
    built = await cli(
        settings, provider, "workspace", "bootstrap", "--merchant-slug", merchant.slug, "--json"
    )
    assert built.code == ExitCode.OK

    service = MerchantEvaluationLaunchService(session, settings)
    plan = await service.plan(merchant.id)
    assert plan.launchable, [blocker.code for blocker in plan.blockers]
    launch = await service.request(
        merchant.id,
        purpose=plan.purpose,
        representation_id=plan.representation_id,
        request_key="cli-dispatch-request",
        plan_digest=plan.digest,
    )
    launch_id = launch.id

    dispatched = await cli(
        settings, provider, "benchmark", "dispatch", "--merchant-slug", merchant.slug, "--json"
    )

    assert dispatched.code == ExitCode.OK
    assert dispatched.json()["status"] == "COMPLETED"
    run = (
        await session.execute(select(BenchmarkRun).where(BenchmarkRun.merchant_id == merchant.id))
    ).scalar_one()
    assert run.status is BenchmarkRunStatus.COMPLETED
    settled = await session.get(BenchmarkEvaluationLaunch, launch_id)
    assert settled is not None
    await session.refresh(settled)
    assert settled.status is EvaluationLaunchStatus.COMPLETED

    read_back = await cli(
        settings,
        provider,
        "benchmark",
        "show",
        str(run.id),
        "--merchant-slug",
        merchant.slug,
        "--json",
    )
    assert read_back.code == ExitCode.OK
    assert read_back.json()["run_id"] == str(run.id)


async def test_dispatch_refuses_a_merchant_with_no_workspace(
    catalog_settings: Settings, provider: FakePaymentProvider, session: AsyncSession
) -> None:
    """Falling back to whatever `--world` defaults to would point a worker at another merchant."""
    await merchant_with(session, "cli-unbuilt-shop")

    result = await cli(
        catalog_settings,
        provider,
        "benchmark",
        "dispatch",
        "--merchant-slug",
        "cli-unbuilt-shop",
    )

    assert result.code == ExitCode.NOT_FOUND
    assert "merchant_evaluation_workspace" in result.err


async def test_a_mission_budget_outside_the_bound_is_a_usage_error(
    catalog_settings: Settings, provider: FakePaymentProvider, session: AsyncSession
) -> None:
    await merchant_with(session, "cli-bound-shop")

    with pytest.raises(SystemExit) as refused:
        await cli(
            catalog_settings,
            provider,
            "workspace",
            "bootstrap",
            "--merchant-slug",
            "cli-bound-shop",
            "--missions",
            "500",
        )

    assert refused.value.code == ExitCode.USAGE


async def test_no_workspace_command_reads_an_authored_world(
    catalog_settings: Settings, provider: FakePaymentProvider, session: AsyncSession
) -> None:
    """Not one of these commands takes a `--world`, which is the point of the whole phase."""
    await merchant_with(session, "cli-fileless-shop")

    with pytest.raises(SystemExit):
        await cli(
            catalog_settings,
            provider,
            "workspace",
            "bootstrap",
            "--merchant-slug",
            "cli-fileless-shop",
            "--world",
            str(uuid.uuid7()),
        )
