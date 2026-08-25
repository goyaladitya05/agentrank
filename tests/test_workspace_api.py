"""A merchant's evaluation setup over HTTP, and the boundary it keeps.

Every test here talks to the real application with a real merchant credential, because what is
being asserted is exactly the boundary: who may build a setup, what a browser is allowed to say,
what a foreign identifier answers, and what a repeated command does.

Nothing here executes a benchmark and nothing calls a model. Building a setup is deterministic,
spends nothing, and writes no commerce row.
"""

import pytest
from conftest import CredentialIssuer, bearer
from fastapi.testclient import TestClient
from launch_support import without_providers
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from workspace_support import awkward, catalogued, plain, product, source, variant

from agentrank_api.auth.service import MerchantCredentialService
from agentrank_api.auth.tokens import TokenMarker
from agentrank_api.benchmark.execution import BenchmarkRunCapability
from agentrank_api.benchmark.models import BenchmarkSuite
from agentrank_api.benchmark.runner import BenchmarkRunService
from agentrank_api.commerce.models import Merchant
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.config import Settings
from agentrank_api.main import create_app
from agentrank_api.payments.fake import FakePaymentProvider
from agentrank_api.representation.definitions import MerchantSourceDefinition
from agentrank_api.representation.models import MerchantSourceSnapshot
from agentrank_api.representation.service import MerchantRepresentationService
from agentrank_api.workspace.service import MerchantEvaluationWorkspaceService

pytestmark = pytest.mark.anyio

SETUP = "/api/v1/benchmark/workspace"
HISTORY = "/api/v1/benchmark/workspace/history"
PREFLIGHT = "/api/v1/benchmark/evaluations/preflight"


def client(settings: Settings, sessions: async_sessionmaker[AsyncSession]) -> TestClient:
    """The built application with this suite's session factory on its state.

    Provider credentials are cleared, so a developer machine with a key in `.env` and a CI runner
    without one resolve the same buyer and no test can reach a model provider.
    """
    app = create_app(without_providers(settings), payment_provider=FakePaymentProvider())
    app.state.session_factory = sessions
    return TestClient(app)


async def merchant_with(
    session: AsyncSession, slug: str, definition: MerchantSourceDefinition | None = None
) -> tuple[Merchant, MerchantSourceSnapshot]:
    merchant = await MerchantRepository(session).create(slug=slug, name=slug.title())
    await session.commit()
    snapshot = await MerchantRepresentationService(session).publish_source(
        definition or catalogued(slug)
    )
    return merchant, snapshot


