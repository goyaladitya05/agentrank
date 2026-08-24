"""The before and after a merchant reads after a re-evaluation, over the real stack.

Two real runs, dispatched by the real worker against a real world, then compared through the
real HTTP surface. What is asserted is what a merchant is actually told: that the first launch
has nothing to compare against and says so with a null rather than an empty panel, that the
second names the run it was measured against, and that the reading arrives with the caveats that
stop it being read as an effect.
"""

import uuid

import pytest
from conftest import CredentialIssuer, bearer
from fastapi.testclient import TestClient
from reevaluation_support import LaunchWorld, build_launch_world, queue_launch, without_providers
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentrank_api.benchmark.dispatch import execute_next_reevaluation
from agentrank_api.config import Settings
from agentrank_api.main import create_app
from agentrank_api.payments.fake import FakePaymentProvider

pytestmark = pytest.mark.anyio

LAUNCH = "/api/v1/benchmark/re-evaluations"


def client(settings: Settings, sessions: async_sessionmaker[AsyncSession]) -> TestClient:
    app = create_app(without_providers(settings), payment_provider=FakePaymentProvider())
    app.state.session_factory = sessions
    return TestClient(app)


async def launch_and_execute(
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    world: LaunchWorld,
    *,
    request_key: str,
) -> uuid.UUID:
    """One admitted launch carried all the way to a completed run."""
    launch_id = await queue_launch(session, settings, world, request_key=request_key)
    outcome = await execute_next_reevaluation(
        session,
        factory,
        world=world.authored,
        provider=FakePaymentProvider(),
        settings=settings,
    )
    assert outcome is not None
    assert outcome.status == "COMPLETED"
    return launch_id


class TestComparisonAvailability:
    async def test_a_first_launch_has_nothing_to_compare_against(
        self,
        catalog_settings: Settings,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        world = await build_launch_world(session, "first-compare-shop")
        merchant_id = world.merchant_id
        launch_id = await launch_and_execute(
            session,
            factory,
            without_providers(catalog_settings),
            world,
            request_key="first-compare",
        )
        token = await issue_credential(merchant_id)
        http = client(settings, factory)

        body = http.get(f"{LAUNCH}/{launch_id}", headers=bearer(token)).json()

        assert body["status"] == "COMPLETED"
        assert body["baseline_run_id"] is None
        # Null rather than an empty comparison, which would read as "nothing changed".
        assert body["comparison"] is None

    async def test_a_second_launch_is_read_against_the_first(
        self,
        catalog_settings: Settings,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        pinned = without_providers(catalog_settings)
        world = await build_launch_world(session, "second-compare-shop")
        merchant_id = world.merchant_id
        first = await launch_and_execute(session, factory, pinned, world, request_key="compare-one")
        second = await launch_and_execute(
            session, factory, pinned, world, request_key="compare-two"
        )
        token = await issue_credential(merchant_id)
        http = client(settings, factory)

        earlier = http.get(f"{LAUNCH}/{first}", headers=bearer(token)).json()
        later = http.get(f"{LAUNCH}/{second}", headers=bearer(token)).json()

        assert later["baseline_run_id"] == earlier["run_id"]
        comparison = later["comparison"]
        assert comparison is not None
        assert comparison["baseline_run_id"] == earlier["run_id"]
        assert comparison["candidate_run_id"] == later["run_id"]
        assert comparison["comparable"] is True

        # The deterministic buyer against the same prepared world twice: nothing moved, and the
        # conclusion says what that is and is not.
        assert comparison["conclusion"]["kind"] == "PARITY"
        assert "not evidence" in comparison["conclusion"]["statement"]

        warnings = {warning["code"] for warning in comparison["warnings"]}
        assert "NOT_A_CONTROLLED_EXPERIMENT" in warnings
        assert "SMALL_SAMPLE" in warnings

        counts = {change["key"]: change for change in comparison["counts"]}
        assert counts["missions_total"]["before"] == counts["missions_total"]["after"]
        assert counts["unsafe_completions"]["delta"] == 0

        # Simulated demand is per currency, labelled simulated at every field, never summed.
        assert comparison["simulated_demand"]
        for change in comparison["simulated_demand"]:
            assert change["currency"]
            assert "simulated_before_amount_minor" in change
            assert "simulated_delta_amount_minor" in change

    async def test_another_merchant_cannot_read_the_comparison(
        self,
        catalog_settings: Settings,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        pinned = without_providers(catalog_settings)
        mine = await build_launch_world(session, "owned-compare-shop")
        theirs = await build_launch_world(session, "other-compare-shop")
        launch_id = await launch_and_execute(
            session, factory, pinned, mine, request_key="owned-compare"
        )
        foreign = await issue_credential(theirs.merchant_id)
        http = client(settings, factory)

        response = http.get(f"{LAUNCH}/{launch_id}", headers=bearer(foreign))

        assert response.status_code == 404
