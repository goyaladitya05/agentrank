"""HTTP behavior of the mandate endpoints.

Deliberately thin. Workflow and transaction behavior are asserted once at the service
level; what is checked here is the wire contract.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
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


def creation_body(merchant_id: uuid.UUID, **overrides: object) -> dict[str, object]:
    return {
        "merchant_id": str(merchant_id),
        "max_total_amount_minor": 500000,
        "currency": "INR",
        "max_quantity": 1,
        "valid_until": (NOW + HOUR).isoformat(),
    } | overrides


async def test_a_mandate_is_created_and_can_be_read_back(
    catalog_settings: Settings, merchant_id: uuid.UUID
) -> None:
    with TestClient(create_app(catalog_settings)) as client:
        created = client.post(MANDATES_URL, json=creation_body(merchant_id))
        assert created.status_code == 201
        body = created.json()
        assert body["max_total_amount_minor"] == 500000
        assert body["currency"] == "INR"
        assert body["status"] == "ACTIVE"
        assert body["revoked_at"] is None

        fetched = client.get(f"{MANDATES_URL}/{body['id']}")
        assert fetched.status_code == 200
        assert fetched.json() == body

        usable = client.get(f"{MANDATES_URL}/{body['id']}/validation")
        assert usable.json() == {"valid": True, "violations": []}


async def test_revoking_is_idempotent_over_http(
    catalog_settings: Settings, merchant_id: uuid.UUID
) -> None:
    with TestClient(create_app(catalog_settings)) as client:
        mandate_id = client.post(MANDATES_URL, json=creation_body(merchant_id)).json()["id"]

        first = client.post(f"{MANDATES_URL}/{mandate_id}/revoke")
        assert first.status_code == 200
        assert first.json()["status"] == "REVOKED"
        assert first.json()["revoked_at"] is not None

        second = client.post(f"{MANDATES_URL}/{mandate_id}/revoke")
        assert second.json() == first.json()

        validation = client.get(f"{MANDATES_URL}/{mandate_id}/validation")
        assert validation.json() == {"valid": False, "violations": ["MANDATE_NOT_ACTIVE"]}


async def test_an_intent_may_accompany_a_mandate_and_is_type_checked(
    catalog_settings: Settings, merchant_id: uuid.UUID
) -> None:
    intent = {
        "description": "One 100W USB-C charger",
        "hard_constraints": [
            {"kind": "max_total_amount", "amount_minor": 500000, "currency": "INR"}
        ],
        "preferences": ["prefer next day delivery"],
    }
    with TestClient(create_app(catalog_settings)) as client:
        accepted = client.post(MANDATES_URL, json=creation_body(merchant_id, intent=intent))
        assert accepted.status_code == 201

        unknown_kind = dict(intent, hard_constraints=[{"kind": "vibes", "value": "good"}])
        refused = client.post(MANDATES_URL, json=creation_body(merchant_id, intent=unknown_kind))
        assert refused.status_code == 422


async def test_an_unknown_mandate_is_a_structured_404(catalog_settings: Settings) -> None:
    missing = uuid.uuid7()
    with TestClient(create_app(catalog_settings)) as client:
        response = client.get(f"{MANDATES_URL}/{missing}")

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"
    assert response.json()["resource"] == "mandate"
    assert response.json()["identifier"] == str(missing)


async def test_a_mandate_for_an_unknown_merchant_is_a_structured_404(
    catalog_settings: Settings,
) -> None:
    with TestClient(create_app(catalog_settings)) as client:
        response = client.post(MANDATES_URL, json=creation_body(uuid.uuid7()))

    assert response.status_code == 404
    assert response.json()["resource"] == "merchant"


async def test_malformed_authorizations_are_refused_with_422(
    catalog_settings: Settings, merchant_id: uuid.UUID
) -> None:
    """Each of these would otherwise reach the database and come back as a 500."""
    with TestClient(create_app(catalog_settings)) as client:
        inverted = client.post(
            MANDATES_URL,
            json=creation_body(
                merchant_id,
                valid_from=(NOW + HOUR).isoformat(),
                valid_until=NOW.isoformat(),
            ),
        )
        assert inverted.status_code == 422
        assert "valid_until" in inverted.text

        assert (
            client.post(
                MANDATES_URL, json=creation_body(merchant_id, max_total_amount_minor=-1)
            ).status_code
            == 422
        )
        assert (
            client.post(MANDATES_URL, json=creation_body(merchant_id, currency="inr")).status_code
            == 422
        )
        assert (
            client.post(MANDATES_URL, json=creation_body(merchant_id, max_quantity=0)).status_code
            == 422
        )
