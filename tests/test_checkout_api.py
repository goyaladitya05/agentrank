"""HTTP behavior of the checkout endpoints.

Deliberately thin. Pricing, refusals and the authorization rules are asserted once at the
service and domain levels; what is checked here is the wire contract.

Every request here carries a merchant API key, because every one of these routes requires one.
The merchant is the credential's and is not in any body, which is why `creation_body` no longer
sends one. Cross merchant behavior is `tests/test_checkout_authorization_scope.py`.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from conftest import CredentialIssuer, bearer
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.commerce.repository import CatalogRepository, MerchantRepository
from agentrank_api.config import Settings
from agentrank_api.main import create_app
from agentrank_api.mandates.repository import MandateRepository

pytestmark = pytest.mark.anyio

CHECKOUTS_URL = "/api/v1/commerce/checkouts"
NOW = datetime.now(UTC)
HOUR = timedelta(hours=1)
PRICE = 499900


@pytest.fixture
async def quotable(session: AsyncSession) -> dict[str, uuid.UUID]:
    """A merchant with a generous mandate and one variant with two units in stock."""
    merchant = await MerchantRepository(session).create(slug="ampere-supply", name="Ampere Supply")
    mandate = await MandateRepository(session).create(
        merchant_id=merchant.id,
        max_total_amount_minor=PRICE,
        currency="INR",
        valid_from=NOW,
        valid_until=NOW + HOUR,
    )
    catalog = CatalogRepository(session)
    product = await catalog.create_product(
        merchant_id=merchant.id, external_id="amp-1", title="Charger", category="chargers"
    )
    variant = await catalog.create_variant(
        product=product,
        sku="AMP-CHG",
        price_amount_minor=PRICE,
        currency="INR",
        inventory_quantity=2,
        attributes={"color": "black", "wattage": 100},
    )
    await session.commit()
    return {"merchant_id": merchant.id, "mandate_id": mandate.id, "variant_id": variant.id}


@pytest.fixture
async def token(issue_credential: CredentialIssuer, quotable: dict[str, uuid.UUID]) -> str:
    """A key for the merchant above. Every route in this file requires one."""
    return await issue_credential(quotable["merchant_id"])


def creation_body(
    ids: dict[str, uuid.UUID], quantity: int = 1, **overrides: object
) -> dict[str, object]:
    return {
        "mandate_id": str(ids["mandate_id"]),
        "items": [{"variant_id": str(ids["variant_id"]), "quantity": quantity}],
    } | overrides


async def test_a_checkout_is_created_read_back_and_authorized(
    catalog_settings: Settings, quotable: dict[str, uuid.UUID], token: str
) -> None:
    with TestClient(create_app(catalog_settings), headers=bearer(token)) as client:
        created = client.post(CHECKOUTS_URL, json=creation_body(quotable))
        assert created.status_code == 201
        body = created.json()
        assert body["currency"] == "INR"
        assert body["subtotal_amount_minor"] == PRICE
        assert body["shipping_amount_minor"] == 0
        assert body["discount_amount_minor"] == 0
        assert body["total_amount_minor"] == PRICE
        assert body["total_quantity"] == 1
        assert body["status"] == "OPEN"
        assert body["cancelled_at"] is None
        # The whole snapshot is in the response, price and description alike, so nothing
        # has to reread the variant to understand what was offered.
        assert body["lines"] == [
            {
                "id": body["lines"][0]["id"],
                "variant_id": str(quotable["variant_id"]),
                "quantity": 1,
                "unit_price_amount_minor": PRICE,
                "line_amount_minor": PRICE,
                "currency": "INR",
                "product_category": "chargers",
                "variant_attributes": {"color": "black", "wattage": 100},
            }
        ]

        fetched = client.get(f"{CHECKOUTS_URL}/{body['id']}")
        assert fetched.status_code == 200
        assert fetched.json() == body

        allowed = client.get(f"{CHECKOUTS_URL}/{body['id']}/authorization")
        assert allowed.json() == {"allowed": True, "violations": []}


async def test_a_quote_above_the_ceiling_is_created_and_then_denied(
    catalog_settings: Settings, quotable: dict[str, uuid.UUID], token: str
) -> None:
    """The graceful failure shape, over HTTP: 201 for the quote, denied for the money."""
    with TestClient(create_app(catalog_settings), headers=bearer(token)) as client:
        created = client.post(CHECKOUTS_URL, json=creation_body(quotable, quantity=2))
        assert created.status_code == 201
        assert created.json()["total_amount_minor"] == 2 * PRICE

        decision = client.get(f"{CHECKOUTS_URL}/{created.json()['id']}/authorization")
        assert decision.status_code == 200
        assert decision.json() == {"allowed": False, "violations": ["MAX_TOTAL_EXCEEDED"]}


async def test_cancelling_is_idempotent_and_denies_the_checkout(
    catalog_settings: Settings, quotable: dict[str, uuid.UUID], token: str
) -> None:
    with TestClient(create_app(catalog_settings), headers=bearer(token)) as client:
        checkout_id = client.post(CHECKOUTS_URL, json=creation_body(quotable)).json()["id"]

        first = client.post(f"{CHECKOUTS_URL}/{checkout_id}/cancel")
        assert first.status_code == 200
        assert first.json()["status"] == "CANCELLED"

        again = client.post(f"{CHECKOUTS_URL}/{checkout_id}/cancel")
        assert again.json()["cancelled_at"] == first.json()["cancelled_at"]

        decision = client.get(f"{CHECKOUTS_URL}/{checkout_id}/authorization")
        assert decision.json() == {"allowed": False, "violations": ["CHECKOUT_NOT_OPEN"]}


async def test_an_unknown_checkout_gives_a_structured_404(
    catalog_settings: Settings, token: str
) -> None:
    with TestClient(create_app(catalog_settings), headers=bearer(token)) as client:
        missing = uuid.uuid7()
        response = client.get(f"{CHECKOUTS_URL}/{missing}")

        assert response.status_code == 404
        assert response.json() == {
            "error": "not_found",
            "detail": f"checkout {missing} was not found",
            "resource": "checkout",
            "identifier": str(missing),
        }


async def test_state_refusals_answer_409_and_name_the_reason(
    catalog_settings: Settings, quotable: dict[str, uuid.UUID], token: str
) -> None:
    """409 rather than 422: the request is well formed and the shelf is simply short."""
    with TestClient(create_app(catalog_settings), headers=bearer(token)) as client:
        response = client.post(CHECKOUTS_URL, json=creation_body(quotable, quantity=3))

        assert response.status_code == 409
        body = response.json()
        assert body["error"] == "insufficient_inventory"
        assert body["resource"] == "variant"
        assert body["identifier"] == str(quotable["variant_id"])


async def test_malformed_creation_requests_answer_422(
    catalog_settings: Settings, quotable: dict[str, uuid.UUID], token: str
) -> None:
    with TestClient(create_app(catalog_settings), headers=bearer(token)) as client:
        assert client.post(CHECKOUTS_URL, json=creation_body(quotable, items=[])).status_code == 422
        assert (
            client.post(CHECKOUTS_URL, json=creation_body(quotable, quantity=0)).status_code == 422
        )
        # Already expired, and pushed further out than a quote may be honoured.
        past = creation_body(quotable, expires_at=(NOW - HOUR).isoformat())
        assert client.post(CHECKOUTS_URL, json=past).status_code == 422
        distant = creation_body(quotable, expires_at=(NOW + 48 * HOUR).isoformat())
        assert client.post(CHECKOUTS_URL, json=distant).status_code == 422
