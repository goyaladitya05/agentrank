"""HTTP behavior of the intent constraint and intent authorization endpoints.

Deliberately thin. The evaluation rules, the refusals and the financial separation are
asserted once at the domain and service levels; what is checked here is the wire contract.

The one shape worth exercising end to end is the pair of decisions over one checkout: the
same quote reported as financially allowed and semantically denied, which is the safety
distinction this phase exists to make and the thing a buyer agent has to be able to read
off two responses.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.commerce.repository import CatalogRepository, MerchantRepository
from agentrank_api.config import Settings
from agentrank_api.main import create_app
from agentrank_api.mandates.repository import MandateRepository

pytestmark = pytest.mark.anyio

COMMERCE_URL = "/api/v1/commerce"
NOW = datetime.now(UTC)
HOUR = timedelta(hours=1)
PRICE = 499900


@pytest.fixture
async def shop(session: AsyncSession) -> dict[str, uuid.UUID]:
    """One mandate, one black charger and one blue one, both inside the ceiling."""
    merchant = await MerchantRepository(session).create(slug="ampere-supply", name="Ampere Supply")
    mandate = await MandateRepository(session).create(
        merchant_id=merchant.id,
        max_total_amount_minor=PRICE,
        currency="INR",
        max_quantity=1,
        valid_from=NOW,
        valid_until=NOW + HOUR,
    )
    catalog = CatalogRepository(session)
    product = await catalog.create_product(
        merchant_id=merchant.id, external_id="amp-1", title="Charger", category="chargers"
    )
    black = await catalog.create_variant(
        product=product,
        sku="AMP-CHG-BLK",
        price_amount_minor=PRICE,
        currency="INR",
        inventory_quantity=5,
        attributes={"color": "black", "wattage": 100},
    )
    blue = await catalog.create_variant(
        product=product,
        sku="AMP-CHG-BLU",
        price_amount_minor=PRICE,
        currency="INR",
        inventory_quantity=5,
        attributes={"color": "blue", "wattage": 100},
    )
    await session.commit()
    return {
        "merchant_id": merchant.id,
        "mandate_id": mandate.id,
        "black": black.id,
        "blue": blue.id,
    }


def constraints_body(shop: dict[str, uuid.UUID], **overrides: Any) -> dict[str, Any]:
    return {
        "merchant_id": str(shop["merchant_id"]),
        "constraints": [
            {"kind": "required_attribute", "name": "color", "value": "black"},
            {"kind": "required_attribute", "name": "wattage", "operator": "GTE", "value": 100},
            {"kind": "allowed_category", "category": "chargers"},
        ],
    } | overrides


def quote(client: TestClient, shop: dict[str, uuid.UUID], variant_id: uuid.UUID) -> str:
    created = client.post(
        f"{COMMERCE_URL}/checkouts",
        json={
            "merchant_id": str(shop["merchant_id"]),
            "mandate_id": str(shop["mandate_id"]),
            "items": [{"variant_id": str(variant_id), "quantity": 1}],
        },
    )
    assert created.status_code == 201
    return str(created.json()["id"])


async def test_constraints_are_created_and_read_back(
    catalog_settings: Settings, shop: dict[str, uuid.UUID]
) -> None:
    with TestClient(create_app(catalog_settings)) as client:
        url = f"{COMMERCE_URL}/mandates/{shop['mandate_id']}/constraints"
        created = client.post(url, json=constraints_body(shop))
        assert created.status_code == 201
        body = created.json()
        assert body["mandate_id"] == str(shop["mandate_id"])
        assert [
            (rule["kind"], rule["attribute"], rule["operator"], rule["value"])
            for rule in body["constraints"]
        ] == [
            ("required_attribute", "color", "EQ", "black"),
            ("required_attribute", "wattage", "GTE", 100),
            ("allowed_category", None, "IN", ["chargers"]),
        ]

        assert client.get(url).json() == body


async def test_a_matching_checkout_passes_both_gates(
    catalog_settings: Settings, shop: dict[str, uuid.UUID]
) -> None:
    with TestClient(create_app(catalog_settings)) as client:
        client.post(
            f"{COMMERCE_URL}/mandates/{shop['mandate_id']}/constraints",
            json=constraints_body(shop),
        )
        checkout_id = quote(client, shop, shop["black"])

        financial = client.get(f"{COMMERCE_URL}/checkouts/{checkout_id}/authorization")
        intent = client.get(f"{COMMERCE_URL}/checkouts/{checkout_id}/intent-authorization")

        assert financial.json() == {"allowed": True, "violations": []}
        assert intent.status_code == 200
        assert intent.json()["satisfied"] is True
        assert intent.json()["violations"] == []


async def test_a_violating_checkout_is_affordable_and_still_denied(
    catalog_settings: Settings, shop: dict[str, uuid.UUID]
) -> None:
    """The exact safety distinction, over HTTP: allowed by one gate, denied by the other."""
    with TestClient(create_app(catalog_settings)) as client:
        client.post(
            f"{COMMERCE_URL}/mandates/{shop['mandate_id']}/constraints",
            json=constraints_body(shop),
        )
        checkout_id = quote(client, shop, shop["blue"])

        financial = client.get(f"{COMMERCE_URL}/checkouts/{checkout_id}/authorization")
        intent = client.get(f"{COMMERCE_URL}/checkouts/{checkout_id}/intent-authorization")

        assert financial.json() == {"allowed": True, "violations": []}

        body = intent.json()
        assert body["satisfied"] is False
        assert len(body["violations"]) == 1
        violation = body["violations"][0]
        assert violation["code"] == "REQUIRED_ATTRIBUTE_MISMATCH"
        assert violation["attribute"] == "color"
        assert violation["operator"] == "EQ"
        assert violation["expected"] == "black"
        assert violation["actual"] == "blue"
        assert violation["variant_id"] == str(shop["blue"])


async def test_a_checkout_whose_mandate_has_no_constraints_is_a_structured_404(
    catalog_settings: Settings, shop: dict[str, uuid.UUID]
) -> None:
    """Absence of a semantic authorization is not a passed one."""
    with TestClient(create_app(catalog_settings)) as client:
        checkout_id = quote(client, shop, shop["black"])

        answer = client.get(f"{COMMERCE_URL}/checkouts/{checkout_id}/intent-authorization")

        assert answer.status_code == 404
        assert answer.json()["resource"] == "intent_constraints"


async def test_unknown_mandates_and_checkouts_are_structured_404s(
    catalog_settings: Settings, shop: dict[str, uuid.UUID]
) -> None:
    with TestClient(create_app(catalog_settings)) as client:
        missing_mandate = client.get(f"{COMMERCE_URL}/mandates/{uuid.uuid7()}/constraints")
        assert missing_mandate.status_code == 404
        assert missing_mandate.json()["resource"] == "intent_constraints"

        missing_checkout = client.get(
            f"{COMMERCE_URL}/checkouts/{uuid.uuid7()}/intent-authorization"
        )
        assert missing_checkout.status_code == 404
        assert missing_checkout.json()["resource"] == "checkout"


async def test_a_mandate_may_be_qualified_only_once_over_http(
    catalog_settings: Settings, shop: dict[str, uuid.UUID]
) -> None:
    with TestClient(create_app(catalog_settings)) as client:
        url = f"{COMMERCE_URL}/mandates/{shop['mandate_id']}/constraints"
        assert client.post(url, json=constraints_body(shop)).status_code == 201

        repeated = client.post(url, json=constraints_body(shop))
        assert repeated.status_code == 409
        assert repeated.json()["error"] == "constraints_already_exist"


@pytest.mark.parametrize(
    "constraints",
    [
        pytest.param([], id="an empty list"),
        pytest.param(
            [{"kind": "max_quantity", "quantity": 1}],
            id="only a financial limit, which stores nothing",
        ),
        pytest.param(
            [{"kind": "required_attribute", "name": "wattage", "operator": "GTE", "value": "100"}],
            id="an ordering comparison against text",
        ),
        pytest.param(
            [{"kind": "required_attribute", "name": "color", "value": ["black", "blue"]}],
            id="a list beside a single valued operator",
        ),
        pytest.param(
            [{"kind": "required_attribute", "name": "color", "operator": "MATCHES", "value": "b"}],
            id="an operator that does not exist",
        ),
        pytest.param(
            [{"kind": "max_total_amount", "amount_minor": 1, "currency": "inr"}],
            id="a lowercase currency",
        ),
    ],
)
async def test_a_request_that_cannot_become_an_authorization_is_a_422(
    catalog_settings: Settings, shop: dict[str, uuid.UUID], constraints: list[dict[str, Any]]
) -> None:
    with TestClient(create_app(catalog_settings)) as client:
        answer = client.post(
            f"{COMMERCE_URL}/mandates/{shop['mandate_id']}/constraints",
            json=constraints_body(shop, constraints=constraints),
        )

        assert answer.status_code == 422
