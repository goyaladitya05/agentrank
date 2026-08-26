"""The merchant evaluation launch command over HTTP, and the identity it freezes.

Every test here talks to the real application with a real merchant credential, because what is
being asserted is exactly the boundary: who may launch, which command the server decides this
merchant is making, what a foreign identifier answers, what a repeated request does, and what
the server resolves rather than accepting from a browser.

Nothing here executes a benchmark. Admission writes a queued launch and answers, which is what
lets a browser request stay an ordinary short request.
"""

import uuid
from dataclasses import replace

import pytest
from conftest import CredentialIssuer, bearer
from fastapi.testclient import TestClient
from launch_support import (
    build_initial_world,
    build_launch_world,
    complete_run,
    queue_launch,
    with_openai,
    without_providers,
    world_source,
)
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agentrank_api.auth.service import MerchantCredentialService
from agentrank_api.auth.tokens import TokenMarker
from agentrank_api.benchmark.environment import BenchmarkEnvironmentService
from agentrank_api.benchmark.evaluation_launch import (
    BenchmarkEvaluationLaunch,
    EvaluationPurpose,
)
from agentrank_api.benchmark.execution import BenchmarkRunCapability
from agentrank_api.benchmark.launch import (
    EvaluationLaunchWorkerService,
    MerchantEvaluationLaunchService,
)
from agentrank_api.benchmark.llm import OPENAI_PROVIDER
from agentrank_api.benchmark.runner import BenchmarkRunService
from agentrank_api.compiler.service import MerchantCompilerService
from agentrank_api.config import Settings
from agentrank_api.main import create_app
from agentrank_api.payments.fake import FakePaymentProvider
from agentrank_api.representation.service import MerchantRepresentationService

pytestmark = pytest.mark.anyio

PREFLIGHT = "/api/v1/benchmark/evaluations/preflight"
LAUNCH = "/api/v1/benchmark/evaluations"


def launch_body(
    http: TestClient,
    token: str,
    *,
    request_key: str,
    representation_id: str | None = None,
    purpose: str | None = None,
) -> dict[str, str | None]:
    """The body the console sends: which command it read, what it is looking at, and which
    request this is.

    The purpose and the digest come from the preflight this "page" just read, which is exactly
    what the console binds into its form. A test that invented either would be admitting a plan
    nobody was shown.
    """
    preflight = http.get(PREFLIGHT, headers=bearer(token)).json()
    resolved = preflight["purpose"] if purpose is None else purpose
    return {
        "purpose": resolved,
        "representation_id": (
            representation_id
            if representation_id is not None or resolved == "INITIAL"
            else preflight["representation_id"]
        ),
        "request_key": request_key,
        "plan_digest": preflight["plan_digest"],
    }


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
                    "purpose": "REEVALUATION",
                    "representation_id": str(world.representation.id),
                    "request_key": "anonymous-key",
                    "plan_digest": "sha256:" + "0" * 64,
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
        theirs_launch_id = await queue_launch(
            session, without_providers(settings), theirs, request_key="their-request"
        )
        token = await issue_credential(mine.merchant_id)
        http = client(settings, factory)

        response = http.get(f"{LAUNCH}/{theirs_launch_id}", headers=bearer(token))
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
                **launch_body(
                    http,
                    token,
                    representation_id=str(mine.representation_id),
                    request_key="widened-key",
                ),
                "merchant_id": str(theirs.merchant_id),
                "suite_id": str(theirs.suite_id),
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
            json=launch_body(
                http,
                token,
                representation_id=str(theirs.representation_id),
                request_key="cross-tenant-key",
            ),
        )
        assert refused.status_code == 409
        assert refused.json()["error"] == "representation_superseded"
        assert (
            await session.scalar(
                BenchmarkEvaluationLaunch.__table__.select().with_only_columns(
                    BenchmarkEvaluationLaunch.id
                )
            )
        ) is None


