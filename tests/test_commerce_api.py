"""HTTP behavior of the catalog endpoints.

Deliberately thin. Search semantics are covered once at the service level; these tests
check the wire contract, not the query.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.commerce.repository import CatalogRepository, MerchantRepository
from agentrank_api.config import Settings
from agentrank_api.main import create_app

pytestmark = pytest.mark.anyio

SEARCH_URL = "/api/v1/commerce/products/search"


@pytest.fixture
async def seeded(session: AsyncSession) -> dict[str, uuid.UUID]:
    """One product per merchant, both titled the same, so isolation is observable."""
    merchants = MerchantRepository(session)
    repository = CatalogRepository(session)

    ampere = await merchants.create(slug="ampere-supply", name="Ampere Supply")
    voltline = await merchants.create(slug="voltline-parts", name="Voltline Parts")

    charger = await repository.create_product(
        merchant_id=ampere.id,
        external_id="AMP-CHG-100",
        title="100W USB-C Charger",
        description="Gallium nitride wall charger",
        category="chargers",
    )
    await repository.create_variant(
        product=charger,
        sku="AMP-CHG-100-BLK",
        price_amount_minor=499900,
        currency="INR",
        label="Black",
        attributes={"color": "black", "wattage": 100},
        inventory_quantity=12,
    )

    rival = await repository.create_product(
        merchant_id=voltline.id, external_id="VLT-CHG-100", title="100W USB-C Charger"
    )
    await repository.create_variant(
        product=rival, sku="VLT-CHG-100-BLK", price_amount_minor=459900, currency="INR"
    )

    await session.commit()
    return {"ampere": ampere.id, "voltline": voltline.id, "charger": charger.id}


async def test_a_product_is_returned_with_its_variants(
    catalog_settings: Settings, seeded: dict[str, uuid.UUID]
) -> None:
    with TestClient(create_app(catalog_settings)) as client:
        response = client.get(f"/api/v1/commerce/products/{seeded['charger']}")

    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "100W USB-C Charger"
    assert body["merchant"]["slug"] == "ampere-supply"
    variant = body["variants"][0]
    assert variant["price_amount_minor"] == 499900
    assert variant["currency"] == "INR"
    assert variant["attributes"] == {"color": "black", "wattage": 100}


async def test_an_unknown_product_is_a_structured_404(catalog_settings: Settings) -> None:
    missing = uuid.uuid7()
    with TestClient(create_app(catalog_settings)) as client:
        response = client.get(f"/api/v1/commerce/products/{missing}")

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"
    assert response.json()["resource"] == "product"
    assert response.json()["identifier"] == str(missing)


async def test_search_returns_eligible_variants(
    catalog_settings: Settings, seeded: dict[str, uuid.UUID]
) -> None:
    with TestClient(create_app(catalog_settings)) as client:
        response = client.post(
            SEARCH_URL,
            json={
                "merchant_id": str(seeded["ampere"]),
                "query": "charger",
                "max_price_amount_minor": 500000,
                "currency": "INR",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["limit"] == 20
    result = body["results"][0]
    assert result["external_id"] == "AMP-CHG-100"
    assert [v["sku"] for v in result["eligible_variants"]] == ["AMP-CHG-100-BLK"]


async def test_search_never_crosses_merchants(
    catalog_settings: Settings, seeded: dict[str, uuid.UUID]
) -> None:
    with TestClient(create_app(catalog_settings)) as client:
        response = client.post(
            SEARCH_URL,
            json={"merchant_id": str(seeded["voltline"]), "query": "100W USB-C Charger"},
        )

    body = response.json()
    assert [result["external_id"] for result in body["results"]] == ["VLT-CHG-100"]


async def test_a_price_ceiling_without_a_currency_is_rejected(
    catalog_settings: Settings, seeded: dict[str, uuid.UUID]
) -> None:
    with TestClient(create_app(catalog_settings)) as client:
        response = client.post(
            SEARCH_URL,
            json={"merchant_id": str(seeded["ampere"]), "max_price_amount_minor": 500000},
        )

    assert response.status_code == 422
    assert "currency" in response.text


async def test_a_limit_above_the_maximum_is_rejected(
    catalog_settings: Settings, seeded: dict[str, uuid.UUID]
) -> None:
    with TestClient(create_app(catalog_settings)) as client:
        response = client.post(
            SEARCH_URL, json={"merchant_id": str(seeded["ampere"]), "limit": 10_000}
        )

    assert response.status_code == 422
