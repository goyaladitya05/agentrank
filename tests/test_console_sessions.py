"""The console's durable browser session, over a real PostgreSQL and the real HTTP surface.

The session moved out of one Next.js process' memory and into a table, so what is asserted here
is the set of properties that move with it: a session resolves from any process, it expires, it
can be closed, it dies with the credential behind it, it names exactly one tenant, and it cannot
mint another session.

Every test uses the real authentication path. A test that fabricated a session row would be a
test that keeps passing after authentication stops working.
"""

import uuid
from datetime import timedelta

import pytest
from conftest import bearer
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentrank_api.auth.console import (
    CONSOLE_SESSION_SCHEME,
    ConsoleSessionService,
    is_console_session_verifier,
)
from agentrank_api.auth.models import MerchantApiCredential, MerchantConsoleSession
from agentrank_api.auth.repository import MerchantCredentialRepository
from agentrank_api.auth.service import MerchantCredentialService
from agentrank_api.auth.tokens import TokenMarker, hash_secret
from agentrank_api.commerce.models import Merchant
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.config import Settings
from agentrank_api.main import create_app
from agentrank_api.payments.fake import FakePaymentProvider

pytestmark = pytest.mark.anyio

SESSIONS = "/api/v1/console/sessions"
CURRENT = "/api/v1/console/sessions/current"

# A console-shaped verifier. The real console derives one by HMAC from its cookie, which is the
# console's own business; what this API requires is the shape, and these are written out so a
# test asserting "a different verifier" is asserting about values the test controls.
FIRST = f"{CONSOLE_SESSION_SCHEME}_{'a1' * 32}"
SECOND = f"{CONSOLE_SESSION_SCHEME}_{'b2' * 32}"


