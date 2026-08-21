"""HTTP behavior of the execution readiness endpoints.

Deliberately thin. The authorization composition, the locking and the reservation rules are
asserted once at the service and domain levels; what is checked here is the wire contract
and the status codes.

The distinction worth reading off the wire is that an authorization denial and an empty
shelf are both ordinary answers with a body, not errors. A buyer agent has to tell them
apart, and a 500 would tell it nothing.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.commerce.repository import CatalogRepository, MerchantRepository
from agentrank_api.config import Settings
from agentrank_api.constraints.repository import IntentConstraintRepository
from agentrank_api.constraints.rules import ConstraintOperator, IntentConstraintSpec
from agentrank_api.main import create_app
from agentrank_api.mandates.repository import MandateRepository

pytestmark = pytest.mark.anyio

CHECKOUTS_URL = "/api/v1/commerce/checkouts"
NOW = datetime.now(UTC)
HOUR = timedelta(hours=1)
PRICE = 499900
BLACK = IntentConstraintSpec.required_attribute("color", ConstraintOperator.EQ, "black")


@pytest.fixture
async def shop(session: AsyncSession) -> dict[str, str]:
    """A merchant whose mandate and constraints permit exactly one black charger."""
    merchant = await MerchantRepository(session).create(slug="ampere-supply", name="Ampere")
    mandate = await MandateRepository(session).create(
        merchant_id=merchant.id,
        max_total_amount_minor=PRICE,
        currency="INR",
        valid_from=NOW - HOUR,
        valid_until=NOW + HOUR,
    )
    await IntentConstraintRepository(session).create(
        merchant_id=merchant.id, mandate_id=mandate.id, specs=[BLACK]
    )
    catalog = CatalogRepository(session)
    product = await catalog.create_product(
        merchant_id=merchant.id, external_id="amp-1", title="Charger", category="chargers"
    )
    black = await catalog.create_variant(
        product=product,
        sku="AMP-BLACK",
        price_amount_minor=PRICE,
        currency="INR",
        inventory_quantity=1,
        attributes={"color": "black"},
    )
    blue = await catalog.create_variant(
        product=product,
        sku="AMP-BLUE",
        price_amount_minor=PRICE,
        currency="INR",
        inventory_quantity=1,
        attributes={"color": "blue"},
    )
    await session.commit()
    return {
        "merchant_id": str(merchant.id),
        "mandate_id": str(mandate.id),
        "black": str(black.id),
        "blue": str(blue.id),
    }


def quote(client: TestClient, shop: dict[str, str], variant: str = "black") -> str:
    created = client.post(
        CHECKOUTS_URL,
        json={
            "merchant_id": shop["merchant_id"],
            "mandate_id": shop["mandate_id"],
            "items": [{"variant_id": shop[variant], "quantity": 1}],
        },
    )
    assert created.status_code == 201
    return str(created.json()["id"])


async def test_a_checkout_is_prepared_and_holds_stock(
    catalog_settings: Settings, shop: dict[str, str]
) -> None:
    with TestClient(create_app(catalog_settings)) as client:
        checkout_id = quote(client, shop)

        response = client.post(f"{CHECKOUTS_URL}/{checkout_id}/prepare-execution")
        assert response.status_code == 200
        body = response.json()
        assert body["ready"] is True
        assert body["checkout_id"] == checkout_id
        assert body["inventory_violations"] == []
        assert body["authorization"]["authorized"] is True
        assert body["authorization"]["violations"] == []
        assert body["authorization"]["financial_authorization"] == {
            "allowed": True,
            "violations": [],
        }
        assert body["authorization"]["intent_authorization"]["satisfied"] is True

        reservation = body["reservation"]
        assert reservation["checkout_id"] == checkout_id
        assert reservation["status"] == "ACTIVE"
        assert reservation["total_quantity"] == 1
        assert reservation["lines"] == [{"variant_id": shop["black"], "quantity": 1}]
        assert reservation["expires_at"]

        # Idempotent on the wire as well: the same reservation, not a second one.
        again = client.post(f"{CHECKOUTS_URL}/{checkout_id}/prepare-execution")
        assert again.json()["reservation"]["id"] == reservation["id"]


async def test_a_semantic_denial_is_an_answer_and_not_an_error(
    catalog_settings: Settings, shop: dict[str, str]
) -> None:
    """A blue charger at the same price: the money is fine and the purchase is not."""
    with TestClient(create_app(catalog_settings)) as client:
        checkout_id = quote(client, shop, variant="blue")

        response = client.post(f"{CHECKOUTS_URL}/{checkout_id}/prepare-execution")

        assert response.status_code == 200
        body = response.json()
        assert body["ready"] is False
        assert body["reservation"] is None
        assert body["authorization"]["financial_authorization"]["allowed"] is True
        intent = body["authorization"]["intent_authorization"]
        assert intent["satisfied"] is False
        assert intent["violations"][0]["code"] == "REQUIRED_ATTRIBUTE_MISMATCH"
        assert intent["violations"][0]["expected"] == "black"
        assert intent["violations"][0]["actual"] == "blue"


async def test_an_empty_shelf_is_reported_with_the_numbers_that_decided_it(
    catalog_settings: Settings, shop: dict[str, str]
) -> None:
    with TestClient(create_app(catalog_settings)) as client:
        first, second = quote(client, shop), quote(client, shop)
        assert client.post(f"{CHECKOUTS_URL}/{first}/prepare-execution").json()["ready"] is True

        response = client.post(f"{CHECKOUTS_URL}/{second}/prepare-execution")

        assert response.status_code == 200
        body = response.json()
        assert body["ready"] is False
        assert body["reservation"] is None
        # Authorized, and still not ready. The two refusals are different facts.
        assert body["authorization"]["authorized"] is True
        assert body["inventory_violations"] == [
            {
                "code": "INSUFFICIENT_INVENTORY",
                "variant_id": shop["black"],
                "requested_quantity": 1,
                "available_quantity": 0,
            }
        ]


async def test_the_authorization_read_reserves_nothing(
    catalog_settings: Settings, shop: dict[str, str]
) -> None:
    """Informational only. If it held stock it would be granting something."""
    with TestClient(create_app(catalog_settings)) as client:
        first, second = quote(client, shop), quote(client, shop)

        read = client.get(f"{CHECKOUTS_URL}/{first}/execution-authorization")
        assert read.status_code == 200
        assert read.json()["authorized"] is True
        assert "reservation" not in read.json()

        # The unit is still there for someone else, which is the proof it held nothing.
        assert client.post(f"{CHECKOUTS_URL}/{second}/prepare-execution").json()["ready"] is True


async def test_a_cancelled_checkout_cannot_be_prepared(
    catalog_settings: Settings, shop: dict[str, str]
) -> None:
    with TestClient(create_app(catalog_settings)) as client:
        checkout_id = quote(client, shop)
        assert client.post(f"{CHECKOUTS_URL}/{checkout_id}/cancel").status_code == 200

        body = client.post(f"{CHECKOUTS_URL}/{checkout_id}/prepare-execution").json()

        assert body["ready"] is False
        assert body["reservation"] is None
        assert "CHECKOUT_NOT_OPEN" in body["authorization"]["financial_authorization"]["violations"]


async def test_a_mandate_with_no_constraints_fails_closed_on_the_wire(
    catalog_settings: Settings, session: AsyncSession, shop: dict[str, str]
) -> None:
    """The dangerous case, read off the response: no semantic authorization means no."""
    unqualified = await MandateRepository(session).create(
        merchant_id=uuid.UUID(shop["merchant_id"]),
        max_total_amount_minor=PRICE,
        currency="INR",
        valid_from=NOW - HOUR,
        valid_until=NOW + HOUR,
    )
    await session.commit()

    with TestClient(create_app(catalog_settings)) as client:
        created = client.post(
            CHECKOUTS_URL,
            json={
                "merchant_id": shop["merchant_id"],
                "mandate_id": str(unqualified.id),
                "items": [{"variant_id": shop["black"], "quantity": 1}],
            },
        )
        checkout_id = created.json()["id"]

        body = client.post(f"{CHECKOUTS_URL}/{checkout_id}/prepare-execution").json()

        assert body["ready"] is False
        assert body["reservation"] is None
        assert body["authorization"]["authorized"] is False
        assert body["authorization"]["violations"] == ["INTENT_CONSTRAINTS_MISSING"]
        assert body["authorization"]["intent_authorization"] is None
        # The money was fine, which is why absence must not read as permission.
        assert body["authorization"]["financial_authorization"]["allowed"] is True


async def test_an_unknown_checkout_answers_a_structured_404(catalog_settings: Settings) -> None:
    with TestClient(create_app(catalog_settings)) as client:
        missing = uuid.uuid7()

        for response in (
            client.post(f"{CHECKOUTS_URL}/{missing}/prepare-execution"),
            client.get(f"{CHECKOUTS_URL}/{missing}/execution-authorization"),
        ):
            assert response.status_code == 404
            assert response.json()["resource"] == "checkout"
            assert response.json()["identifier"] == str(missing)
