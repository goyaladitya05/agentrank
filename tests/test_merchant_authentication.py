"""The HTTP authentication boundary: what `require_merchant` accepts, refuses and publishes.

The dependency is exercised against a probe application rather than against the real routes,
and that is deliberate. What is under test here is the boundary itself: the ways a request can
fail to identify itself, the shape of the refusal, and what the generated schema says about it.
Whether a particular route is behind it, and whether a route that is behind it scopes its
queries, are separate claims tested against the real application in the scoping tests beside
this file.

The probe is three lines and it uses the real dependency, the real error and the real handlers.
Nothing about authentication is reimplemented for it.

The property this file exists to pin: every way of failing to authenticate produces one
response. A caller must not be able to learn from a 401 whether a credential identifier exists,
whether a key was revoked, or whether a secret was merely wrong.
"""

import logging
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.auth.principal import AuthenticatedMerchant
from agentrank_api.auth.service import MerchantCredentialService
from agentrank_api.auth.tokens import TokenMarker, format_token, generate_secret
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.config import Settings
from agentrank_api.dependencies import MerchantDep
from agentrank_api.main import create_app

pytestmark = pytest.mark.anyio

DEV = TokenMarker.DEVELOPMENT
PROBE = "/probe"


def probe_app(settings: Settings) -> FastAPI:
    """The real application with one extra route that reports who the caller is.

    Built on `create_app` so that the error handler, the dependency and the OpenAPI generation
    are the ones that ship. The route exists only to have something behind the dependency that
    does nothing else, so a failure here is a failure of authentication rather than of whatever
    a real route would have gone on to do.
    """
    app = create_app(settings)

    @app.get(PROBE)
    async def who(merchant: MerchantDep) -> dict[str, str]:
        return {
            "merchant_id": str(merchant.merchant_id),
            "credential_id": str(merchant.credential_id),
        }

    return app


@pytest.fixture
async def issued(session: AsyncSession) -> dict[str, str]:
    """One merchant holding one credential."""
    merchant = await MerchantRepository(session).create(slug="ampere-supply", name="Ampere")
    await session.commit()
    credential = await MerchantCredentialService(session).issue(
        merchant_id=merchant.id, label="probe", marker=DEV
    )
    return {
        "merchant_id": str(merchant.id),
        "credential_id": str(credential.credential.id),
        "token": credential.token,
    }


def refusal(response: Response) -> None:
    """Every 401 in this application is this response and nothing else."""
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json() == {
        "error": "unauthenticated",
        "detail": "a valid merchant API credential is required",
        "resource": None,
        "identifier": None,
    }


async def test_a_valid_credential_identifies_its_merchant(
    catalog_settings: Settings, issued: dict[str, str]
) -> None:
    with TestClient(probe_app(catalog_settings)) as client:
        response = client.get(PROBE, headers={"Authorization": f"Bearer {issued['token']}"})

    assert response.status_code == 200
    assert response.json() == {
        "merchant_id": issued["merchant_id"],
        "credential_id": issued["credential_id"],
    }


async def test_a_request_with_no_credential_is_refused(
    catalog_settings: Settings, issued: dict[str, str]
) -> None:
    """401 and not 403. There is no caller here to be forbidden."""
    with TestClient(probe_app(catalog_settings)) as client:
        refusal(client.get(PROBE))


@pytest.mark.parametrize(
    "header",
    [
        "",
        "Bearer",
        "Bearer ",
        "Basic YWJjOmRlZg==",
        "Token ar_dev_x_y",
        "Bearer not-a-token",
        "Bearer ar_dev_short_short",
        # The scheme is case insensitive by the specification, and the token still is not one.
        "bearer garbage",
    ],
)
async def test_a_credential_that_is_not_one_is_refused(
    catalog_settings: Settings, issued: dict[str, str], header: str
) -> None:
    with TestClient(probe_app(catalog_settings)) as client:
        refusal(client.get(PROBE, headers={"Authorization": header}))


async def test_an_unknown_credential_is_refused(
    catalog_settings: Settings, issued: dict[str, str]
) -> None:
    """Well formed, and names a credential that was never issued."""
    unissued = format_token(uuid.uuid7(), generate_secret(), marker=DEV)

    with TestClient(probe_app(catalog_settings)) as client:
        refusal(client.get(PROBE, headers={"Authorization": f"Bearer {unissued}"}))


async def test_a_real_identifier_with_the_wrong_secret_is_refused(
    catalog_settings: Settings, issued: dict[str, str]
) -> None:
    forged = format_token(uuid.UUID(issued["credential_id"]), generate_secret(), marker=DEV)

    with TestClient(probe_app(catalog_settings)) as client:
        refusal(client.get(PROBE, headers={"Authorization": f"Bearer {forged}"}))


async def test_a_revoked_credential_stops_authenticating_immediately(
    catalog_settings: Settings, session: AsyncSession, issued: dict[str, str]
) -> None:
    """No cache and no window. The revocation condition is in the authentication query."""
    with TestClient(probe_app(catalog_settings)) as client:
        header = {"Authorization": f"Bearer {issued['token']}"}
        assert client.get(PROBE, headers=header).status_code == 200

        await MerchantCredentialService(session).revoke(uuid.UUID(issued["credential_id"]))

        refusal(client.get(PROBE, headers=header))


