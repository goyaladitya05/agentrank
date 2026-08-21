"""Cross merchant behavior of the payment endpoints, and what a denial costs a provider.

The highest risk surface in the application, so the claim is stronger here than anywhere else.
It is not enough that merchant A gets a 404 for merchant B's payment. The request must be
refused before this application talks to a processor at all, because a route that queried a
provider and then refused would still have told an outsider that an identity exists, would still
have consumed a rate limit somebody else pays for, and against a real processor would still have
been an unauthorized instruction that happened to fail.

So every cross merchant test here asserts the provider's own counters. `executions` is every
charge instruction it was ever given, `queries` is every question it was ever asked, and
`charges` is how many logical operations it believes it performed. A denied request leaves all
three exactly where it found them.

Idempotency is checked under authentication too. One key against one checkout is still one
payment, and because a checkout belongs to one merchant, two merchants using the same key string
are two payments that cannot see each other.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from conftest import CredentialIssuer, bearer
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.auth.service import MerchantCredentialService
from agentrank_api.auth.tokens import parse_token
from agentrank_api.commerce.repository import CatalogRepository, MerchantRepository
from agentrank_api.config import Settings
from agentrank_api.constraints.repository import IntentConstraintRepository
from agentrank_api.constraints.rules import ConstraintOperator, IntentConstraintSpec
from agentrank_api.main import create_app
from agentrank_api.mandates.repository import MandateRepository
from agentrank_api.payments.fake import FakeOutcome, FakePaymentProvider

pytestmark = pytest.mark.anyio

CHECKOUTS_URL = "/api/v1/commerce/checkouts"
PAYMENTS_URL = "/api/v1/commerce/payments"
NOW = datetime.now(UTC)
HOUR = timedelta(hours=1)
PRICE = 499900
KEY = "pay-scope-0001"
BLACK = IntentConstraintSpec.required_attribute("color", ConstraintOperator.EQ, "black")


@dataclass(frozen=True, slots=True)
class Shop:
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


@pytest.fixture
def provider() -> FakePaymentProvider:
    """One provider shared by every client in a test, so the counters are the whole story."""
    return FakePaymentProvider(default=FakeOutcome.SUCCESS)


def client_for(catalog_settings: Settings, provider: FakePaymentProvider, shop: Shop) -> TestClient:
    return TestClient(create_app(catalog_settings, provider), headers=bearer(shop.token))


def prepared(client: TestClient, shop: Shop) -> str:
    """A quote with stock held for it, which is what a payment requires."""
    created = client.post(
        CHECKOUTS_URL,
        json={
            "mandate_id": str(shop.mandate_id),
            "items": [{"variant_id": str(shop.variant_id), "quantity": 1}],
        },
    )
    assert created.status_code == 201
    checkout_id = str(created.json()["id"])
    readiness = client.post(f"{CHECKOUTS_URL}/{checkout_id}/prepare-execution")
    assert readiness.json()["ready"] is True
    return checkout_id


def untouched(provider: FakePaymentProvider) -> None:
    """The provider was never involved. All three counters, because they answer differently.

    `executions` would catch an instruction that was sent, `queries` a question that was asked,
    and `charges` a logical operation the provider believes it performed. A refusal that reached
    any of them is a refusal that arrived too late.
    """
    assert provider.executions == []
    assert provider.queries == []
    assert provider.charges == 0


def not_found(response: Response, resource: str) -> None:
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"
    assert response.json()["resource"] == resource


async def test_a_merchant_pays_for_its_own_checkout(
    catalog_settings: Settings, provider: FakePaymentProvider, alice: Shop
) -> None:
    with client_for(catalog_settings, provider, alice) as client:
        checkout_id = prepared(client, alice)
        response = client.post(
            f"{CHECKOUTS_URL}/{checkout_id}/payments", json={"idempotency_key": KEY}
        )

    assert response.status_code == 200
    assert response.json()["attempt"]["status"] == "SUCCEEDED"
    assert provider.charges == 1


async def test_paying_for_another_merchants_checkout_never_reaches_a_provider(
    catalog_settings: Settings, provider: FakePaymentProvider, alice: Shop, bob: Shop
) -> None:
    """The single most important assertion in this phase.

    Merchant B prepares a real, payable checkout. Merchant A knows its identifier and asks to
    pay for it. The answer is 404, and the provider was never told anything.
    """
    with client_for(catalog_settings, provider, bob) as client:
        bobs_checkout = prepared(client, bob)

    with client_for(catalog_settings, provider, alice) as client:
        foreign = client.post(
            f"{CHECKOUTS_URL}/{bobs_checkout}/payments", json={"idempotency_key": KEY}
        )
        unknown = client.post(
            f"{CHECKOUTS_URL}/{uuid.uuid7()}/payments", json={"idempotency_key": KEY}
        )

    not_found(foreign, "checkout")
    not_found(unknown, "checkout")
    assert foreign.json().keys() == unknown.json().keys()
    untouched(provider)


async def test_reading_another_merchants_payment_is_refused(
    catalog_settings: Settings, provider: FakePaymentProvider, alice: Shop, bob: Shop
) -> None:
    with client_for(catalog_settings, provider, bob) as client:
        checkout_id = prepared(client, bob)
        attempt_id = client.post(
            f"{CHECKOUTS_URL}/{checkout_id}/payments", json={"idempotency_key": KEY}
        ).json()["attempt"]["id"]

    with client_for(catalog_settings, provider, alice) as client:
        foreign = client.get(f"{PAYMENTS_URL}/{attempt_id}")
        unknown = client.get(f"{PAYMENTS_URL}/{uuid.uuid7()}")

    not_found(foreign, "payment_attempt")
    not_found(unknown, "payment_attempt")
    assert foreign.json().keys() == unknown.json().keys()
    # One charge, and it is merchant B's. Nothing merchant A did added to it.
    assert provider.charges == 1


async def test_reconciling_another_merchants_payment_asks_the_provider_nothing(
    catalog_settings: Settings, provider: FakePaymentProvider, alice: Shop, bob: Shop
) -> None:
    """Reconciliation is the one read shaped route that talks to an external system.

    Merchant B has a genuinely unresolved payment, which is exactly the state reconciliation
    acts on, so a route that checked ownership after querying would be caught here.
    """
    provider.default = FakeOutcome.AMBIGUOUS

    with client_for(catalog_settings, provider, bob) as client:
        checkout_id = prepared(client, bob)
        attempt_id = client.post(
            f"{CHECKOUTS_URL}/{checkout_id}/payments", json={"idempotency_key": KEY}
        ).json()["attempt"]["id"]

    assert provider.queries == []
    executions = len(provider.executions)

    with client_for(catalog_settings, provider, alice) as client:
        foreign = client.post(f"{PAYMENTS_URL}/{attempt_id}/reconcile")
        unknown = client.post(f"{PAYMENTS_URL}/{uuid.uuid7()}/reconcile")

    not_found(foreign, "payment_attempt")
    not_found(unknown, "payment_attempt")
    assert provider.queries == []
    assert len(provider.executions) == executions


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/checkouts/{checkout}/payments"),
        ("get", "/payments/{attempt}"),
        ("post", "/payments/{attempt}/reconcile"),
    ],
)
async def test_every_payment_operation_refuses_an_anonymous_caller(
    catalog_settings: Settings,
    provider: FakePaymentProvider,
    alice: Shop,
    method: str,
    path: str,
) -> None:
    """All three payment paths, and the provider stays untouched for every one of them."""
    with client_for(catalog_settings, provider, alice) as client:
        checkout_id = prepared(client, alice)

    url = "/api/v1/commerce" + path.format(checkout=checkout_id, attempt=uuid.uuid7())

    with TestClient(create_app(catalog_settings, provider)) as client:
        response = client.request(method.upper(), url, json={"idempotency_key": KEY})

    assert response.status_code == 401
    assert response.json()["error"] == "unauthenticated"
    untouched(provider)


async def test_idempotency_still_resolves_to_one_payment_for_one_merchant(
    catalog_settings: Settings, provider: FakePaymentProvider, alice: Shop
) -> None:
    """Authentication must not have changed what a repeated key means."""
    with client_for(catalog_settings, provider, alice) as client:
        checkout_id = prepared(client, alice)
        first = client.post(
            f"{CHECKOUTS_URL}/{checkout_id}/payments", json={"idempotency_key": KEY}
        )
        second = client.post(
            f"{CHECKOUTS_URL}/{checkout_id}/payments", json={"idempotency_key": KEY}
        )

    assert first.json()["created"] is True
    assert second.json()["created"] is False
    assert second.json()["attempt"]["id"] == first.json()["attempt"]["id"]
    assert provider.executions_for(KEY) == 1
    assert provider.charges == 1


async def test_two_merchants_using_one_key_are_two_payments_here(
    catalog_settings: Settings, provider: FakePaymentProvider, alice: Shop, bob: Shop
) -> None:
    """One key names an operation against a checkout, and a checkout belongs to one merchant.

    So inside this application two merchants choosing the same key string are two payments:
    two attempts, two identifiers, and neither can read the other's. That is the property
    authentication had to preserve and it is preserved.

    What this test also pins is the edge of that property. `provider.executions` shows two
    instructions leaving this application, and `provider.charges` shows the fake performing one,
    because the key that travels to a provider is the caller's own string and the fake keeps one
    ledger for all of them. That is a real limitation of a shared provider account rather than a
    bug in scoping, and it is recorded in docs/shortcomings.md rather than fixed here: changing
    the identity that reaches a provider is a change to payment semantics, and this phase
    deliberately made none.
    """
    with client_for(catalog_settings, provider, alice) as client:
        alices_checkout = prepared(client, alice)
        mine = client.post(
            f"{CHECKOUTS_URL}/{alices_checkout}/payments", json={"idempotency_key": KEY}
        )

    with client_for(catalog_settings, provider, bob) as client:
        bobs_checkout = prepared(client, bob)
        theirs = client.post(
            f"{CHECKOUTS_URL}/{bobs_checkout}/payments", json={"idempotency_key": KEY}
        )

    assert mine.json()["created"] is True
    assert theirs.json()["created"] is True
    assert mine.json()["attempt"]["id"] != theirs.json()["attempt"]["id"]
    assert mine.json()["attempt"]["merchant_id"] == str(alice.merchant_id)
    assert theirs.json()["attempt"]["merchant_id"] == str(bob.merchant_id)

    # Two instructions left this application. The fake collapsed them, because one key string
    # is one entry in its single ledger.
    assert len(provider.executions) == 2
    assert provider.charges == 1

    # Neither merchant can read the other's payment, even holding the identifier.
    with client_for(catalog_settings, provider, alice) as client:
        assert client.get(f"{PAYMENTS_URL}/{theirs.json()['attempt']['id']}").status_code == 404
    with client_for(catalog_settings, provider, bob) as client:
        assert client.get(f"{PAYMENTS_URL}/{mine.json()['attempt']['id']}").status_code == 404


async def test_a_revoked_credential_cannot_pay(
    catalog_settings: Settings,
    session: AsyncSession,
    provider: FakePaymentProvider,
    alice: Shop,
) -> None:
    """Revocation reaches the payment path immediately, and it costs a provider nothing."""
    parsed = parse_token(alice.token)
    assert parsed is not None

    with client_for(catalog_settings, provider, alice) as client:
        checkout_id = prepared(client, alice)

        await MerchantCredentialService(session).revoke(parsed.credential_id)

        response = client.post(
            f"{CHECKOUTS_URL}/{checkout_id}/payments", json={"idempotency_key": KEY}
        )

    assert response.status_code == 401
    untouched(provider)


def test_every_commerce_operation_declares_the_bearer_scheme(catalog_settings: Settings) -> None:
    """Read off the generated schema rather than off the route table.

    The schema is what a client generator reads, so it is what has to be right. Asserting it
    across a whole namespace at once is what catches a route added later without the dependency:
    a new path with no `security` fails here rather than being noticed in production.

    The catalog namespace is asserted separately, by `tests/test_catalog_access.py`, because
    what it publishes is a product decision rather than a consequence of this one.
    """
    schema = create_app(catalog_settings).openapi()
    scoped = (
        "/api/v1/commerce/mandates",
        "/api/v1/commerce/checkouts",
        "/api/v1/commerce/payments",
    )

    unprotected = [
        path
        for path, operations in schema["paths"].items()
        if path.startswith(scoped)
        for operation in operations.values()
        if operation.get("security") != [{"MerchantApiKey": []}]
    ]

    assert unprotected == []
    # A positive count beside the negative assertion, so an empty set cannot pass as clean.
    assert len([path for path in schema["paths"] if path.startswith(scoped)]) == 15


def test_the_schema_still_publishes_no_operator_recovery(catalog_settings: Settings) -> None:
    """Authentication did not become a reason to expose recovery over HTTP.

    Restated here beside the scoping tests, because the tempting move once a caller can be
    identified is to publish the operator commands and gate them. A merchant credential
    identifies a merchant, not an operator, and abandoning a payment or resuming one is neither
    merchant's business. See docs/security.md.
    """
    paths = set(create_app(catalog_settings).openapi()["paths"])

    assert paths
    for forbidden in ("abandon", "resume", "sweep", "operator", "recovery", "unresolved"):
        assert not [path for path in paths if forbidden in path]