class TestBenchmarkCredentialBoundary:
    async def test_a_benchmark_buyers_credential_cannot_command_a_benchmark(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A buyer executing a run must have no route to that run's lifecycle.

        The loopback server an isolated buyer is given does not mount this router at all, which
        is the layer that holds even if this one were removed. This is the second layer, and it
        holds for any deployment where a benchmark credential can reach the ordinary API.
        """
        world = await build_launch_world(session, "buyer-credential-shop")
        started = await BenchmarkRunService(session).start_run(
            suite_key=world.suite.suite_key,
            suite_version=world.suite.version,
            merchant_slug=world.merchant_slug,
        )
        capability = BenchmarkRunCapability(merchant_id=world.merchant_id, run_id=started.id)
        issued = await MerchantCredentialService(session).issue_for_benchmark(
            capability=capability, label="benchmark executor", marker=TokenMarker.DEVELOPMENT
        )
        http = client(settings, factory)
        headers = bearer(issued.token)

        assert http.get(PREFLIGHT, headers=headers).status_code == 401
        assert http.get(LAUNCH, headers=headers).status_code == 401
        assert http.get(f"{LAUNCH}/{uuid.uuid7()}", headers=headers).status_code == 401
        refused = http.post(
            LAUNCH,
            headers=headers,
            json={
                "representation_id": str(world.representation_id),
                "request_key": "buyer-launch-key",
                "plan_digest": "sha256:" + "0" * 64,
            },
        )
        assert refused.status_code == 401
        # Byte for byte the refusal an unknown credential gets: which of the two it is says
        # something about the credential, and this says nothing.
        assert refused.json() == {
            "error": "unauthenticated",
            "detail": "a valid merchant API credential is required",
            "resource": None,
            "identifier": None,
        }


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

    async def test_a_merchant_with_nothing_at_all_is_told_every_reason(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        """A merchant with no evidence is offered a first evaluation, and told what it needs."""
        from agentrank_api.commerce.repository import MerchantRepository

        merchant = await MerchantRepository(session).create(slug="bare-shop", name="Bare")
        await session.commit()
        token = await issue_credential(merchant.id)
        http = client(settings, factory)

        body = http.get(PREFLIGHT, headers=bearer(token)).json()

        assert body["purpose"] == "INITIAL"
        assert body["launchable"] is False
        codes = {blocker["code"] for blocker in body["blockers"]}
        assert codes == {
            "merchant_source_unavailable",
            "benchmark_suite_unavailable",
            "benchmark_world_unregistered",
        }
        assert all(blocker["message"] for blocker in body["blockers"])

    async def test_a_merchant_with_history_and_nothing_published_is_told_to_publish(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        """Once evidence exists, an initial evaluation is not offered a second time.

        A first evaluation is the way out of having no evidence. A merchant who has one is past
        that, so the next honest measurement is of something they publish, and the preflight says
        so rather than offering to measure the storefront again.
        """
        world = await build_initial_world(session, "history-bare-shop")
        await complete_run(session, world)
        token = await issue_credential(world.merchant_id)
        http = client(settings, factory)

        body = http.get(PREFLIGHT, headers=bearer(token)).json()

        assert body["purpose"] == "REEVALUATION"
        assert body["launchable"] is False
        assert [blocker["code"] for blocker in body["blockers"]] == ["no_published_representation"]

    async def test_a_published_merchant_with_no_runs_still_re_evaluates(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        """Publishing decides the command, not run history.

        A merchant who has published is asking about that artifact whether or not they have ever
        run a benchmark, and this was admissible before first evaluations existed. Making them
        measure a storefront first would be forcing a workflow on a merchant who already told
        AgentRank what they want measured.
        """
        world = await build_launch_world(session, "published-no-runs-shop")
        token = await issue_credential(world.merchant_id)
        http = client(settings, factory)

        body = http.get(PREFLIGHT, headers=bearer(token)).json()

        assert body["purpose"] == "REEVALUATION"
        assert body["launchable"] is True
        assert body["representation_id"] == str(world.representation_id)
        assert body["baseline_run_id"] is None

    async def test_preflight_says_when_the_earlier_run_read_a_different_surface(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        """A merchant is told before spending that there will be no reading beside this one.

        Their first evaluation read the ordinary storefront and this re-evaluation would deliver
        a representation, so the comparison engine refuses to draw a before and after across the
        two. Saying that after the run finishes would be saying it after the quota was spent.
        """
        world = await build_initial_world(session, "surface-warning-shop")
        await complete_run(session, world)
        compiler = MerchantCompilerService(session)
        compiler_run = await compiler.run(world.merchant_id, world.source_snapshot_id)
        await compiler.publish(world.merchant_id, compiler_run.id)
        token = await issue_credential(world.merchant_id)
        http = client_with_model_provider(settings, factory)

        body = http.get(PREFLIGHT, headers=bearer(token)).json()

        assert body["purpose"] == "REEVALUATION"
        assert body["buyer_profile"] == "AI_BUYER"
        assert body["baseline_run_id"] is not None
        assert body["baseline_surface_matches"] is False

    async def test_preflight_reports_no_surface_change_without_a_baseline(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        """Null rather than true. With nothing to compare against there is nothing to match."""
        world = await build_launch_world(session, "surface-null-shop")
        token = await issue_credential(world.merchant_id)
        http = client(settings, factory)

        body = http.get(PREFLIGHT, headers=bearer(token)).json()

        assert body["baseline_run_id"] is None
        assert body["baseline_surface_matches"] is None

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
            json=launch_body(
                http,
                token,
                representation_id=str(world.representation_id),
                request_key="freeze-request",
            ),
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
        body = launch_body(
            http,
            token,
            representation_id=str(world.representation_id),
            request_key="double-submitted",
        )

        first = http.post(LAUNCH, headers=bearer(token), json=body)
        second = http.post(LAUNCH, headers=bearer(token), json=body)

        assert first.status_code == 201
        assert second.status_code == 201
        assert first.json()["launch_id"] == second.json()["launch_id"]
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
            json=launch_body(
                http,
                token,
                representation_id=str(world.representation_id),
                request_key="reused-key",
            ),
        )

        refused = http.post(
            LAUNCH,
            headers=bearer(token),
            json={
                "purpose": "REEVALUATION",
                "representation_id": str(uuid.uuid7()),
                "request_key": "reused-key",
                "plan_digest": "sha256:" + "0" * 64,
            },
        )

        assert refused.status_code == 409
        assert refused.json()["error"] == "evaluation_request_key_reused"

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
            json=launch_body(
                http,
                token,
                representation_id=str(world.representation_id),
                request_key="first-launch",
            ),
        )

        refused = http.post(
            LAUNCH,
            headers=bearer(token),
            json={
                "purpose": "REEVALUATION",
                "representation_id": str(world.representation_id),
                "request_key": "second-launch",
                "plan_digest": "sha256:" + "0" * 64,
            },
        )

        assert refused.status_code == 409
        assert refused.json()["error"] == "evaluation_already_pending"
        assert http.get(PREFLIGHT, headers=bearer(token)).json()["launchable"] is False

    async def test_a_plan_that_moved_since_the_page_rendered_is_refused(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        """What a merchant committed to is the whole plan they read, not just the artifact.

        The representation has its own refusal because it is what the command is about. Every
        other thing the preflight showed, the suite, the world and the buyer, is covered at once
        by the digest, so none of them can be frozen silently between the render and the submit.
        """
        world = await build_launch_world(session, "moved-plan-shop")
        token = await issue_credential(world.merchant_id)
        http = client(settings, factory)
        body = launch_body(
            http,
            token,
            representation_id=str(world.representation_id),
            request_key="moved-plan-key",
        )
        # A newer benchmark world is registered while the page is open, so the run this launch
        # would produce is no longer prepared against the world the merchant was shown.
        await BenchmarkEnvironmentService(session).register(
            replace(world.fixture, version=world.fixture.version + 1)
        )

        refused = http.post(LAUNCH, headers=bearer(token), json=body)

        assert refused.status_code == 409
        assert refused.json()["error"] == "preflight_superseded"
        # Reading the page again and submitting what it now says is accepted.
        accepted = http.post(
            LAUNCH,
            headers=bearer(token),
            json=launch_body(
                http,
                token,
                representation_id=str(world.representation_id),
                request_key="reloaded-plan-key",
            ),
        )
        assert accepted.status_code == 201

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
            json=launch_body(http, token, representation_id=str(stale), request_key="stale-key"),
        )

        assert refused.status_code == 409
        assert refused.json()["error"] == "representation_superseded"
        assert refused.json()["identifier"] == str(newer.id)
        # The current one launches, so the refusal was about staleness and not about the world.
        accepted = http.post(
            LAUNCH,
            headers=bearer(token),
            json=launch_body(
                http, token, representation_id=str(newer.id), request_key="current-key"
            ),
        )
        assert accepted.status_code == 201


class TestFirstEvaluation:
    """The command a merchant with no evidence and nothing published is actually making."""

    async def test_a_merchant_with_no_history_is_offered_their_current_state(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        world = await build_initial_world(session, "first-preflight-shop")
        token = await issue_credential(world.merchant_id)
        http = client(settings, factory)

        body = http.get(PREFLIGHT, headers=bearer(token)).json()

        assert body["purpose"] == "INITIAL"
        assert body["launchable"] is True
        assert body["blockers"] == []
        # What is measured is the merchant as they are: no Commerce IR anywhere, and the source
        # snapshot named so a merchant reads which of their own documents is under test.
        assert body["representation_id"] is None
        assert body["representation_label"] is None
        assert body["compiler_run_id"] is None
        assert body["source_snapshot_id"] == str(world.source_snapshot_id)
        assert body["source_snapshot_label"] == f"{world.merchant_slug}-source@1"
        assert body["suite_id"] == str(world.suite_id)
        assert body["mission_count"] == len(world.authored.suite.missions)
        assert body["environment_id"] == str(world.environment_id)
        # No before, and no field pretending there might be one later.
        assert body["baseline_run_id"] is None
        assert body["baseline_run_completed_at"] is None

    async def test_a_merchant_without_recorded_information_is_told_what_is_missing(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        world = await build_initial_world(session, "first-nosource-shop", with_source=False)
        token = await issue_credential(world.merchant_id)
        http = client(settings, factory)

        body = http.get(PREFLIGHT, headers=bearer(token)).json()

        assert body["purpose"] == "INITIAL"
        assert body["launchable"] is False
        assert [blocker["code"] for blocker in body["blockers"]] == ["merchant_source_unavailable"]
        assert body["source_snapshot_id"] is None

    async def test_a_first_evaluation_freezes_the_state_it_measures(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        world = await build_initial_world(session, "first-launch-shop")
        token = await issue_credential(world.merchant_id)
        http = client(settings, factory)

        response = http.post(
            LAUNCH,
            headers=bearer(token),
            json=launch_body(http, token, request_key="first-evaluation"),
        )

        assert response.status_code == 201
        body = response.json()
        assert body["purpose"] == "INITIAL"
        assert body["status"] == "QUEUED"
        assert body["run_id"] is None
        assert body["representation_id"] is None
        assert body["representation_label"] is None
        assert body["compiler_run_id"] is None
        assert body["source_snapshot_id"] == str(world.source_snapshot_id)
        assert body["source_snapshot_label"] == f"{world.merchant_slug}-source@1"
        assert body["baseline_run_id"] is None

        stored = await session.get(BenchmarkEvaluationLaunch, uuid.UUID(body["launch_id"]))
        assert stored is not None
        assert stored.purpose is EvaluationPurpose.INITIAL
        assert stored.representation_id is None
        assert stored.compiler_run_id is None
        assert stored.source_snapshot_id == world.source_snapshot_id
        assert stored.baseline_run_id is None

    async def test_a_first_evaluation_never_reports_a_comparison(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        """Absence is absence. A first evaluation has no before and says so with null."""
        world = await build_initial_world(session, "first-nocompare-shop")
        token = await issue_credential(world.merchant_id)
        http = client(settings, factory)
        launched = http.post(
            LAUNCH,
            headers=bearer(token),
            json=launch_body(http, token, request_key="first-nocompare"),
        ).json()

        detail = http.get(f"{LAUNCH}/{launched['launch_id']}", headers=bearer(token)).json()

        assert detail["comparison"] is None
        assert detail["baseline_run_id"] is None

    async def test_a_repeated_first_evaluation_request_is_the_same_launch(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        world = await build_initial_world(session, "first-retry-shop")
        token = await issue_credential(world.merchant_id)
        http = client(settings, factory)
        body = launch_body(http, token, request_key="first-double-submit")

        first = http.post(LAUNCH, headers=bearer(token), json=body)
        second = http.post(LAUNCH, headers=bearer(token), json=body)

        assert (first.status_code, second.status_code) == (201, 201)
        assert first.json()["launch_id"] == second.json()["launch_id"]
        assert len(http.get(LAUNCH, headers=bearer(token)).json()) == 1

    async def test_a_first_evaluation_cannot_name_a_representation(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        world = await build_initial_world(session, "first-names-ir-shop")
        token = await issue_credential(world.merchant_id)
        http = client(settings, factory)
        body = launch_body(http, token, request_key="first-with-ir")
        body["representation_id"] = str(uuid.uuid7())

        refused = http.post(LAUNCH, headers=bearer(token), json=body)

        assert refused.status_code == 422

    async def test_a_page_that_predates_a_publication_is_refused(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        """A merchant reading a first-evaluation page who publishes in another tab.

        The command changed underneath them, so the submit is refused by name rather than
        admitted as a measurement of a storefront they have just stopped presenting.
        """
        world = await build_initial_world(session, "first-superseded-shop")
        token = await issue_credential(world.merchant_id)
        http = client(settings, factory)
        body = launch_body(http, token, request_key="first-superseded")
        compiler = MerchantCompilerService(session)
        compiler_run = await compiler.run(world.merchant_id, world.source_snapshot_id)
        await compiler.publish(world.merchant_id, compiler_run.id)

        refused = http.post(LAUNCH, headers=bearer(token), json=body)

        assert refused.status_code == 409
        assert refused.json()["error"] == "evaluation_purpose_superseded"
        assert http.get(LAUNCH, headers=bearer(token)).json() == []

    async def test_one_request_key_cannot_change_which_evaluation_it_made(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        world = await build_initial_world(session, "first-reused-shop")
        token = await issue_credential(world.merchant_id)
        http = client(settings, factory)
        http.post(
            LAUNCH,
            headers=bearer(token),
            json=launch_body(http, token, request_key="first-reused"),
        )

        refused = http.post(
            LAUNCH,
            headers=bearer(token),
            json={
                "purpose": "REEVALUATION",
                "representation_id": str(uuid.uuid7()),
                "request_key": "first-reused",
                "plan_digest": "sha256:" + "0" * 64,
            },
        )

        assert refused.status_code == 409
        assert refused.json()["error"] == "evaluation_request_key_reused"

    async def test_an_initial_launch_holds_the_one_pending_slot(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        """One pending launch per merchant, whichever kind is pending.

        A merchant owns one benchmark world, so an initial evaluation waiting to run and a
        re-evaluation waiting to run are two runs resetting each other's shelf rather than two
        measurements. Publishing between the two is exactly how a merchant reaches this: the
        pending index does not read the purpose, so the second command is refused for the reason
        that is true rather than admitted as a different kind of launch.
        """
        world = await build_initial_world(session, "first-pending-shop")
        token = await issue_credential(world.merchant_id)
        http = client(settings, factory)
        http.post(
            LAUNCH,
            headers=bearer(token),
            json=launch_body(http, token, request_key="first-pending"),
        )
        compiler = MerchantCompilerService(session)
        compiler_run = await compiler.run(world.merchant_id, world.source_snapshot_id)
        published = await compiler.publish(world.merchant_id, compiler_run.id)

        body = http.get(PREFLIGHT, headers=bearer(token)).json()
        refused = http.post(
            LAUNCH,
            headers=bearer(token),
            json={
                "purpose": "REEVALUATION",
                "representation_id": str(published.id),
                "request_key": "pending-then-reevaluate",
                "plan_digest": body["plan_digest"],
            },
        )

        assert body["purpose"] == "REEVALUATION"
        assert body["launchable"] is False
        assert "evaluation_already_pending" in {blocker["code"] for blocker in body["blockers"]}
        assert body["pending_launch_id"] is not None
        assert refused.status_code == 409
        assert refused.json()["error"] == "evaluation_already_pending"
        assert len(http.get(LAUNCH, headers=bearer(token)).json()) == 1


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
            json=launch_body(
                http,
                token,
                representation_id=str(world.representation_id),
                request_key="model-buyer-key",
            ),
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
        service = MerchantEvaluationLaunchService(session, pinned)
        worker = EvaluationLaunchWorkerService(session)
        for index in range(3):
            launch_id = await queue_launch(
                session, pinned, world, request_key=f"cost-request-{index}"
            )
            # Settled without a run, so the merchant's one pending slot frees for the next.
            await worker.settle_failed(launch_id, failure_code="run_aborted")

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


async def test_a_merchant_can_withdraw_a_queued_evaluation_and_ask_again(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    """The exit from the one state the console could not leave.

    A queued launch waits for a worker an operator configures, and while it waits the merchant
    can neither request another evaluation nor build a new evaluation setup. A deployment with no
    capable worker left them holding a request nothing would run and no way to put it down.
    """
    world = await build_launch_world(session, "withdraw-shop")
    token = await issue_credential(world.merchant_id)
    http = client(settings, factory)

    queued = http.post(
        LAUNCH, headers=bearer(token), json=launch_body(http, token, request_key="withdraw-first")
    ).json()
    assert queued["status"] == "QUEUED"

    withdrawn = http.post(f"{LAUNCH}/{queued['launch_id']}/withdraw", headers=bearer(token))

    assert withdrawn.status_code == 200
    assert withdrawn.json()["status"] == "FAILED"
    assert withdrawn.json()["failure_code"] == "withdrawn_by_merchant"

    # The merchant's one pending slot is free again, so they can ask a second time.
    assert http.get(PREFLIGHT, headers=bearer(token)).json()["launchable"] is True
    second = http.post(
        LAUNCH, headers=bearer(token), json=launch_body(http, token, request_key="withdraw-second")
    )
    assert second.status_code == 201
    assert second.json()["status"] == "QUEUED"

    # Withdrawing something already settled is refused by name rather than done twice.
    repeated = http.post(f"{LAUNCH}/{queued['launch_id']}/withdraw", headers=bearer(token))
    assert repeated.status_code == 409
    assert repeated.json()["error"] == "launch_not_queued"


async def test_another_merchants_launch_cannot_be_withdrawn(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    world = await build_launch_world(session, "withdraw-owner")
    intruder = await build_launch_world(session, "withdraw-intruder")
    token = await issue_credential(world.merchant_id)
    other = await issue_credential(intruder.merchant_id)
    http = client(settings, factory)

    queued = http.post(
        LAUNCH,
        headers=bearer(token),
        json=launch_body(http, token, request_key="withdraw-isolation"),
    ).json()
    refused = http.post(f"{LAUNCH}/{queued['launch_id']}/withdraw", headers=bearer(other))

    assert refused.status_code == 404
