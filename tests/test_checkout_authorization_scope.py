"""Cross merchant behavior of the checkout endpoints.

The same claim the mandate scope tests make, against the surface that costs a merchant
something. Preparing a checkout holds real stock and cancelling one releases it, so a caller who
could reach another merchant's quote could take that merchant's inventory off the shelf or give
it back underneath a buyer.

Every route is exercised twice from merchant A: once against merchant B's checkout and once
against an identifier nobody has ever used. The two answers have to be the same, or a caller can
tell a real checkout from an invented one by asking, and that is enumeration with extra steps.

Denials are also asserted to be inert. The stock merchant B is holding does not move, its
checkout does not change status, and nothing is written into its audit history.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from conftest import CredentialIssuer, bearer
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.audit.repository import AuditRepository
from agentrank_api.checkout.models import CheckoutStatus
from agentrank_api.checkout.repository import CheckoutRepository
from agentrank_api.commerce.models import Variant
from agentrank_api.commerce.repository import CatalogRepository, MerchantRepository
from agentrank_api.config import Settings
from agentrank_api.constraints.repository import IntentConstraintRepository
from agentrank_api.constraints.rules import ConstraintOperator, IntentConstraintSpec
from agentrank_api.inventory.models import ReservationStatus
from agentrank_api.main import create_app
from agentrank_api.mandates.repository import MandateRepository

pytestmark = pytest.mark.anyio

CHECKOUTS_URL = "/api/v1/commerce/checkouts"
NOW = datetime.now(UTC)
HOUR = timedelta(hours=1)
PRICE = 499900
BLACK = IntentConstraintSpec.required_attribute("color", ConstraintOperator.EQ, "black")


@dataclass(frozen=True, slots=True)
class Shop:
    """One merchant, its key, and one quotable variant under one qualified mandate."""

    merchant_id: uuid.UUID
    mandate_id: uuid.UUID
    variant_id: uuid.UUID
    token: str


async def build(session: AsyncSession, issue_credential: CredentialIssuer, slug: str) -> Shop:
    merchant = await MerchantRepository(session).create(slug=slug, name=slug.title())
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
    variant = await catalog.create_variant(
        product=product,
        sku=f"{slug}-BLK",
        price_amount_minor=PRICE,
        currency="INR",
        inventory_quantity=3,
        attributes={"color": "black"},
    )
    await session.commit()
    token = await issue_credential(merchant.id, slug)
    return Shop(
        merchant_id=merchant.id,
        mandate_id=mandate.id,
        variant_id=variant.id,
        token=token,
    )


@pytest.fixture
async def alice(session: AsyncSession, issue_credential: CredentialIssuer) -> Shop:
    return await build(session, issue_credential, "ampere-supply")


@pytest.fixture
async def bob(session: AsyncSession, issue_credential: CredentialIssuer) -> Shop:
    return await build(session, issue_credential, "volt-works")


def client_for(catalog_settings: Settings, shop: Shop) -> TestClient:
    return TestClient(create_app(catalog_settings), headers=bearer(shop.token))


def quote(client: TestClient, shop: Shop) -> str:
    created = client.post(
        CHECKOUTS_URL,
        json={
            "mandate_id": str(shop.mandate_id),
            "items": [{"variant_id": str(shop.variant_id), "quantity": 1}],
        },
    )
    assert created.status_code == 201
    return str(created.json()["id"])


def indistinguishable(foreign: Response, unknown: Response) -> None:
    """A foreign checkout and a nonexistent one must answer identically."""
    assert foreign.status_code == 404
    assert unknown.status_code == 404
    assert foreign.json()["error"] == unknown.json()["error"] == "not_found"
    assert foreign.json()["resource"] == unknown.json()["resource"] == "checkout"
    assert foreign.json().keys() == unknown.json().keys()


async def stock(session: AsyncSession, variant_id: uuid.UUID) -> int:
    variant = await session.get(Variant, variant_id)
    assert variant is not None
    await session.refresh(variant)
    return variant.inventory_quantity


async def events(session: AsyncSession, merchant_id: uuid.UUID) -> list[str]:
    return [
        event.event_type for event in await AuditRepository(session).list_for_merchant(merchant_id)
    ]


@pytest.fixture
def bobs_checkout(catalog_settings: Settings, bob: Shop) -> str:
    with client_for(catalog_settings, bob) as client:
        return quote(client, bob)


async def test_a_merchant_reads_its_own_checkout(catalog_settings: Settings, alice: Shop) -> None:
    with client_for(catalog_settings, alice) as client:
        checkout_id = quote(client, alice)
        response = client.get(f"{CHECKOUTS_URL}/{checkout_id}")

    assert response.status_code == 200
    assert response.json()["merchant_id"] == str(alice.merchant_id)


async def test_a_mandate_belonging_to_another_merchant_cannot_be_quoted_against(
    catalog_settings: Settings, alice: Shop, bob: Shop
) -> None:
    """Knowing both identifiers used to be enough. Now the credential decides."""
    with client_for(catalog_settings, alice) as client:
        response = client.post(
            CHECKOUTS_URL,
            json={
                "mandate_id": str(bob.mandate_id),
                "items": [{"variant_id": str(alice.variant_id), "quantity": 1}],
            },
        )

    assert response.status_code == 404
    assert response.json()["resource"] == "mandate"


async def test_a_variant_belonging_to_another_merchant_cannot_be_quoted(
    catalog_settings: Settings, alice: Shop, bob: Shop
) -> None:
    with client_for(catalog_settings, alice) as client:
        response = client.post(
            CHECKOUTS_URL,
            json={
                "mandate_id": str(alice.mandate_id),
                "items": [{"variant_id": str(bob.variant_id), "quantity": 1}],
            },
        )

    assert response.status_code == 404
    assert response.json()["resource"] == "variant"


@pytest.mark.parametrize(
    ("method", "suffix"),
    [
        ("get", ""),
        ("get", "/authorization"),
        ("get", "/execution-authorization"),
        ("get", "/intent-authorization"),
        ("post", "/prepare-execution"),
        ("post", "/cancel"),
    ],
)
async def test_no_checkout_route_reaches_another_merchants_quote(
    catalog_settings: Settings,
    alice: Shop,
    bobs_checkout: str,
    method: str,
    suffix: str,
) -> None:
    """Enumerated rather than sampled. A route added without scoping fails here."""
    with client_for(catalog_settings, alice) as client:
        foreign = client.request(method.upper(), f"{CHECKOUTS_URL}/{bobs_checkout}{suffix}")
        unknown = client.request(method.upper(), f"{CHECKOUTS_URL}/{uuid.uuid7()}{suffix}")

    indistinguishable(foreign, unknown)


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("post", "", {"mandate_id": None, "items": []}),
        ("get", "/{checkout}", None),
        ("get", "/{checkout}/authorization", None),
        ("get", "/{checkout}/execution-authorization", None),
        ("post", "/{checkout}/prepare-execution", None),
        ("post", "/{checkout}/cancel", None),
        ("post", "/{checkout}/payments", {"idempotency_key": "pay-anonymous-0001"}),
    ],
)
async def test_every_checkout_operation_refuses_an_anonymous_caller(
    catalog_settings: Settings,
    bobs_checkout: str,
    method: str,
    suffix: str,
    body: dict[str, Any] | None,
) -> None:
    url = f"{CHECKOUTS_URL}{suffix.format(checkout=bobs_checkout)}"

    with TestClient(create_app(catalog_settings)) as client:
        response = client.request(method.upper(), url, json=body)

    assert response.status_code == 401
    assert response.json()["error"] == "unauthenticated"


async def test_another_merchants_checkout_cannot_be_prepared_and_holds_no_stock(
    catalog_settings: Settings, session: AsyncSession, alice: Shop, bob: Shop, bobs_checkout: str
) -> None:
    """Preparation is the first operation that costs a merchant something.

    A denied preparation must not take a unit off merchant B's shelf, and it must not leave a
    reservation behind for merchant B to wait out.
    """
    before = await stock(session, bob.variant_id)
    history = await events(session, bob.merchant_id)

    with client_for(catalog_settings, alice) as client:
        foreign = client.post(f"{CHECKOUTS_URL}/{bobs_checkout}/prepare-execution")
        unknown = client.post(f"{CHECKOUTS_URL}/{uuid.uuid7()}/prepare-execution")

    indistinguishable(foreign, unknown)
    assert await stock(session, bob.variant_id) == before
    assert await events(session, bob.merchant_id) == history

    # And merchant B can still prepare it, which proves nothing was half held.
    with client_for(catalog_settings, bob) as client:
        readiness = client.post(f"{CHECKOUTS_URL}/{bobs_checkout}/prepare-execution")
    assert readiness.status_code == 200
    assert readiness.json()["ready"] is True


async def test_another_merchants_checkout_cannot_be_cancelled(
    catalog_settings: Settings, session: AsyncSession, alice: Shop, bob: Shop, bobs_checkout: str
) -> None:
    """Cancelling somebody else's quote would withdraw an offer they had not withdrawn."""
    history = await events(session, bob.merchant_id)

    with client_for(catalog_settings, alice) as client:
        foreign = client.post(f"{CHECKOUTS_URL}/{bobs_checkout}/cancel")
        unknown = client.post(f"{CHECKOUTS_URL}/{uuid.uuid7()}/cancel")

    indistinguishable(foreign, unknown)
    assert await events(session, bob.merchant_id) == history

    checkout = await CheckoutRepository(session).get(
        uuid.UUID(bobs_checkout), merchant_id=bob.merchant_id
    )
    assert checkout is not None
    assert checkout.status is CheckoutStatus.OPEN
    assert checkout.cancelled_at is None


async def test_a_cancellation_cannot_release_another_merchants_hold(
    catalog_settings: Settings, session: AsyncSession, alice: Shop, bob: Shop, bobs_checkout: str
) -> None:
    """The version of the previous test that actually moves inventory.

    Merchant B prepares first, so there is a real reservation to try to release. A cancellation
    from merchant A must leave it holding.
    """
    with client_for(catalog_settings, bob) as client:
        assert (
            client.post(f"{CHECKOUTS_URL}/{bobs_checkout}/prepare-execution").json()["ready"]
            is True
        )

    with client_for(catalog_settings, alice) as client:
        assert client.post(f"{CHECKOUTS_URL}/{bobs_checkout}/cancel").status_code == 404

    with client_for(catalog_settings, bob) as client:
        readiness = client.post(f"{CHECKOUTS_URL}/{bobs_checkout}/prepare-execution")

    assert readiness.json()["ready"] is True
    assert readiness.json()["reservation"]["status"] == ReservationStatus.ACTIVE.value
