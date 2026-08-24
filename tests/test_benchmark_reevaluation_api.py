"""The merchant re-evaluation command over HTTP, and the identity it freezes.

Every test here talks to the real application with a real merchant credential, because what is
being asserted is exactly the boundary: who may launch, what a foreign identifier answers, what
a repeated request does, and what the server resolves rather than accepting from a browser.

Nothing here executes a benchmark. Admission writes a queued launch and answers, which is what
lets a browser request stay an ordinary short request.
"""

import uuid

import pytest
from conftest import CredentialIssuer, bearer
from fastapi.testclient import TestClient
from reevaluation_support import build_launch_world, with_openai, without_providers, world_source
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agentrank_api.benchmark.launch import (
    MerchantReevaluationService,
    ReevaluationWorkerService,
)
from agentrank_api.benchmark.llm import OPENAI_PROVIDER
from agentrank_api.benchmark.reevaluation import BenchmarkReevaluation
from agentrank_api.benchmark.runner import BenchmarkRunService
from agentrank_api.compiler.service import MerchantCompilerService
from agentrank_api.config import Settings
from agentrank_api.main import create_app
from agentrank_api.payments.fake import FakePaymentProvider
from agentrank_api.representation.service import MerchantRepresentationService

pytestmark = pytest.mark.anyio

PREFLIGHT = "/api/v1/benchmark/re-evaluations/preflight"
LAUNCH = "/api/v1/benchmark/re-evaluations"


def client(settings: Settings, sessions: async_sessionmaker[AsyncSession]) -> TestClient:
    """The built application with this suite's session factory on its state.

    Provider credentials are cleared unless a test says otherwise. A developer machine with a
    key in `.env` and a CI runner without one would otherwise resolve different buyers, and a
    test that asserts whatever the environment happens to hold asserts nothing.
    """
    app = create_app(without_providers(settings), payment_provider=FakePaymentProvider())
    app.state.session_factory = sessions
    return TestClient(app)


def client_with_model_provider(
    settings: Settings, sessions: async_sessionmaker[AsyncSession]
) -> TestClient:
    """The same application, built as a deployment that has a model provider credential."""
    app = create_app(with_openai(settings), payment_provider=FakePaymentProvider())
    app.state.session_factory = sessions
    return TestClient(app)


