"""The insights HTTP surface: authenticated, merchant scoped and bounded.

Every test here talks to the real application over the FastAPI test client with a real
merchant credential, because the point of this file is exactly the boundary: who may read
what, what a foreign identifier answers, and what the serialized payloads do and do not
contain. A diagnostics layer that leaked another merchant's run, a provider payload or an
authorization header would fail here rather than in review.
"""

import uuid
from pathlib import Path

import pytest
from benchmark_support import VOLTEDGE, fixture, mission, suite
from commerce_support import build_shop
from conftest import CredentialIssuer
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentrank_api.benchmark.buyer import MerchantBuyerSurface
from agentrank_api.benchmark.environment import BenchmarkEnvironmentService
from agentrank_api.benchmark.experiment import CompilerImpactExperimentService
from agentrank_api.benchmark.llm import GEMINI_PROVIDER, AgentConfiguration
from agentrank_api.benchmark.reference_executor import ReferenceMissionExecutor
from agentrank_api.benchmark.repository import BenchmarkSuiteRepository
from agentrank_api.benchmark.runner import BenchmarkRunService
from agentrank_api.benchmark.suites import BenchmarkSuiteService
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.compiler.service import MerchantCompilerService
from agentrank_api.config import Settings
from agentrank_api.main import create_app
from agentrank_api.payments.fake import FakePaymentProvider
from agentrank_api.representation.fixtures import read_source
from agentrank_api.representation.service import MerchantRepresentationService

pytestmark = pytest.mark.anyio

SLUG = "test-merchant"


