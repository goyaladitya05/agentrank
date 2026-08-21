"""The catalog access boundary, stated as tests so that it cannot drift into an accident.

Phase 1H had to decide something the code had never been asked: is a catalog read public?
AgentRank exists so that buyer agents can shop merchants, which is an argument for yes, and a
storefront is public by definition, which is another. The answer is still no, and the reason is
specific rather than general. It is not that a catalog is secret. It is that these particular
responses are not a storefront:

- `inventory_quantity` is an exact stock level
- `include_inactive` returns products and variants the merchant has deactivated, which are by
  definition not on sale
- `external_id` is the merchant's own identifier from their own systems

Publishing a merchant's stock, their withdrawn products and their internal identifiers is a
product decision, and nobody has made it. So the catalog is merchant private, and a genuinely
public projection with a different response shape belongs to Phase 4, when there is a buyer
agent that needs one. Opening a route later is easy; closing one that agents already depend on
is not.

These tests exist so that the decision is enforced rather than remembered. If someone later
decides the catalog should be public, they have to delete an assertion that says why it is not,
which is exactly the moment to reconsider.
"""

import uuid
from dataclasses import dataclass

import pytest
from conftest import CredentialIssuer, bearer
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.commerce.repository import CatalogRepository, MerchantRepository
from agentrank_api.config import Settings
from agentrank_api.main import create_app

pytestmark = pytest.mark.anyio

PRODUCTS_URL = "/api/v1/commerce/products"
SEARCH_URL = f"{PRODUCTS_URL}/search"


@dataclass(frozen=True, slots=True)
class Catalog:
    """One merchant, its key, one live product and one it has withdrawn."""

    merchant_id: uuid.UUID
    product_id: uuid.UUID
    retired_id: uuid.UUID
    token: str


async def build(session: AsyncSession, issue_credential: CredentialIssuer, slug: str) -> Catalog:
    merchant = await MerchantRepository(session).create(slug=slug, name=slug.title())
    catalog = CatalogRepository(session)
    product = await catalog.create_product(
        merchant_id=merchant.id, external_id=f"{slug}-1", title="Charger", category="chargers"
    )
    await catalog.create_variant(
        product=product,
        sku=f"{slug}-BLK",
        price_amount_minor=499900,
        currency="INR",
        inventory_quantity=7,
    )
    retired = await catalog.create_product(
        merchant_id=merchant.id,
        external_id=f"{slug}-2",
        title="Discontinued Charger",
        category="chargers",
        is_active=False,
    )
    await catalog.create_variant(
        product=retired,
        sku=f"{slug}-OLD",
        price_amount_minor=199900,
        currency="INR",
        inventory_quantity=1,
        is_active=False,
    )
    await session.commit()
    token = await issue_credential(merchant.id, slug)
    return Catalog(
        merchant_id=merchant.id,
        product_id=product.id,
        retired_id=retired.id,
        token=token,
    )


@pytest.fixture
async def alice(session: AsyncSession, issue_credential: CredentialIssuer) -> Catalog:
    return await build(session, issue_credential, "ampere-supply")


@pytest.fixture
async def bob(session: AsyncSession, issue_credential: CredentialIssuer) -> Catalog:
    return await build(session, issue_credential, "volt-works")


def client_for(catalog_settings: Settings, shop: Catalog) -> TestClient:
    return TestClient(create_app(catalog_settings), headers=bearer(shop.token))


async def test_a_catalog_read_requires_a_credential(
    catalog_settings: Settings, alice: Catalog
) -> None:
    """Both operations, so a route that stayed public is caught rather than assumed absent."""
    with TestClient(create_app(catalog_settings)) as client:
        product = client.get(f"{PRODUCTS_URL}/{alice.product_id}")
        search = client.post(SEARCH_URL, json={"query": "charger"})

    assert product.status_code == 401
    assert search.status_code == 401
    assert product.json()["error"] == search.json()["error"] == "unauthenticated"


async def test_a_merchant_reads_its_own_product(catalog_settings: Settings, alice: Catalog) -> None:
    with client_for(catalog_settings, alice) as client:
        response = client.get(f"{PRODUCTS_URL}/{alice.product_id}")

    assert response.status_code == 200
    assert response.json()["merchant"]["id"] == str(alice.merchant_id)