class TestAuthorization:
    async def test_every_route_requires_a_credential(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        world = await build_launch_world(session, "anon-shop")
        http = client(settings, factory)

        assert http.get(PREFLIGHT).status_code == 401
        assert http.get(LAUNCH).status_code == 401
        assert (
            http.post(
                LAUNCH,
                json={
                    "representation_id": str(world.representation.id),
                    "request_key": "anonymous-key",
                },
            ).status_code
            == 401
        )

    async def test_another_merchants_launch_is_indistinguishable_from_nothing(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        mine = await build_launch_world(session, "mine-api-shop")
        theirs = await build_launch_world(session, "theirs-api-shop")
        theirs_launch = await MerchantReevaluationService(
            session, without_providers(settings)
        ).request(
            theirs.merchant_id,
            representation_id=theirs.representation.id,
            request_key="their-request",
        )
        token = await issue_credential(mine.merchant_id)
        http = client(settings, factory)

        response = http.get(f"{LAUNCH}/{theirs_launch.id}", headers=bearer(token))
        assert response.status_code == 404
        unknown = http.get(f"{LAUNCH}/{uuid.uuid7()}", headers=bearer(token))
        assert unknown.status_code == 404
        assert response.json()["error"] == unknown.json()["error"]

    async def test_the_browser_cannot_name_a_merchant_or_a_configuration(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        mine = await build_launch_world(session, "narrow-shop")
        theirs = await build_launch_world(session, "wide-shop")
        token = await issue_credential(mine.merchant_id)
        http = client(settings, factory)

        refused = http.post(
            LAUNCH,
            headers=bearer(token),
            json={
                "representation_id": str(mine.representation.id),
                "request_key": "widened-key",
                "merchant_id": str(theirs.merchant_id),
                "suite_id": str(theirs.suite.id),
            },
        )
        assert refused.status_code == 422

    async def test_another_merchants_representation_is_not_launchable(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        mine = await build_launch_world(session, "scoped-shop")
        theirs = await build_launch_world(session, "foreign-shop")
        token = await issue_credential(mine.merchant_id)
        http = client(settings, factory)

        refused = http.post(
            LAUNCH,
            headers=bearer(token),
            json={
                "representation_id": str(theirs.representation.id),
                "request_key": "cross-tenant-key",
            },
        )
        assert refused.status_code == 409
        assert refused.json()["error"] == "representation_superseded"
        assert (
            await session.scalar(
                BenchmarkReevaluation.__table__.select().with_only_columns(BenchmarkReevaluation.id)
            )
        ) is None


class TestPreflight:
    async def test_preflight_states_what_would_be_evaluated(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        world = await build_launch_world(session, "preflight-shop")
        token = await issue_credential(world.merchant_id)
        http = client(settings, factory)

        body = http.get(PREFLIGHT, headers=bearer(token)).json()

        assert body["launchable"] is True
        assert body["blockers"] == []
        assert body["representation_id"] == str(world.representation.id)
        assert body["compiler_run_id"] == str(world.compiler_run_id)
        assert body["suite_label"] == world.suite.label
        assert body["suite_definition_hash"] == world.suite.definition_hash
        assert body["mission_count"] == len(world.suite.missions)
        assert body["environment_label"] == world.environment.label
        assert body["baseline_run_id"] is None
        # Execution bounds, and deliberately no currency figure anywhere.
        assert "cost" not in body
        assert "estimate" not in body

    async def test_a_merchant_with_nothing_published_is_told_why(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        from agentrank_api.commerce.repository import MerchantRepository

        merchant = await MerchantRepository(session).create(slug="bare-shop", name="Bare")
        await session.commit()
        token = await issue_credential(merchant.id)
        http = client(settings, factory)

        body = http.get(PREFLIGHT, headers=bearer(token)).json()

        assert body["launchable"] is False
        codes = {blocker["code"] for blocker in body["blockers"]}
        assert codes == {
            "no_published_representation",
            "benchmark_suite_unavailable",
            "benchmark_world_unregistered",
        }
        assert all(blocker["message"] for blocker in body["blockers"])

    async def test_preflight_names_a_baseline_once_one_run_has_completed(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        world = await build_launch_world(session, "baseline-shop")
        runs = BenchmarkRunService(session)
        started = await runs.start_run(
            suite_key=world.suite.suite_key,
            suite_version=world.suite.version,
            merchant_slug=world.merchant_slug,
        )
        run_id, merchant_id = started.id, world.merchant_id
        await runs.start_mission(run_id, "buy-a-charger", merchant_id=merchant_id)
        await runs.abort_run(run_id, merchant_id=merchant_id)
        token = await issue_credential(merchant_id)
        http = client(settings, factory)

        # An aborted run is not a baseline: its numbers describe part of a workload.
        assert http.get(PREFLIGHT, headers=bearer(token)).json()["baseline_run_id"] is None


class TestAdmission:
    async def test_a_launch_freezes_every_methodology_critical_identity(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        world = await build_launch_world(session, "freeze-shop")
        token = await issue_credential(world.merchant_id)
        http = client(settings, factory)

        response = http.post(
            LAUNCH,
            headers=bearer(token),
            json={
                "representation_id": str(world.representation.id),
                "request_key": "freeze-request",
            },
        )

        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "QUEUED"
        assert body["run_id"] is None
        assert body["run_status"] is None
        assert body["missions_completed"] is None
        assert body["representation_id"] == str(world.representation.id)
        assert body["compiler_run_id"] == str(world.compiler_run_id)
        assert body["suite_id"] == str(world.suite.id)
        assert body["mission_count"] == len(world.suite.missions)
        assert body["executor_kind"]

    async def test_a_repeated_request_key_answers_with_the_same_launch(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        world = await build_launch_world(session, "retry-shop")
        token = await issue_credential(world.merchant_id)
        http = client(settings, factory)
        body = {
            "representation_id": str(world.representation.id),
            "request_key": "double-submitted",
        }

        first = http.post(LAUNCH, headers=bearer(token), json=body)
        second = http.post(LAUNCH, headers=bearer(token), json=body)

        assert first.status_code == 201
        assert second.status_code == 201
        assert first.json()["reevaluation_id"] == second.json()["reevaluation_id"]
        listed = http.get(LAUNCH, headers=bearer(token)).json()
        assert len(listed) == 1

    async def test_one_request_key_cannot_launch_a_different_representation(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        world = await build_launch_world(session, "reused-shop")
        token = await issue_credential(world.merchant_id)
        http = client(settings, factory)
        http.post(
            LAUNCH,
            headers=bearer(token),
            json={
                "representation_id": str(world.representation.id),
                "request_key": "reused-key",
            },
        )

        refused = http.post(
            LAUNCH,
            headers=bearer(token),
            json={"representation_id": str(uuid.uuid7()), "request_key": "reused-key"},
        )

        assert refused.status_code == 409
        assert refused.json()["error"] == "reevaluation_request_key_reused"

    async def test_a_second_pending_launch_is_refused_by_name(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        world = await build_launch_world(session, "queued-shop")
        token = await issue_credential(world.merchant_id)
        http = client(settings, factory)
        http.post(
            LAUNCH,
            headers=bearer(token),
            json={
                "representation_id": str(world.representation.id),
                "request_key": "first-launch",
            },
        )

        refused = http.post(
            LAUNCH,
            headers=bearer(token),
            json={
                "representation_id": str(world.representation.id),
                "request_key": "second-launch",
            },
        )

        assert refused.status_code == 409
        assert refused.json()["error"] == "reevaluation_already_pending"
        assert http.get(PREFLIGHT, headers=bearer(token)).json()["launchable"] is False

    async def test_a_superseded_representation_is_refused_rather_than_substituted(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        world = await build_launch_world(session, "stale-shop")
        stale = world.representation.id
        # The merchant compiles and publishes a newer source while the page is open.
        snapshot = await MerchantRepresentationService(session).publish_source(
            world_source("stale-shop", version=2)
        )
        compiler = MerchantCompilerService(session)
        newer_run = await compiler.run(world.merchant_id, snapshot.id)
        newer = await compiler.publish(world.merchant_id, newer_run.id)
        token = await issue_credential(world.merchant_id)
        http = client(settings, factory)

        refused = http.post(
            LAUNCH,
            headers=bearer(token),
            json={"representation_id": str(stale), "request_key": "stale-key"},
        )

        assert refused.status_code == 409
        assert refused.json()["error"] == "representation_superseded"
        assert refused.json()["identifier"] == str(newer.id)
        # The current one launches, so the refusal was about staleness and not about the world.
        accepted = http.post(
            LAUNCH,
            headers=bearer(token),
            json={"representation_id": str(newer.id), "request_key": "current-key"},
        )
        assert accepted.status_code == 201


class TestBuyerResolution:
    async def test_a_deployment_with_a_model_provider_freezes_the_model_buyer(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        world = await build_launch_world(session, "model-shop")
        token = await issue_credential(world.merchant_id)
        http = client_with_model_provider(settings, factory)

        plan = http.get(PREFLIGHT, headers=bearer(token)).json()
        assert plan["buyer_profile"] == "AI_BUYER"
        assert plan["provider"] == OPENAI_PROVIDER
        assert plan["requested_model"]
        assert plan["max_model_turns"] > 0
        assert plan["max_tool_calls"] > 0
        assert plan["mission_deadline_seconds"] > 0

        launched = http.post(
            LAUNCH,
            headers=bearer(token),
            json={
                "representation_id": str(world.representation.id),
                "request_key": "model-buyer-key",
            },
        ).json()
        assert launched["buyer_profile"] == "AI_BUYER"
        assert launched["executor_kind"] == "llm-openai"
        assert launched["buyer_configuration_digest"].startswith("sha256:")
        assert launched["requested_model"] == plan["requested_model"]

    async def test_a_deployment_with_no_model_provider_says_which_buyer_it_will_use(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        world = await build_launch_world(session, "referenced-shop")
        token = await issue_credential(world.merchant_id)
        http = client(settings, factory)

        plan = http.get(PREFLIGHT, headers=bearer(token)).json()

        assert plan["buyer_profile"] == "REFERENCE_BUYER"
        assert plan["executor_kind"] == "reference-isolated"
        assert plan["provider"] is None
        assert plan["requested_model"] is None
        # Nothing is invented for a buyer that has none of it.
        assert plan["max_model_turns"] is None
        assert plan["mission_deadline_seconds"] is None
        assert plan["launchable"] is True


class TestReadCost:
    async def test_listing_launches_does_not_grow_a_statement_per_launch(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        catalog_engine: AsyncEngine,
    ) -> None:
        """The history list and the launch page are the console's most repeated reads.

        The launch page re-reads itself every few seconds while a run executes, so a read that
        grew a handful of statements per row would be this console's most wasteful pattern by a
        wide margin. What is asserted is the property rather than a magic number: reading three
        launches costs exactly what reading one costs.
        """
        pinned = without_providers(settings)
        world = await build_launch_world(session, "cost-shop")
        service = MerchantReevaluationService(session, pinned)
        worker = ReevaluationWorkerService(session)
        for index in range(3):
            launch = await service.request(
                world.merchant_id,
                representation_id=world.representation.id,
                request_key=f"cost-request-{index}",
            )
            # Settled without a run, so the merchant's one pending slot frees for the next.
            await worker.settle_failed(launch.id, failure_code="run_aborted")

        counted: list[str] = []

        def record(
            connection: object,
            cursor: object,
            statement: str,
            parameters: object,
            context: object,
            executemany: object,
        ) -> None:
            counted.append(statement)

        event.listen(catalog_engine.sync_engine, "before_cursor_execute", record)
        try:
            counted.clear()
            await service.details(world.merchant_id, limit=1)
            one = len(counted)
            counted.clear()
            await service.details(world.merchant_id, limit=3)
            three = len(counted)
        finally:
            event.remove(catalog_engine.sync_engine, "before_cursor_execute", record)

        assert one > 0
        assert three == one