class TestAuthorization:
    async def test_every_route_requires_a_credential(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        _, snapshot = await merchant_with(session, "anon-workspace-shop")
        http = client(settings, factory)

        assert http.get(SETUP).status_code == 401
        assert http.get(HISTORY).status_code == 401
        assert http.post(SETUP, json={"source_snapshot_id": str(snapshot.id)}).status_code == 401

    async def test_a_benchmark_buyer_credential_is_refused(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A buyer that could build a setup could publish the suite it is measured against."""
        merchant, snapshot = await merchant_with(session, "buyer-workspace-shop")
        built = await MerchantEvaluationWorkspaceService(session).bootstrap(
            merchant.id, source_snapshot_id=snapshot.id
        )
        suite = await session.get(BenchmarkSuite, built.workspace.suite_id)
        assert suite is not None
        started = await BenchmarkRunService(session).start_run(
            suite_key=suite.suite_key, suite_version=suite.version, merchant_slug=merchant.slug
        )
        issued = await MerchantCredentialService(session).issue_for_benchmark(
            capability=BenchmarkRunCapability(merchant_id=merchant.id, run_id=started.id),
            label="benchmark executor",
            marker=TokenMarker.DEVELOPMENT,
        )
        http = client(settings, factory)
        headers = bearer(issued.token)

        assert http.get(SETUP, headers=headers).status_code == 401
        assert http.get(HISTORY, headers=headers).status_code == 401
        assert (
            http.post(
                SETUP, headers=headers, json={"source_snapshot_id": str(snapshot.id)}
            ).status_code
            == 401
        )

    async def test_a_browser_cannot_build_from_another_merchants_source(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        """The snapshot identifier is checked against the caller's own current source, never
        used to select one, so naming somebody else's is refused rather than honoured."""
        _, theirs = await merchant_with(session, "their-source-shop")
        mine, _ = await merchant_with(session, "my-source-shop")
        token = await issue_credential(mine.id)
        http = client(settings, factory)

        response = http.post(
            SETUP, headers=bearer(token), json={"source_snapshot_id": str(theirs.id)}
        )

        assert response.status_code == 409
        assert response.json()["error"] == "source_superseded"


class TestReadingTheSetup:
    async def test_a_merchant_with_no_source_is_told_what_to_do(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        merchant = await MerchantRepository(session).create(slug="bare-setup", name="Bare")
        await session.commit()
        token = await issue_credential(merchant.id)
        http = client(settings, factory)

        body = http.get(SETUP, headers=bearer(token)).json()

        assert body["buildable"] is False
        assert body["workspace"] is None
        assert body["planned"] is None
        assert [entry["code"] for entry in body["blockers"]] == ["merchant_source_unavailable"]
        assert all(entry["message"] for entry in body["blockers"])

    async def test_a_merchant_with_source_reads_what_would_be_built(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        """Mission count and composition before anything is built, so the scale is never a
        surprise."""
        merchant, snapshot = await merchant_with(session, "planned-setup")
        token = await issue_credential(merchant.id)
        http = client(settings, factory)

        body = http.get(SETUP, headers=bearer(token)).json()

        assert body["buildable"] is True
        assert body["current_source_snapshot_id"] == str(snapshot.id)
        planned = body["planned"]
        assert planned["mission_count"] > 0
        assert planned["mission_count"] == sum(
            entry["missions"] for entry in planned["composition"]
        )
        assert planned["catalog"]["products"] > 0
        assert planned["catalog"]["currencies"] == ["INR"]
        assert any(entry["reason"] for entry in planned["unsupported"])

    async def test_the_setup_never_publishes_a_mission_or_its_answer(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        """Counts and composition, never a brief and never an expected outcome."""
        merchant, snapshot = await merchant_with(session, "quiet-setup")
        token = await issue_credential(merchant.id)
        http = client(settings, factory)
        http.post(SETUP, headers=bearer(token), json={"source_snapshot_id": str(snapshot.id)})

        text = http.get(SETUP, headers=bearer(token)).text

        assert "expected_outcome" not in text
        assert "PURCHASE_AVAILABLE" not in text
        assert "simulated_value_amount_minor" not in text
        assert "objective" not in text

    async def test_a_merchant_whose_data_supports_nothing_is_told_why(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        merchant, _ = await merchant_with(
            session,
            "nothing-setup",
            source(product("P1", variant("P1-A", stock=0)), slug="nothing-setup"),
        )
        token = await issue_credential(merchant.id)
        http = client(settings, factory)

        body = http.get(SETUP, headers=bearer(token)).json()

        assert body["buildable"] is False
        assert [entry["code"] for entry in body["blockers"]] == ["no_purchasable_variant"]


class TestBuildingASetup:
    async def test_building_makes_a_first_evaluation_available(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        """The whole phase, over the wire: source in, and the existing first evaluation preflight
        stops saying the merchant has no world and no suite."""
        merchant, snapshot = await merchant_with(session, "ready-setup")
        token = await issue_credential(merchant.id)
        http = client(settings, factory)

        before = http.get(PREFLIGHT, headers=bearer(token)).json()
        assert {entry["code"] for entry in before["blockers"]} == {
            "benchmark_suite_unavailable",
            "benchmark_world_unregistered",
        }

        built = http.post(
            SETUP, headers=bearer(token), json={"source_snapshot_id": str(snapshot.id)}
        )
        assert built.status_code == 201
        assert built.json()["created"] is True

        after = http.get(PREFLIGHT, headers=bearer(token)).json()
        assert after["blockers"] == []
        assert after["launchable"] is True
        assert after["purpose"] == "INITIAL"
        assert after["mission_count"] == built.json()["workspace"]["mission_count"]
        assert after["suite_id"] == built.json()["workspace"]["suite_id"]
        assert after["environment_id"] == built.json()["workspace"]["environment_id"]

    async def test_building_executes_no_benchmark(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        """A setup and a measurement are two commands, and this is the one that spends nothing."""
        merchant, snapshot = await merchant_with(session, "unspent-setup")
        token = await issue_credential(merchant.id)
        http = client(settings, factory)

        http.post(SETUP, headers=bearer(token), json={"source_snapshot_id": str(snapshot.id)})

        assert http.get("/api/v1/benchmark/evaluations", headers=bearer(token)).json() == []

    async def test_a_repeated_command_answers_with_the_same_setup(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        """201 both times, and `created` is what says which one wrote it."""
        merchant, snapshot = await merchant_with(session, "repeat-setup")
        token = await issue_credential(merchant.id)
        http = client(settings, factory)
        body = {"source_snapshot_id": str(snapshot.id)}

        first = http.post(SETUP, headers=bearer(token), json=body)
        second = http.post(SETUP, headers=bearer(token), json=body)

        assert (first.status_code, second.status_code) == (201, 201)
        assert first.json()["created"] is True
        assert second.json()["created"] is False
        assert (
            first.json()["workspace"]["workspace_id"]
            == (second.json()["workspace"]["workspace_id"])
        )

    async def test_a_body_naming_anything_else_is_refused(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        """A browser may say which evidence it read and nothing else about what gets built."""
        merchant, snapshot = await merchant_with(session, "extra-setup")
        token = await issue_credential(merchant.id)
        http = client(settings, factory)

        response = http.post(
            SETUP,
            headers=bearer(token),
            json={
                "source_snapshot_id": str(snapshot.id),
                "mission_budget": 40,
                "merchant_slug": "somebody-else",
            },
        )

        assert response.status_code == 422

    async def test_a_newer_source_is_reported_and_never_applied(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        merchant, first = await merchant_with(session, "refreshed-setup")
        token = await issue_credential(merchant.id)
        http = client(settings, factory)
        http.post(SETUP, headers=bearer(token), json={"source_snapshot_id": str(first.id)})
        await MerchantRepresentationService(session).publish_source(
            source(*awkward(merchant.slug).products, slug=merchant.slug, version=2)
        )

        body = http.get(SETUP, headers=bearer(token)).json()

        assert body["source_is_newer_than_the_workspace"] is True
        assert body["workspace"]["source_snapshot_id"] == str(first.id)
        assert body["current_source_snapshot_id"] != str(first.id)

    async def test_history_holds_every_setup_newest_first(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        merchant, first = await merchant_with(session, "history-setup")
        token = await issue_credential(merchant.id)
        http = client(settings, factory)
        http.post(SETUP, headers=bearer(token), json={"source_snapshot_id": str(first.id)})
        newer = await MerchantRepresentationService(session).publish_source(
            source(*plain(merchant.slug).products, slug=merchant.slug, version=2)
        )
        http.post(SETUP, headers=bearer(token), json={"source_snapshot_id": str(newer.id)})

        body = http.get(HISTORY, headers=bearer(token)).json()

        assert [entry["source_snapshot_id"] for entry in body] == [str(newer.id), str(first.id)]

    async def test_another_merchants_history_is_never_visible(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        theirs, their_snapshot = await merchant_with(session, "their-history-shop")
        mine, _ = await merchant_with(session, "my-history-shop")
        their_token = await issue_credential(theirs.id)
        my_token = await issue_credential(mine.id)
        http = client(settings, factory)
        http.post(
            SETUP, headers=bearer(their_token), json={"source_snapshot_id": str(their_snapshot.id)}
        )

        assert http.get(HISTORY, headers=bearer(my_token)).json() == []
        assert http.get(SETUP, headers=bearer(my_token)).json()["workspace"] is None