async def run_one(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> tuple[uuid.UUID, uuid.UUID]:
    """One completed reference run for the test merchant, through every real service."""
    built = await build_shop(session, SLUG)
    world = fixture()
    await BenchmarkEnvironmentService(session).register(world)
    await BenchmarkSuiteService(session).publish(suite(mission("buy-one")))
    surface = MerchantBuyerSurface(
        factory, merchant_id=built.merchant_id, provider=FakePaymentProvider()
    )
    finished = await BenchmarkRunService(session).run_suite(
        ReferenceMissionExecutor(surface),
        suite_key="test-suite",
        suite_version=1,
        fixture=world,
    )
    return built.merchant_id, finished.id


def client(settings: Settings, sessions: async_sessionmaker[AsyncSession]) -> TestClient:
    """The built application with this suite's session factory on its state."""
    app = create_app(settings, payment_provider=FakePaymentProvider())
    app.state.session_factory = sessions
    return TestClient(app)


class TestAuthenticationAndScoping:
    async def test_insights_require_a_credential(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        _, run_id = await run_one(session, factory)
        http = client(settings, factory)
        anonymous = http.get(f"/api/v1/insights/runs/{run_id}")
        assert anonymous.status_code == 401
        assert "WWW-Authenticate" in anonymous.headers

    async def test_own_run_reads_and_foreign_ids_answer_404(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        theirs = await build_shop(session, "other-shop")
        merchant_id, run_id = await run_one(session, factory)
        token = await issue_credential(merchant_id)
        foreign = await issue_credential(theirs.merchant_id)
        http = client(settings, factory)

        headers = {"Authorization": f"Bearer {token}"}
        overview = http.get("/api/v1/insights/overview", headers=headers)
        assert overview.status_code == 200

        runs = http.get("/api/v1/insights/runs", headers=headers)
        assert runs.status_code == 200
        assert [entry["run_id"] for entry in runs.json()] == [str(run_id)]

        detail = http.get(f"/api/v1/insights/runs/{run_id}", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["status"] == "COMPLETED"

        # The same identifier under the other merchant's credential is not found, byte for
        # byte like an identifier nobody has ever used.
        other = {"Authorization": f"Bearer {foreign}"}
        assert http.get(f"/api/v1/insights/runs/{run_id}", headers=other).status_code == 404
        foreign_mission = http.get(
            f"/api/v1/insights/runs/{run_id}/missions/{uuid.uuid7()}", headers=other
        )
        assert foreign_mission.status_code == 404
        assert (
            http.get(f"/api/v1/insights/experiments/{uuid.uuid7()}", headers=other).status_code
            == 404
        )

    async def test_nonexistent_identifiers_answer_404(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        merchant_id, _ = await run_one(session, factory)
        token = await issue_credential(merchant_id)
        http = client(settings, factory)
        headers = {"Authorization": f"Bearer {token}"}
        assert http.get(f"/api/v1/insights/runs/{uuid.uuid7()}", headers=headers).status_code == 404


class TestMissionAndTrace:
    async def test_mission_detail_and_trace_are_readable_for_own_missions(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        merchant_id, run_id = await run_one(session, factory)
        token = await issue_credential(merchant_id)
        http = client(settings, factory)
        headers = {"Authorization": f"Bearer {token}"}

        detail = http.get(f"/api/v1/insights/runs/{run_id}", headers=headers).json()
        mission_entry = detail["missions"][0]
        mission_run_id = mission_entry["mission_run_id"]

        mission_response = http.get(
            f"/api/v1/insights/runs/{run_id}/missions/{mission_run_id}", headers=headers
        )
        assert mission_response.status_code == 200
        body = mission_response.json()
        assert body["primary_code"] is None or isinstance(body["primary_code"], str)
        assert body["engine_identity"].startswith("sha256:")

        trace = http.get(
            f"/api/v1/insights/runs/{run_id}/missions/{mission_run_id}/trace", headers=headers
        )
        assert trace.status_code == 200
        assert trace.json()["total_events"] == 0

    async def test_trace_pagination_is_bounded(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        merchant_id, run_id = await run_one(session, factory)
        token = await issue_credential(merchant_id)
        http = client(settings, factory)
        headers = {"Authorization": f"Bearer {token}"}
        detail = http.get(f"/api/v1/insights/runs/{run_id}", headers=headers).json()
        mission_run_id = detail["missions"][0]["mission_run_id"]

        too_many = http.get(
            f"/api/v1/insights/runs/{run_id}/missions/{mission_run_id}/trace",
            params={"limit": 10000},
            headers=headers,
        )
        assert too_many.status_code == 422

        negative = http.get(
            f"/api/v1/insights/runs/{run_id}/missions/{mission_run_id}/trace",
            params={"offset": -5},
            headers=headers,
        )
        assert negative.status_code == 422


class TestExperimentEndpoint:
    async def test_experiment_comparison_is_readable_with_warnings(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        source_definition = read_source(Path("benchmarks/voltedge/source.json"))
        merchant = await MerchantRepository(session).create(slug="voltedge", name="VoltEdge")
        await session.commit()
        representations = MerchantRepresentationService(session)
        source = await representations.publish_source(source_definition)
        compiler = MerchantCompilerService(session)
        compiler_run = await compiler.run(merchant.id, source.id)
        compiled = await compiler.publish(merchant.id, compiler_run.id)
        stored_suite = await BenchmarkSuiteRepository(session).create(
            suite(mission("buy-one"), merchant_slug="voltedge")
        )
        await session.commit()
        environment = await BenchmarkEnvironmentService(session).register(VOLTEDGE.fixture)
        config = AgentConfiguration(provider=GEMINI_PROVIDER, requested_model="test-model")
        experiment = await CompilerImpactExperimentService(session).create(
            merchant_id=merchant.id,
            suite_id=stored_suite.id,
            environment=environment,
            source_snapshot_id=source.id,
            compiled_representation_id=compiled.id,
            buyer_configuration=config.payload(),
            buyer_configuration_digest=config.configuration_digest,
            sample_count=1,
            development_benchmark=False,
        )
        token = await issue_credential(merchant.id)
        http = client(settings, factory)
        headers = {"Authorization": f"Bearer {token}"}

        response = http.get(f"/api/v1/insights/experiments/{experiment.id}", headers=headers)
        assert response.status_code == 200
        body = response.json()
        assert body["conclusion"]["kind"] == "INCOMPLETE"
        codes = {warning["code"] for warning in body["warnings"]}
        assert "INCOMPLETE_PAIRS" in codes
        assert "DEVELOPMENT_BENCHMARK" not in codes


class TestPayloadHygiene:
    async def test_serialized_payloads_carry_no_secret_material(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        merchant_id, run_id = await run_one(session, factory)
        token = await issue_credential(merchant_id)
        http = client(settings, factory)
        headers = {"Authorization": f"Bearer {token}"}

        detail = http.get(f"/api/v1/insights/runs/{run_id}", headers=headers).json()
        mission_run_id = detail["missions"][0]["mission_run_id"]
        captured: list[str] = []
        paths = [
            "/api/v1/insights/overview",
            "/api/v1/insights/runs",
            f"/api/v1/insights/runs/{run_id}",
            f"/api/v1/insights/runs/{run_id}/missions/{mission_run_id}",
            f"/api/v1/insights/runs/{run_id}/missions/{mission_run_id}/trace",
        ]
        for path in paths:
            response = http.get(path, headers=headers)
            assert response.status_code == 200
            captured.append(response.text)

        joined = "\n".join(captured)
        # The presented credential must never be echoed anywhere in any response.
        assert token not in joined
        assert token.split("_")[-1] not in joined
        assert "secret_hash" not in joined
        assert "postgres" not in joined.lower()
        assert "authorization" not in joined.lower()

    async def test_the_insights_namespace_declares_itself_in_openapi(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        http = client(settings, factory)
        document = http.get("/openapi.json").json()
        insight_paths = [path for path in document["paths"] if path.startswith("/api/v1/insights")]
        assert len(insight_paths) == 6