def session_bearer(verifier: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {verifier}"}


async def merchant_with_key(
    session: AsyncSession, slug: str
) -> tuple[Merchant, str, MerchantApiCredential]:
    """A merchant, a real issued key, and the credential row behind it."""
    merchant = await MerchantRepository(session).create(slug=slug, name="Console Shop")
    await session.commit()
    issued = await MerchantCredentialService(session).issue(
        merchant_id=merchant.id, label="console", marker=TokenMarker.DEVELOPMENT
    )
    return merchant, issued.token, issued.credential


def console(settings: Settings, sessions: async_sessionmaker[AsyncSession]) -> TestClient:
    """One console-facing application, sharing the test's engine."""
    app = create_app(settings, payment_provider=FakePaymentProvider())
    app.state.session_factory = sessions
    return TestClient(app)


@pytest.fixture
def client(settings: Settings, factory: async_sessionmaker[AsyncSession]) -> TestClient:
    return console(settings, factory)


class TestOpening:
    async def test_a_merchant_key_opens_a_session_and_the_key_is_not_stored(
        self, client: TestClient, session: AsyncSession
    ) -> None:
        """The one thing the in-process store did that this must not: hold the merchant key."""
        merchant, token, _ = await merchant_with_key(session, "console-open")

        response = client.post(SESSIONS, json={"verifier": FIRST}, headers=bearer(token))

        assert response.status_code == 201
        assert response.json()["merchant_id"] == str(merchant.id)

        record = (await session.execute(select(MerchantConsoleSession))).scalars().one()
        assert record.merchant_id == merchant.id
        assert record.verifier_hash == hash_secret(FIRST)
        # Neither the key nor the verifier is recoverable from what was written.
        stored = str(record.__dict__)
        assert token not in stored
        assert FIRST not in stored

    async def test_the_session_authenticates_an_ordinary_merchant_endpoint(
        self, client: TestClient, session: AsyncSession
    ) -> None:
        _, token, _ = await merchant_with_key(session, "console-authenticates")
        client.post(SESSIONS, json={"verifier": FIRST}, headers=bearer(token))

        response = client.get("/api/v1/insights/runs?limit=1", headers=session_bearer(FIRST))

        assert response.status_code == 200

    async def test_a_verifier_of_the_wrong_shape_is_refused_before_anything_is_written(
        self, client: TestClient, session: AsyncSession
    ) -> None:
        _, token, _ = await merchant_with_key(session, "console-shape")

        for bad in ["", "not-a-verifier", f"{CONSOLE_SESSION_SCHEME}_short", "ar_dev_" + "c" * 32]:
            response = client.post(SESSIONS, json={"verifier": bad}, headers=bearer(token))
            assert response.status_code == 422, bad

        assert (await session.execute(select(MerchantConsoleSession))).scalars().all() == []

    async def test_opening_a_session_needs_a_key_and_refuses_a_session(
        self, client: TestClient, session: AsyncSession
    ) -> None:
        """A session that could mint a session would have no bound on its own lifetime."""
        _, token, _ = await merchant_with_key(session, "console-no-chaining")
        client.post(SESSIONS, json={"verifier": FIRST}, headers=bearer(token))

        chained = client.post(SESSIONS, json={"verifier": SECOND}, headers=session_bearer(FIRST))

        assert chained.status_code == 401
        records = (await session.execute(select(MerchantConsoleSession))).scalars().all()
        assert len(list(records)) == 1

    async def test_an_unknown_key_opens_nothing(
        self, client: TestClient, session: AsyncSession
    ) -> None:
        await merchant_with_key(session, "console-unknown-key")
        invented = f"ar_dev_{uuid.uuid7().hex}_{'d4' * 32}"

        response = client.post(SESSIONS, json={"verifier": FIRST}, headers=bearer(invented))

        assert response.status_code == 401
        assert (await session.execute(select(MerchantConsoleSession))).scalars().all() == []


class TestResolving:
    async def test_a_session_resolves_from_a_second_process(
        self,
        client: TestClient,
        catalog_settings: Settings,
        session: AsyncSession,
    ) -> None:
        """The whole point of the table: a cookie is not addressed to one process.

        A second application is built over the same database with its own engine, its own pool
        and its own connections, which is what a second console instance behind a load balancer
        is. It resolves a session it never saw opened.
        """
        merchant, token, _ = await merchant_with_key(session, "console-second-process")
        client.post(SESSIONS, json={"verifier": FIRST}, headers=bearer(token))

        # Built with settings rather than handed this test's factory, and entered so its lifespan
        # runs. It therefore opens its own engine, its own pool and its own connections against
        # the same database, which is what a second console instance behind a load balancer is.
        elsewhere = create_app(catalog_settings, payment_provider=FakePaymentProvider())
        with TestClient(elsewhere) as other:
            response = other.get(CURRENT, headers=session_bearer(FIRST))
            ordinary = other.get("/api/v1/insights/runs?limit=1", headers=session_bearer(FIRST))

        assert response.status_code == 200
        assert response.json()["merchant_id"] == str(merchant.id)
        assert ordinary.status_code == 200, "and it serves an ordinary merchant screen too"

    async def test_an_unknown_session_is_refused(self, client: TestClient) -> None:
        response = client.get(CURRENT, headers=session_bearer(SECOND))
        assert response.status_code == 401

    async def test_a_malformed_cookie_value_is_refused_without_a_lookup(
        self, client: TestClient
    ) -> None:
        for bad in ["ars_", "ars_zzzz", "garbage", f"{CONSOLE_SESSION_SCHEME}_{'A1' * 32}"]:
            assert not is_console_session_verifier(bad)
            response = client.get(CURRENT, headers=session_bearer(bad))
            assert response.status_code == 401, bad

    async def test_an_expired_session_is_refused(
        self, client: TestClient, session: AsyncSession
    ) -> None:
        """Expiry is decided by the database clock, so this opens one that is already over."""
        merchant, _, credential = await merchant_with_key(session, "console-expired")
        await ConsoleSessionService(session).open(
            merchant_id=merchant.id,
            credential_id=credential.id,
            verifier=FIRST,
            lifetime=timedelta(seconds=-1),
        )

        assert (client.get(CURRENT, headers=session_bearer(FIRST))).status_code == 401
        response = client.get("/api/v1/insights/runs", headers=session_bearer(FIRST))
        assert response.status_code == 401

    async def test_revoking_the_credential_closes_the_sessions_it_opened(
        self, client: TestClient, session: AsyncSession
    ) -> None:
        """A leaked console key has to be revocable, and revoking it has to actually end access."""
        _, token, credential = await merchant_with_key(session, "console-credential-revoked")
        client.post(SESSIONS, json={"verifier": FIRST}, headers=bearer(token))
        assert (client.get(CURRENT, headers=session_bearer(FIRST))).status_code == 200

        credentials = MerchantCredentialRepository(session)
        found = await credentials.get(credential.id)
        assert found is not None
        await credentials.revoke(found)
        await session.commit()

        assert (client.get(CURRENT, headers=session_bearer(FIRST))).status_code == 401


class TestClosing:
    async def test_signing_out_revokes_the_session_that_made_the_request(
        self, client: TestClient, session: AsyncSession
    ) -> None:
        _, token, _ = await merchant_with_key(session, "console-sign-out")
        client.post(SESSIONS, json={"verifier": FIRST}, headers=bearer(token))

        closed = client.delete(CURRENT, headers=session_bearer(FIRST))

        assert closed.status_code == 200
        assert closed.json() == {"revoked": True}
        assert (client.get(CURRENT, headers=session_bearer(FIRST))).status_code == 401
        response = client.get("/api/v1/insights/runs", headers=session_bearer(FIRST))
        assert response.status_code == 401

    async def test_signing_out_twice_is_not_an_error_and_changes_nothing(
        self, client: TestClient, session: AsyncSession
    ) -> None:
        _, token, _ = await merchant_with_key(session, "console-sign-out-twice")
        client.post(SESSIONS, json={"verifier": FIRST}, headers=bearer(token))
        client.delete(CURRENT, headers=session_bearer(FIRST))

        again = client.delete(CURRENT, headers=session_bearer(FIRST))

        assert again.status_code == 401, "a closed session no longer authenticates anything"

    async def test_one_browser_signing_out_leaves_the_others_open(
        self, client: TestClient, session: AsyncSession
    ) -> None:
        _, token, _ = await merchant_with_key(session, "console-two-browsers")
        client.post(SESSIONS, json={"verifier": FIRST}, headers=bearer(token))
        client.post(SESSIONS, json={"verifier": SECOND}, headers=bearer(token))

        client.delete(CURRENT, headers=session_bearer(FIRST))

        assert (client.get(CURRENT, headers=session_bearer(FIRST))).status_code == 401
        assert (client.get(CURRENT, headers=session_bearer(SECOND))).status_code == 200


class TestTenancy:
    async def test_a_session_names_one_merchant_and_the_browser_cannot_choose_it(
        self, client: TestClient, session: AsyncSession
    ) -> None:
        """There is no merchant field to send, and the credential decides."""
        first, first_token, _ = await merchant_with_key(session, "console-tenant-one")
        second, _, _ = await merchant_with_key(session, "console-tenant-two")

        opened = client.post(
            SESSIONS,
            json={"verifier": FIRST, "merchant_id": str(second.id)},
            headers=bearer(first_token),
        )

        assert opened.status_code == 422, "an invented field is refused rather than ignored"

        opened = client.post(SESSIONS, json={"verifier": FIRST}, headers=bearer(first_token))
        assert opened.json()["merchant_id"] == str(first.id)
        assert opened.json()["merchant_id"] != str(second.id)

    async def test_a_session_cannot_be_moved_to_another_merchant(
        self, session: AsyncSession
    ) -> None:
        """The database refuses it, so no service anywhere has to remember not to."""
        first, _, credential = await merchant_with_key(session, "console-immutable-one")
        second, _, _ = await merchant_with_key(session, "console-immutable-two")
        record = await ConsoleSessionService(session).open(
            merchant_id=first.id, credential_id=credential.id, verifier=FIRST
        )
        record_id = record.id

        with pytest.raises(Exception) as refused:
            await session.execute(
                text("UPDATE merchant_console_session SET merchant_id = :other WHERE id = :id"),
                {"other": second.id, "id": record_id},
            )
        await session.rollback()
        assert "immutable" in str(refused.value)

    async def test_a_session_cannot_be_given_a_later_expiry(self, session: AsyncSession) -> None:
        merchant, _, credential = await merchant_with_key(session, "console-no-extension")
        record = await ConsoleSessionService(session).open(
            merchant_id=merchant.id, credential_id=credential.id, verifier=FIRST
        )
        record_id = record.id

        with pytest.raises(Exception) as refused:
            await session.execute(
                text(
                    "UPDATE merchant_console_session"
                    " SET expires_at = expires_at + interval '1 year' WHERE id = :id"
                ),
                {"id": record_id},
            )
        await session.rollback()
        assert "immutable" in str(refused.value)

    async def test_a_revoked_session_cannot_be_reopened(self, session: AsyncSession) -> None:
        merchant, _, credential = await merchant_with_key(session, "console-no-reopen")
        service = ConsoleSessionService(session)
        await service.open(merchant_id=merchant.id, credential_id=credential.id, verifier=FIRST)
        assert await service.revoke(FIRST) is True

        with pytest.raises(Exception) as refused:
            await session.execute(text("UPDATE merchant_console_session SET revoked_at = NULL"))
        await session.rollback()
        assert "reopened" in str(refused.value)


class TestCleanup:
    async def test_only_settled_sessions_old_enough_to_be_uninteresting_are_deleted(
        self, session: AsyncSession, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """An open session is never touched, and a recently closed one stays readable."""
        merchant, _, credential = await merchant_with_key(session, "console-cleanup")
        service = ConsoleSessionService(session)
        open_session = await service.open(
            merchant_id=merchant.id, credential_id=credential.id, verifier=FIRST
        )
        open_id = open_session.id
        long_expired = await service.open(
            merchant_id=merchant.id,
            credential_id=credential.id,
            verifier=SECOND,
            lifetime=timedelta(days=-30),
        )
        long_expired_id = long_expired.id

        removed = await service.purge_settled(older_than=timedelta(days=7), limit=100)

        assert removed == 1
        remaining = [
            row.id
            for row in (await session.execute(select(MerchantConsoleSession))).scalars().all()
        ]
        assert remaining == [open_id]
        assert long_expired_id not in remaining

    async def test_cleanup_is_bounded_by_its_limit(self, session: AsyncSession) -> None:
        merchant, _, credential = await merchant_with_key(session, "console-cleanup-bound")
        service = ConsoleSessionService(session)
        for index in range(4):
            await service.open(
                merchant_id=merchant.id,
                credential_id=credential.id,
                verifier=f"{CONSOLE_SESSION_SCHEME}_{index:064x}",
                lifetime=timedelta(days=-30),
            )

        assert await service.purge_settled(older_than=timedelta(days=7), limit=2) == 2
        assert await service.purge_settled(older_than=timedelta(days=7), limit=100) == 2
        assert await service.purge_settled(older_than=timedelta(days=7), limit=100) == 0