async def test_revoking_one_credential_leaves_the_others_working(
    catalog_settings: Settings, session: AsyncSession, issued: dict[str, str]
) -> None:
    """Rotation over HTTP, which is the reason a merchant may hold several credentials."""
    service = MerchantCredentialService(session)
    second = await service.issue(
        merchant_id=uuid.UUID(issued["merchant_id"]), label="rotated", marker=DEV
    )

    with TestClient(probe_app(catalog_settings)) as client:
        first_header = {"Authorization": f"Bearer {issued['token']}"}
        second_header = {"Authorization": f"Bearer {second.token}"}
        assert client.get(PROBE, headers=first_header).status_code == 200
        assert client.get(PROBE, headers=second_header).status_code == 200

        await service.revoke(uuid.UUID(issued["credential_id"]))

        refusal(client.get(PROBE, headers=first_header))
        assert client.get(PROBE, headers=second_header).status_code == 200


async def test_every_way_of_failing_produces_one_indistinguishable_answer(
    catalog_settings: Settings, session: AsyncSession, issued: dict[str, str]
) -> None:
    """The claim stated once, across all of the ways it can be broken.

    Asserted as a set of whole responses rather than per case, because the failure this guards
    against is one branch answering differently, and that is only visible when the answers are
    compared with each other.
    """
    revoked = await MerchantCredentialService(session).issue(
        merchant_id=uuid.UUID(issued["merchant_id"]), label="withdrawn", marker=DEV
    )
    await MerchantCredentialService(session).revoke(revoked.credential.id)
    wrong_secret = format_token(uuid.UUID(issued["credential_id"]), generate_secret(), marker=DEV)
    unissued = format_token(uuid.uuid7(), generate_secret(), marker=DEV)

    with TestClient(probe_app(catalog_settings)) as client:
        answers = [
            client.get(PROBE),
            client.get(PROBE, headers={"Authorization": "Bearer garbage"}),
            client.get(PROBE, headers={"Authorization": f"Bearer {unissued}"}),
            client.get(PROBE, headers={"Authorization": f"Bearer {wrong_secret}"}),
            client.get(PROBE, headers={"Authorization": f"Bearer {revoked.token}"}),
        ]

    assert {answer.status_code for answer in answers} == {401}
    assert len({answer.text for answer in answers}) == 1
    for answer in answers:
        refusal(answer)


async def test_no_refusal_echoes_anything_the_caller_sent(
    catalog_settings: Settings, issued: dict[str, str]
) -> None:
    """A 401 body that quoted the token would put a secret in whatever logs the response."""
    token = issued["token"]

    with TestClient(probe_app(catalog_settings)) as client:
        response = client.get(PROBE, headers={"Authorization": f"Bearer {token}xx"})

    assert token not in response.text
    assert issued["credential_id"] not in response.text
    assert issued["merchant_id"] not in response.text


async def test_a_credential_never_reaches_the_logs(
    catalog_settings: Settings, issued: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    """Both halves: a request that succeeds and one that fails.

    A token in a log file is a token an attacker reads without touching the database, and the
    failing case is the one a careless implementation logs, because a failure feels like
    something worth recording.
    """
    token = issued["token"]

    with caplog.at_level(logging.DEBUG), TestClient(probe_app(catalog_settings)) as client:
        client.get(PROBE, headers={"Authorization": f"Bearer {token}"})
        client.get(PROBE, headers={"Authorization": f"Bearer {token}broken"})

    assert token not in caplog.text
    assert "Authorization" not in caplog.text


async def test_the_principal_is_immutable(catalog_settings: Settings) -> None:
    """A route that could rewrite its own merchant identifier would be a route with none."""
    principal = AuthenticatedMerchant(merchant_id=uuid.uuid7(), credential_id=uuid.uuid7())

    with pytest.raises(AttributeError):
        principal.merchant_id = uuid.uuid7()  # type: ignore[misc]


def test_the_schema_publishes_one_bearer_scheme(catalog_settings: Settings) -> None:
    """An API whose schema does not say it needs a credential cannot be generated a client for."""
    schema = probe_app(catalog_settings).openapi()

    schemes = schema["components"]["securitySchemes"]
    assert schemes["MerchantApiKey"]["type"] == "http"
    assert schemes["MerchantApiKey"]["scheme"] == "bearer"


def test_a_protected_operation_declares_that_it_needs_the_scheme(
    catalog_settings: Settings,
) -> None:
    schema = probe_app(catalog_settings).openapi()

    assert schema["paths"][PROBE]["get"]["security"] == [{"MerchantApiKey": []}]


def test_health_and_readiness_declare_no_security(catalog_settings: Settings) -> None:
    """They answer about the process, they expose no merchant data, and they stay public.

    An orchestrator that had to hold a merchant credential to ask whether the process is alive
    would be an orchestrator that stops probing when a credential is rotated.
    """
    schema = probe_app(catalog_settings).openapi()

    assert "security" not in schema["paths"]["/health"]["get"]
    assert "security" not in schema["paths"]["/ready"]["get"]


async def test_health_and_readiness_answer_without_a_credential(
    catalog_settings: Settings,
) -> None:
    with TestClient(probe_app(catalog_settings)) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 200
        assert client.get("/ready").json()["status"] == "ready"
