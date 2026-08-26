"""HTTP behavior of the payment endpoints.

Deliberately thin. The admission rules, the locking, the outcome transactions and the
inventory arithmetic are all asserted at the service and domain levels; what is checked here is
the wire contract, the status codes and the two things that are only visible from the outside.

The first is that a refusal is an ordinary answer with a body rather than an error. A caller
that cannot tell "you may not buy this" from "somebody is already paying for it" will retry the
same request forever, and the two call for opposite next moves.

The second is that no request field can choose what the provider does. The fake is injected
when the application is built, and there is no `simulate` parameter, no header and no body
field that reaches it.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from conftest import CredentialIssuer, bearer
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

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
KEY = "pay-ampere-0001"
OTHER_KEY = "pay-ampere-0002"
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
        inventory_quantity=3,
        attributes={"color": "black"},
    )
    blue = await catalog.create_variant(
        product=product,
        sku="AMP-BLUE",
        price_amount_minor=PRICE,
        currency="INR",
        inventory_quantity=3,
        attributes={"color": "blue"},
    )
    await session.commit()
    return {
        "merchant_id": str(merchant.id),
        "mandate_id": str(mandate.id),
        "black": str(black.id),
        "blue": str(blue.id),
    }


@pytest.fixture
async def token(issue_credential: CredentialIssuer, shop: dict[str, str]) -> str:
    """A key for the shop above. Every route exercised here requires one."""
    return await issue_credential(uuid.UUID(shop["merchant_id"]))


def quote(client: TestClient, shop: dict[str, str], variant: str = "black") -> str:
    created = client.post(
        CHECKOUTS_URL,
        json={
            "mandate_id": shop["mandate_id"],
            "items": [{"variant_id": shop[variant], "quantity": 1}],
        },
    )
    assert created.status_code == 201
    return str(created.json()["id"])


def prepared(client: TestClient, shop: dict[str, str], variant: str = "black") -> str:
    checkout_id = quote(client, shop, variant)
    readiness = client.post(f"{CHECKOUTS_URL}/{checkout_id}/prepare-execution")
    assert readiness.status_code == 200
    assert readiness.json()["ready"] is True
    return checkout_id


async def test_a_prepared_checkout_is_paid(
    catalog_settings: Settings, shop: dict[str, str], token: str
) -> None:
    provider = FakePaymentProvider(default=FakeOutcome.SUCCESS)
    with TestClient(create_app(catalog_settings, provider), headers=bearer(token)) as client:
        checkout_id = prepared(client, shop)

        response = client.post(
            f"{CHECKOUTS_URL}/{checkout_id}/payments", json={"idempotency_key": KEY}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["admitted"] is True
        assert body["created"] is True
        assert body["refusal"] is None
        assert body["checkout_id"] == checkout_id
        assert body["authorization"]["authorized"] is True

        attempt = body["attempt"]
        # Admitted is not paid, and the attempt's own status is what says which.
        assert attempt["status"] == "SUCCEEDED"
        assert attempt["amount_minor"] == PRICE
        assert attempt["currency"] == "INR"
        assert attempt["provider_reference"]
        assert attempt["failure_code"] is None
        assert attempt["outcome_source"] == "EXECUTION"
        assert attempt["dispatched_at"]
        assert attempt["resolved_at"]

        paid = client.get(f"{CHECKOUTS_URL}/{checkout_id}")
        assert paid.json()["status"] == "PAID"
        assert paid.json()["paid_at"]

    assert provider.charges == 1
    assert provider.executions_for(KEY) == 1


async def test_the_same_key_answers_with_the_same_payment(
    catalog_settings: Settings, shop: dict[str, str], token: str
) -> None:
    """Idempotency, visible from the outside. `created` is what makes it visible."""
    provider = FakePaymentProvider(default=FakeOutcome.SUCCESS)
    with TestClient(create_app(catalog_settings, provider), headers=bearer(token)) as client:
        checkout_id = prepared(client, shop)
        first = client.post(
            f"{CHECKOUTS_URL}/{checkout_id}/payments", json={"idempotency_key": KEY}
        )

        second = client.post(
            f"{CHECKOUTS_URL}/{checkout_id}/payments", json={"idempotency_key": KEY}
        )

        assert second.status_code == 200
        assert second.json()["created"] is False
        assert second.json()["attempt"]["id"] == first.json()["attempt"]["id"]
        assert second.json()["attempt"]["status"] == "SUCCEEDED"

    assert provider.executions_for(KEY) == 1
    assert provider.charges == 1


async def test_a_decline_is_an_answer_and_not_an_error(
    catalog_settings: Settings, shop: dict[str, str], token: str
) -> None:
    """The payment was admitted and the provider said no. Both facts are on the wire."""
    provider = FakePaymentProvider(default=FakeOutcome.DECLINE)
    with TestClient(create_app(catalog_settings, provider), headers=bearer(token)) as client:
        checkout_id = prepared(client, shop)

        response = client.post(
            f"{CHECKOUTS_URL}/{checkout_id}/payments", json={"idempotency_key": KEY}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["admitted"] is True
        assert body["attempt"]["status"] == "FAILED"
        assert body["attempt"]["failure_code"] == "CARD_DECLINED"

        # Not a failed checkout. The quote is still good and could be paid for again.
        assert client.get(f"{CHECKOUTS_URL}/{checkout_id}").json()["status"] == "OPEN"


async def test_an_ambiguous_result_is_reported_as_unknown(
    catalog_settings: Settings, shop: dict[str, str], token: str
) -> None:
    provider = FakePaymentProvider(default=FakeOutcome.AMBIGUOUS)
    with TestClient(create_app(catalog_settings, provider), headers=bearer(token)) as client:
        checkout_id = prepared(client, shop)

        response = client.post(
            f"{CHECKOUTS_URL}/{checkout_id}/payments", json={"idempotency_key": KEY}
        )

        body = response.json()
        assert body["attempt"]["status"] == "UNKNOWN"
        # Not resolved, and the wire says so rather than implying it.
        assert body["attempt"]["resolved_at"] is None
        assert body["attempt"]["failure_code"] is None
        assert client.get(f"{CHECKOUTS_URL}/{checkout_id}").json()["status"] == "OPEN"


async def test_a_lost_response_is_resolved_by_the_reconcile_endpoint(
    catalog_settings: Settings, shop: dict[str, str], token: str
) -> None:
    """The whole timeline over HTTP: charged, lost, retried, queried, paid."""
    provider = FakePaymentProvider(default=FakeOutcome.LOST_RESPONSE)
    with TestClient(create_app(catalog_settings, provider), headers=bearer(token)) as client:
        checkout_id = prepared(client, shop)
        lost = client.post(f"{CHECKOUTS_URL}/{checkout_id}/payments", json={"idempotency_key": KEY})
        attempt_id = lost.json()["attempt"]["id"]
        assert lost.json()["attempt"]["status"] == "UNKNOWN"

        retried = client.post(
            f"{CHECKOUTS_URL}/{checkout_id}/payments", json={"idempotency_key": KEY}
        )
        assert retried.json()["attempt"]["id"] == attempt_id
        assert retried.json()["created"] is False

        response = client.post(f"{PAYMENTS_URL}/{attempt_id}/reconcile")

        assert response.status_code == 200
        body = response.json()
        assert body["resolved"] is True
        assert body["provider_queried"] is True
        assert body["attempt"]["status"] == "SUCCEEDED"
        assert body["attempt"]["outcome_source"] == "RECONCILIATION"
        assert client.get(f"{CHECKOUTS_URL}/{checkout_id}").json()["status"] == "PAID"

    # One charge, one execute, one query. The numbers this endpoint exists to keep at one.
    assert provider.charges == 1
    assert provider.executions_for(KEY) == 1
    assert provider.queries == [KEY]


async def test_reconciling_a_settled_payment_asks_nothing(
    catalog_settings: Settings, shop: dict[str, str], token: str
) -> None:
    provider = FakePaymentProvider(default=FakeOutcome.SUCCESS)
    with TestClient(create_app(catalog_settings, provider), headers=bearer(token)) as client:
        checkout_id = prepared(client, shop)
        paid = client.post(f"{CHECKOUTS_URL}/{checkout_id}/payments", json={"idempotency_key": KEY})

        response = client.post(f"{PAYMENTS_URL}/{paid.json()['attempt']['id']}/reconcile")

        assert response.status_code == 200
        assert response.json()["resolved"] is False
        assert response.json()["provider_queried"] is False
        assert response.json()["attempt"]["status"] == "SUCCEEDED"

    assert provider.queries == []


async def test_a_semantic_denial_refuses_the_payment_without_a_provider(
    catalog_settings: Settings, shop: dict[str, str], token: str
) -> None:
    """A blue charger at the same price: the money is fine and the purchase is not.

    Quoted rather than prepared, because a quote that fails the semantic gate cannot be
    prepared either. The refusal is `payment_not_authorized` rather than `reservation_missing`,
    which is the ordering that matters: a caller is told what is actually wrong rather than
    told to go and hold stock it may never buy.
    """
    provider = FakePaymentProvider(default=FakeOutcome.SUCCESS)
    with TestClient(create_app(catalog_settings, provider), headers=bearer(token)) as client:
        checkout_id = quote(client, shop, variant="blue")

        response = client.post(
            f"{CHECKOUTS_URL}/{checkout_id}/payments", json={"idempotency_key": KEY}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["admitted"] is False
        assert body["refusal"] == "payment_not_authorized"
        assert body["attempt"] is None
        assert body["authorization"]["authorized"] is False
        assert body["authorization"]["intent_authorization"]["satisfied"] is False

    # Nothing external was involved in refusing this.
    assert provider.executions == []


async def test_a_checkout_with_no_hold_refuses_the_payment(
    catalog_settings: Settings, shop: dict[str, str], token: str
) -> None:
    """Paying is not preparing. A quote that holds no stock is refused by name."""
    provider = FakePaymentProvider(default=FakeOutcome.SUCCESS)
    with TestClient(create_app(catalog_settings, provider), headers=bearer(token)) as client:
        checkout_id = quote(client, shop)

        response = client.post(
            f"{CHECKOUTS_URL}/{checkout_id}/payments", json={"idempotency_key": KEY}
        )

        assert response.status_code == 200
        assert response.json()["refusal"] == "reservation_missing"
        assert response.json()["attempt"] is None

    assert provider.executions == []


async def test_a_second_key_while_a_payment_is_open_is_refused(
    catalog_settings: Settings, shop: dict[str, str], token: str
) -> None:
    provider = FakePaymentProvider(default=FakeOutcome.AMBIGUOUS)
    with TestClient(create_app(catalog_settings, provider), headers=bearer(token)) as client:
        checkout_id = prepared(client, shop)
        client.post(f"{CHECKOUTS_URL}/{checkout_id}/payments", json={"idempotency_key": KEY})

        response = client.post(
            f"{CHECKOUTS_URL}/{checkout_id}/payments", json={"idempotency_key": OTHER_KEY}
        )

        assert response.status_code == 200
        assert response.json()["refusal"] == "payment_in_progress"
        assert response.json()["attempt"] is None

    assert provider.executions_for(OTHER_KEY) == 0


async def test_a_payment_reads_back_with_its_frozen_money(
    catalog_settings: Settings, shop: dict[str, str], token: str
) -> None:
    provider = FakePaymentProvider(default=FakeOutcome.SUCCESS)
    with TestClient(create_app(catalog_settings, provider), headers=bearer(token)) as client:
        checkout_id = prepared(client, shop)
        created = client.post(
            f"{CHECKOUTS_URL}/{checkout_id}/payments", json={"idempotency_key": KEY}
        )
        attempt_id = created.json()["attempt"]["id"]

        response = client.get(f"{PAYMENTS_URL}/{attempt_id}")

        assert response.status_code == 200
        assert response.json() == created.json()["attempt"]
        assert response.json()["amount_minor"] == PRICE
        # Never on the wire: the identity a provider was given.
        assert "idempotency_key" not in response.json()


async def test_a_missing_payment_is_a_structured_404(
    catalog_settings: Settings, shop: dict[str, str], token: str
) -> None:
    with TestClient(create_app(catalog_settings), headers=bearer(token)) as client:
        response = client.get(f"{PAYMENTS_URL}/{uuid.uuid7()}")

        assert response.status_code == 404
        assert response.json()["error"] == "not_found"
        assert response.json()["resource"] == "payment_attempt"


async def test_paying_for_a_missing_checkout_is_a_structured_404(
    catalog_settings: Settings, shop: dict[str, str], token: str
) -> None:
    with TestClient(create_app(catalog_settings), headers=bearer(token)) as client:
        response = client.post(
            f"{CHECKOUTS_URL}/{uuid.uuid7()}/payments", json={"idempotency_key": KEY}
        )

        assert response.status_code == 404
        assert response.json()["resource"] == "checkout"


async def test_reconciling_a_payment_that_was_never_dispatched_is_a_409(
    catalog_settings: Settings, shop: dict[str, str], token: str
) -> None:
    """Nothing over HTTP can leave a payment ADMITTED, so this is built through the service.

    The endpoint dispatches whatever it admits, which is why the state is unreachable from the
    wire and why the refusal still has to exist: an internal caller or a future recovery path
    can produce it.
    """
    from agentrank_api.database import create_engine, create_session_factory
    from agentrank_api.payments.admission import PaymentAdmissionService

    engine = create_engine(catalog_settings)
    try:
        factory = create_session_factory(engine)
        with TestClient(create_app(catalog_settings), headers=bearer(token)) as client:
            checkout_id = prepared(client, shop)
            async with factory() as session:
                admission = await PaymentAdmissionService(session).admit_payment(
                    uuid.UUID(checkout_id),
                    merchant_id=uuid.UUID(shop["merchant_id"]),
                    idempotency_key=KEY,
                )
                assert admission.attempt is not None
                attempt_id = admission.attempt.id

            response = client.post(f"{PAYMENTS_URL}/{attempt_id}/reconcile")

            assert response.status_code == 409
            assert response.json()["error"] == "payment_not_dispatched"
            assert response.json()["resource"] == "payment_attempt"
    finally:
        await engine.dispose()


async def test_a_request_cannot_choose_what_the_provider_does(
    catalog_settings: Settings, shop: dict[str, str], token: str
) -> None:
    """The fake is chosen when the application is built and by nothing a caller sends.

    Extra fields are rejected rather than ignored, so an attempt to smuggle one in is a 422
    rather than a silently discarded field that somebody later wires up by accident.
    """
    provider = FakePaymentProvider(default=FakeOutcome.SUCCESS)
    with TestClient(create_app(catalog_settings, provider), headers=bearer(token)) as client:
        checkout_id = prepared(client, shop)

        response = client.post(
            f"{CHECKOUTS_URL}/{checkout_id}/payments",
            json={"idempotency_key": KEY, "simulate": "timeout", "amount_minor": 1},
        )

        # Refused rather than ignored. A caller who states an amount and is answered 200 has been
        # told yes, and the field they invented is one somebody could later wire up by accident.
        assert response.status_code == 422
        assert response.json()["error"] == "invalid_request"


async def test_a_malformed_idempotency_key_is_a_422(
    catalog_settings: Settings, shop: dict[str, str], token: str
) -> None:
    with TestClient(create_app(catalog_settings), headers=bearer(token)) as client:
        checkout_id = prepared(client, shop)

        response = client.post(
            f"{CHECKOUTS_URL}/{checkout_id}/payments", json={"idempotency_key": "short"}
        )

        assert response.status_code == 422


async def test_a_payment_without_a_key_is_admitted_under_a_generated_one(
    catalog_settings: Settings, shop: dict[str, str], token: str
) -> None:
    """Allowed, and not the same as idempotent.

    A generated key is a new identity every time, so the repeat below is a new operation and is
    refused because the first one is still unresolved, rather than being answered with the
    first one's result.
    """
    provider = FakePaymentProvider(default=FakeOutcome.AMBIGUOUS)
    with TestClient(create_app(catalog_settings, provider), headers=bearer(token)) as client:
        checkout_id = prepared(client, shop)

        first = client.post(f"{CHECKOUTS_URL}/{checkout_id}/payments", json={})
        second = client.post(f"{CHECKOUTS_URL}/{checkout_id}/payments", json={})

        assert first.json()["admitted"] is True
        assert first.json()["attempt"]["status"] == "UNKNOWN"
        assert second.json()["admitted"] is False
        assert second.json()["refusal"] == "payment_in_progress"

    assert len(provider.executions) == 1


async def test_the_payment_routes_expose_no_provider_operations(
    catalog_settings: Settings,
) -> None:
    """Five operations and deliberately not a sixth.

    Refund, capture, void and a webhook receiver are all absent because nothing exists behind
    the provider interface to serve them, and an endpoint with nothing behind it is a promise.

    The last two are the Razorpay checkout bridge added in Phase 1I. Neither is a provider
    operation in the sense this test is about. Preparation creates an order for a customer to
    pay against and moves no money. Verification receives what the customer's browser reports,
    proves it, asks Razorpay what actually happened, and converges on exactly the same outcome
    machinery the first three use. Both exist because Standard Checkout is interactive and
    cannot be performed by a server.

    The schema is public and needs no credential to read, which is what lets a client be
    generated before one is issued.
    """
    with TestClient(create_app(catalog_settings)) as client:
        paths = client.get("/openapi.json").json()["paths"]

    payment_paths = {path for path in paths if "payment" in path}
    assert payment_paths == {
        "/api/v1/commerce/checkouts/{checkout_id}/payments",
        "/api/v1/commerce/payments/{attempt_id}",
        "/api/v1/commerce/payments/{attempt_id}/reconcile",
        "/api/v1/commerce/payments/{attempt_id}/razorpay-checkout",
        "/api/v1/commerce/payments/{attempt_id}/razorpay-checkout/verify",
    }
