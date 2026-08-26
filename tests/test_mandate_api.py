"""HTTP behavior of the mandate endpoints.

Deliberately thin. Workflow and transaction behavior are asserted once at the service
level; what is checked here is the wire contract.

Every request here carries a merchant API key, because every one of these routes requires one.
Which merchant a request acts for is the credential's and is not in any body, which is why
`creation_body` no longer takes a merchant. Cross merchant behavior is
`tests/test_mandate_authorization_scope.py`.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from conftest import CredentialIssuer, bearer
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.config import Settings
from agentrank_api.main import create_app

pytestmark = pytest.mark.anyio

MANDATES_URL = "/api/v1/commerce/mandates"
NOW = datetime.now(UTC)
HOUR = timedelta(hours=1)


@pytest.fixture
async def merchant_id(session: AsyncSession) -> uuid.UUID:
    merchant = await MerchantRepository(session).create(slug="ampere-supply", name="Ampere Supply")
    await session.commit()
    return merchant.id


@pytest.fixture
async def token(issue_credential: CredentialIssuer, merchant_id: uuid.UUID) -> str:
    return await issue_credential(merchant_id)


def creation_body(**overrides: object) -> dict[str, object]:
    return {
        "max_total_amount_minor": 500000,
        "currency": "INR",
        "max_quantity": 1,
        "valid_until": (NOW + HOUR).isoformat(),
    } | overrides


async def test_a_mandate_is_created_and_can_be_read_back(
    catalog_settings: Settings, merchant_id: uuid.UUID, token: str
) -> None:
    with TestClient(create_app(catalog_settings), headers=bearer(token)) as client:
        created = client.post(MANDATES_URL, json=creation_body())
        assert created.status_code == 201
        body = created.json()
        assert body["max_total_amount_minor"] == 500000
        assert body["currency"] == "INR"
        assert body["status"] == "ACTIVE"
        assert body["revoked_at"] is None
        # The merchant came from the credential and from nowhere else.
        assert body["merchant_id"] == str(merchant_id)

        fetched = client.get(f"{MANDATES_URL}/{body['id']}")
        assert fetched.status_code == 200
        assert fetched.json() == body

        usable = client.get(f"{MANDATES_URL}/{body['id']}/validation")
        assert usable.json() == {"valid": True, "violations": []}


async def test_revoking_is_idempotent_over_http(catalog_settings: Settings, token: str) -> None:
    with TestClient(create_app(catalog_settings), headers=bearer(token)) as client:
        mandate_id = client.post(MANDATES_URL, json=creation_body()).json()["id"]

        first = client.post(f"{MANDATES_URL}/{mandate_id}/revoke")
        assert first.status_code == 200
        assert first.json()["status"] == "REVOKED"
        assert first.json()["revoked_at"] is not None

        second = client.post(f"{MANDATES_URL}/{mandate_id}/revoke")
        assert second.json() == first.json()

        validation = client.get(f"{MANDATES_URL}/{mandate_id}/validation")
        assert validation.json() == {"valid": False, "violations": ["MANDATE_NOT_ACTIVE"]}


async def test_an_intent_may_accompany_a_mandate_and_is_type_checked(
    catalog_settings: Settings, token: str
) -> None:
    intent = {
        "description": "One 100W USB-C charger",
        "hard_constraints": [
            {"kind": "max_total_amount", "amount_minor": 500000, "currency": "INR"}
        ],
        "preferences": ["prefer next day delivery"],
    }
    with TestClient(create_app(catalog_settings), headers=bearer(token)) as client:
        accepted = client.post(MANDATES_URL, json=creation_body(intent=intent))
        assert accepted.status_code == 201

        unknown_kind = dict(intent, hard_constraints=[{"kind": "vibes", "value": "good"}])
        refused = client.post(MANDATES_URL, json=creation_body(intent=unknown_kind))
        assert refused.status_code == 422


async def test_an_unknown_mandate_is_a_structured_404(
    catalog_settings: Settings, token: str
) -> None:
    missing = uuid.uuid7()
    with TestClient(create_app(catalog_settings), headers=bearer(token)) as client:
        response = client.get(f"{MANDATES_URL}/{missing}")

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"
    assert response.json()["resource"] == "mandate"
    assert response.json()["identifier"] == str(missing)


async def test_a_merchant_named_in_the_body_is_refused(
    catalog_settings: Settings, session: AsyncSession, merchant_id: uuid.UUID, token: str
) -> None:
    """The field is gone, and a caller who sends it anyway is refused rather than ignored.

    A caller who tries to choose their own tenant and is answered 201 has been told yes. The
    mandate would have been written against the authenticated merchant either way, so nothing
    was ever authorized wrongly, and a reader of the old API had every reason to keep sending it.
    """
    other = await MerchantRepository(session).create(slug="volt-works", name="Volt")
    await session.commit()

    with TestClient(create_app(catalog_settings), headers=bearer(token)) as client:
        created = client.post(MANDATES_URL, json=creation_body(merchant_id=str(other.id)))

    assert created.status_code == 422
    assert created.json()["error"] == "invalid_request"
    assert str(merchant_id) not in created.text


async def test_malformed_authorizations_are_refused_with_422(
    catalog_settings: Settings, token: str
) -> None:
    """Each of these would otherwise reach the database and come back as a 500."""
    with TestClient(create_app(catalog_settings), headers=bearer(token)) as client:
        inverted = client.post(
            MANDATES_URL,
            json=creation_body(
                valid_from=(NOW + HOUR).isoformat(),
                valid_until=NOW.isoformat(),
            ),
        )
        assert inverted.status_code == 422
        assert "valid_until" in inverted.text

        assert (
            client.post(MANDATES_URL, json=creation_body(max_total_amount_minor=-1)).status_code
            == 422
        )
        assert client.post(MANDATES_URL, json=creation_body(currency="inr")).status_code == 422
        assert client.post(MANDATES_URL, json=creation_body(max_quantity=0)).status_code == 422