async def test_another_merchants_product_does_not_exist(
    catalog_settings: Settings, alice: Catalog, bob: Catalog
) -> None:
    with client_for(catalog_settings, alice) as client:
        foreign = client.get(f"{PRODUCTS_URL}/{bob.product_id}")
        unknown = client.get(f"{PRODUCTS_URL}/{uuid.uuid7()}")

    assert foreign.status_code == unknown.status_code == 404
    assert foreign.json()["resource"] == unknown.json()["resource"] == "product"
    assert foreign.json().keys() == unknown.json().keys()


async def test_a_search_returns_only_the_credentials_own_catalog(
    catalog_settings: Settings, alice: Catalog, bob: Catalog
) -> None:
    """There is no identifier to guess here, so the credential is the entire scope."""
    with client_for(catalog_settings, alice) as client:
        mine = client.post(SEARCH_URL, json={"query": "charger"}).json()

    assert [result["id"] for result in mine["results"]] == [str(alice.product_id)]


async def test_a_search_body_cannot_name_a_merchant(
    catalog_settings: Settings, alice: Catalog, bob: Catalog
) -> None:
    """The field is gone, and a caller who sends it anyway searches their own shelves.

    Worth asserting rather than assuming: Pydantic ignores unknown fields, so removing
    `merchant_id` from the request model would otherwise have silently accepted a body that
    still named one.
    """
    with client_for(catalog_settings, alice) as client:
        response = client.post(
            SEARCH_URL, json={"query": "charger", "merchant_id": str(bob.merchant_id)}
        )

    assert [result["id"] for result in response.json()["results"]] == [str(alice.product_id)]


async def test_the_response_carries_exactly_the_data_that_kept_it_private(
    catalog_settings: Settings, alice: Catalog
) -> None:
    """The three fields the decision rests on, asserted to still be there.

    This is the test that makes the reasoning falsifiable rather than a paragraph. If a future
    change strips stock levels, deactivated products and merchant external identifiers out of
    these responses, this fails, and at that point publishing the catalog becomes a reasonable
    thing to reconsider. While it passes, the argument holds.
    """
    with client_for(catalog_settings, alice) as client:
        product = client.get(f"{PRODUCTS_URL}/{alice.product_id}").json()
        withdrawn = client.post(
            SEARCH_URL, json={"query": "charger", "include_inactive": True}
        ).json()

    assert product["external_id"] == "ampere-supply-1"
    assert product["variants"][0]["inventory_quantity"] == 7
    assert str(alice.retired_id) in {result["id"] for result in withdrawn["results"]}


async def test_a_withdrawn_product_is_reachable_only_with_a_credential(
    catalog_settings: Settings, alice: Catalog
) -> None:
    """The sharpest case for the decision, stated on its own.

    A deactivated product is one the merchant has taken off sale. Serving it to an anonymous
    caller would publish a commercial decision that has not been announced.
    """
    with TestClient(create_app(catalog_settings)) as client:
        anonymous = client.get(f"{PRODUCTS_URL}/{alice.retired_id}")

    with client_for(catalog_settings, alice) as client:
        owner = client.get(f"{PRODUCTS_URL}/{alice.retired_id}")

    assert anonymous.status_code == 401
    assert owner.status_code == 200
    assert owner.json()["is_active"] is False


def test_the_whole_commerce_namespace_declares_the_bearer_scheme(
    catalog_settings: Settings,
) -> None:
    """With the catalog decided, the rule covers every commerce operation without exception.

    Stated as one assertion over the namespace rather than a list of paths, so that a route
    added later without the dependency fails here rather than being noticed in production.
    """
    schema = create_app(catalog_settings).openapi()

    commerce = {
        path: operations
        for path, operations in schema["paths"].items()
        if path.startswith("/api/v1/commerce")
    }
    unprotected = [
        path
        for path, operations in commerce.items()
        for operation in operations.values()
        if operation.get("security") != [{"MerchantApiKey": []}]
    ]

    # A positive count beside the negative assertion, so an empty set cannot pass as clean.
    assert len(commerce) == 17
    assert unprotected == []


def test_health_and_readiness_are_still_the_only_public_operations(
    catalog_settings: Settings,
) -> None:
    """The public surface, enumerated. Anything added to it has to be added here too."""
    schema = create_app(catalog_settings).openapi()

    public = {
        path
        for path, operations in schema["paths"].items()
        for operation in operations.values()
        if "security" not in operation
    }

    assert public == {"/health", "/ready"}
