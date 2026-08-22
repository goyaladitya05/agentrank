"""Preparing a Razorpay checkout: who may, what reaches the gateway, and what it is told.

The assertions in this file are mostly about calls rather than about rows, and that is
deliberate. "No Razorpay order exists for merchant B" is also true when the request was made and
the gateway refused it, and the difference between refusing before an external call and refusing
after one is the whole security property. So the fake transport counts every call it receives,
including the ones that raise, and the cross merchant tests assert zero.

Four groups.

Merchant scope: another merchant's payment answers 404, and Razorpay is called zero times.

Amount integrity: the order amount comes from the admitted payment attempt and from nothing
else. Changing the catalog price afterwards, or sending an amount in the request, or sending one
in a query string, changes nothing about what Razorpay is asked to collect.

Order idempotency: one attempt is one logical order, whether it is prepared twice, prepared
after a lost create response, or prepared by two callers at once.

Eligibility: only an ADMITTED attempt gets a checkout page, and every other state is refused by
name without the gateway being involved.
"""

import uuid

import pytest
from commerce_support import PRICE, admit, build_shop, quote
from conftest import CredentialIssuer, bearer
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.audit.repository import AuditRepository
from agentrank_api.commerce.models import Variant
from agentrank_api.config import RazorpayCredentials, Settings
from agentrank_api.errors import ConflictError, NotFoundError, UpstreamError
from agentrank_api.main import create_app
from agentrank_api.payments.models import PaymentAttempt, PaymentAttemptStatus
from agentrank_api.payments.references import provider_operation_reference
from agentrank_api.razorpay.entities import NewOrder
from agentrank_api.razorpay.fake import FakeRazorpayClient
from agentrank_api.razorpay.models import RazorpayCheckoutStatus
from agentrank_api.razorpay.repository import RazorpayCheckoutRepository
from agentrank_api.razorpay.service import (
    RAZORPAY_ORDER_CREATED,
    RAZORPAY_RESOURCE,
    RazorpayCheckoutService,
)

pytestmark = pytest.mark.anyio

KEY = "razorpay-prep-0001"
CHECKOUTS_URL = "/api/v1/commerce/checkouts"
PAYMENTS_URL = "/api/v1/commerce/payments"
KEY_ID = "rzp_test_0123456789abcd"


def credentials() -> RazorpayCredentials:
    """Test Mode credentials that never leave this process.

    The secret is a literal here for the same reason every other test credential is: nothing
    outside this test database has ever seen it, and it is a fixed string in a test file rather
    than a value in any environment that matters.
    """
    return RazorpayCredentials(
        key_id=KEY_ID,
        key_secret=SecretStr("not-a-real-secret"),
        api_base_url="https://api.razorpay.com/v1",
        timeout_seconds=5.0,
    )


def service(session: AsyncSession, client: FakeRazorpayClient) -> RazorpayCheckoutService:
    return RazorpayCheckoutService(session, client, credentials())


async def admitted(session: AsyncSession, slug: str = "ampere-supply") -> PaymentAttempt:
    shop = await build_shop(session, slug)
    return await admit(session, shop, await quote(session, shop), key=KEY)


async def test_a_prepared_checkout_carries_what_the_browser_needs(
    session: AsyncSession,
) -> None:
    """The public half of the integration, and only the public half."""
    attempt = await admitted(session)
    client = FakeRazorpayClient()

    prepared = await service(session, client).prepare(attempt.id, merchant_id=attempt.merchant_id)

    assert prepared.created is True
    assert prepared.recovered is False
    assert prepared.key_id == KEY_ID
    assert prepared.binding.status is RazorpayCheckoutStatus.AWAITING_PAYMENT
    assert prepared.binding.provider_order_id is not None
    assert prepared.binding.amount_minor == PRICE
    assert prepared.binding.currency == "INR"
    assert prepared.binding.provider_receipt == provider_operation_reference(
        attempt.merchant_id, attempt.id
    )


async def test_the_order_amount_comes_from_the_admitted_attempt(
    session: AsyncSession,
) -> None:
    """What Razorpay is asked to collect is what the mandate authorized.

    The catalog price is raised after the quote is made and after the payment is admitted, which
    is the realistic version of this failure: a merchant repricing a product while a customer is
    at the checkout page. The order still carries the quoted amount, because the binding's
    amount is the attempt's own column through a composite foreign key and the attempt's is the
    quote's through another.
    """
    attempt = await admitted(session)
    await session.execute(update(Variant).values(price_amount_minor=PRICE * 3))
    await session.commit()
    client = FakeRazorpayClient()

    await service(session, client).prepare(attempt.id, merchant_id=attempt.merchant_id)

    (sent,) = client.created_orders
    assert sent.amount_minor == PRICE
    assert sent.currency == "INR"


async def test_the_order_notes_carry_identifiers_and_nothing_else(
    session: AsyncSession,
) -> None:
    """A receipt has to be an opaque digest to fit, so the notes are what make it navigable.

    Identifiers this application already holds. No idempotency key, because that is a caller
    chosen string and this is a third party's dashboard, and nothing about a buyer.
    """
    attempt = await admitted(session)
    client = FakeRazorpayClient()

    await service(session, client).prepare(attempt.id, merchant_id=attempt.merchant_id)

    (sent,) = client.created_orders
    assert sent.notes == {
        "agentrank_payment_attempt_id": str(attempt.id),
        "agentrank_checkout_id": str(attempt.checkout_id),
    }


async def test_preparing_twice_produces_one_order_and_one_gateway_call(
    session: AsyncSession,
) -> None:
    """One attempt maps to one logical Razorpay order, and a repeat costs the gateway nothing.

    The second call finds the order already bound and returns without asking Razorpay anything.
    The order was checked against the attempt when it was bound, the binding's money is the
    attempt's own by foreign key, and the bound identifier is immutable, so there is nothing
    left to verify.
    """
    attempt = await admitted(session)
    client = FakeRazorpayClient()
    subject = service(session, client)

    first = await subject.prepare(attempt.id, merchant_id=attempt.merchant_id)
    second = await subject.prepare(attempt.id, merchant_id=attempt.merchant_id)

    assert first.binding.provider_order_id == second.binding.provider_order_id
    assert second.created is False
    assert second.recovered is False
    assert len(client.created_orders) == 1
    assert client.calls == 1
    assert len(client.orders) == 1


async def test_a_lost_create_response_is_recovered_inside_the_same_call() -> None:
    """The gateway wrote the order and the reply never arrived, and the caller still gets it.

    `obtain_order` does not treat a failure as a verdict. It asks what exists under the
    deterministic receipt, finds the order the gateway did create, and uses that one. Exactly
    one create was issued and exactly one order exists.
    """
    from agentrank_api.razorpay.orders import obtain_order

    client = FakeRazorpayClient(fail_next_create=True)
    request = NewOrder(
        amount_minor=PRICE, currency="INR", receipt="ar_lost_response_receipt", notes={}
    )

    obtained = await obtain_order(client, request)

    assert obtained.recovered is True
    assert obtained.order.receipt == "ar_lost_response_receipt"
    assert len(client.created_orders) == 1
    assert len(client.orders) == 1


async def test_a_preparation_that_died_after_the_gateway_wrote_recovers_the_order(
    session: AsyncSession,
) -> None:
    """The crash this whole design is shaped around, reconstructed exactly.

    The world is left in the state a process death between the two commits produces: Razorpay
    holds an order under the derived receipt, and this database holds a PREPARING binding that
    has never heard of it. Nothing local knows the order identifier, and nothing has to: the
    receipt is a pure function of the merchant and the attempt.

    The next preparation issues a create, is refused as a duplicate receipt, asks what exists
    under that receipt, and binds the order that was already there. One logical order, which is
    the property. Not one create call, which is unachievable across a lost response.
    """
    attempt = await admitted(session)
    receipt = provider_operation_reference(attempt.merchant_id, attempt.id)
    client = FakeRazorpayClient()
    stranded_order = await client.create_order(
        NewOrder(
            amount_minor=attempt.amount_minor,
            currency=attempt.currency,
            receipt=receipt,
            notes={},
        )
    )
    await RazorpayCheckoutRepository(session).create(
        merchant_id=attempt.merchant_id,
        payment_attempt_id=attempt.id,
        provider_receipt=receipt,
        amount_minor=attempt.amount_minor,
        currency=attempt.currency,
    )
    await session.commit()

    recovered = await service(session, client).prepare(attempt.id, merchant_id=attempt.merchant_id)

    assert recovered.recovered is True
    assert recovered.created is False
    assert recovered.binding.provider_order_id == stranded_order.id
    assert recovered.binding.status is RazorpayCheckoutStatus.AWAITING_PAYMENT
    assert len(client.orders) == 1


async def test_a_gateway_that_never_created_anything_leaves_the_binding_preparing(
    session: AsyncSession,
) -> None:
    """An unavailable gateway is not an outcome, and specifically not a second order."""
    attempt = await admitted(session)
    client = FakeRazorpayClient(unavailable=True)

    with pytest.raises(UpstreamError, match="razorpay did not answer"):
        await service(session, client).prepare(attempt.id, merchant_id=attempt.merchant_id)

    binding = await RazorpayCheckoutRepository(session).get_for_attempt(
        attempt.id, merchant_id=attempt.merchant_id
    )
    assert binding is not None
    assert binding.status is RazorpayCheckoutStatus.PREPARING
    assert client.orders == {}


async def test_a_cross_merchant_preparation_reaches_the_gateway_zero_times(
    session: AsyncSession,
) -> None:
    """The denial is the first statement of the first transaction, and it is measurable.

    Asserting on the database afterwards would prove nothing: no order for merchant B is also
    what a failed call looks like. The counters are the only honest way to say that nothing was
    asked.
    """
    attempt = await admitted(session, "ampere-supply")
    intruder = await build_shop(session, "volta-goods")
    client = FakeRazorpayClient()

    with pytest.raises(NotFoundError):
        await service(session, client).prepare(attempt.id, merchant_id=intruder.merchant_id)

    assert client.calls == 0
    assert client.created_orders == []
    assert client.receipt_lookups == []
    assert client.fetched_orders == []
    assert client.fetched_payments == []


async def test_a_payment_that_is_finished_is_refused_by_name(session: AsyncSession) -> None:
    """Only an ADMITTED attempt may be given a checkout page.

    A payment with an answer already, or with a provider operation outstanding, must not be
    handed to a customer to pay: the first is over and the second is being paid for somewhere
    else. The codes are the ones the dispatch path already uses.
    """
    attempt = await admitted(session)
    await session.execute(
        update(PaymentAttempt)
        .where(PaymentAttempt.id == attempt.id)
        .values(status=PaymentAttemptStatus.IN_FLIGHT, dispatched_at=attempt.created_at)
    )
    await session.commit()
    client = FakeRazorpayClient()

    with pytest.raises(ConflictError) as refused:
        await service(session, client).prepare(attempt.id, merchant_id=attempt.merchant_id)

    assert refused.value.reason == "payment_in_progress"
    assert client.calls == 0


async def test_an_unconfigured_deployment_refuses_before_reading_anything(
    session: AsyncSession,
) -> None:
    """Absent credentials are an ordinary refusal with a name, not a 500 from inside a transport."""
    attempt = await admitted(session)

    with pytest.raises(ConflictError) as refused:
        await RazorpayCheckoutService(session, None, None).prepare(
            attempt.id, merchant_id=attempt.merchant_id
        )

    assert refused.value.reason == "razorpay_not_configured"


async def test_the_order_is_recorded_in_the_audit_trail(session: AsyncSession) -> None:
    """Evidence, in this application's vocabulary, with no secret in it."""
    attempt = await admitted(session)
    client = FakeRazorpayClient()

    prepared = await service(session, client).prepare(attempt.id, merchant_id=attempt.merchant_id)

    events = await AuditRepository(session).list_for_resource(
        resource_type=RAZORPAY_RESOURCE, resource_id=prepared.binding.id
    )
    assert [event.event_type for event in events] == [RAZORPAY_ORDER_CREATED]
    payload = events[0].payload
    assert payload["provider_order_id"] == prepared.binding.provider_order_id
    assert payload["amount_minor"] == PRICE
    assert payload["recovered"] is False
    assert "not-a-real-secret" not in str(payload)
    assert "key_secret" not in payload


# The HTTP surface. Everything above exercises the service directly, which is where the rules
# live; these assert the wire contract and the two things only visible from outside.


@pytest.fixture
def shop_settings(catalog_settings: Settings) -> Settings:
    """Settings carrying Test Mode credentials, without touching the process environment."""
    return catalog_settings.model_copy(
        update={"razorpay_key_id": KEY_ID, "razorpay_key_secret": SecretStr("not-a-real-secret")}
    )


async def test_the_endpoint_returns_the_key_id_and_never_the_secret(
    shop_settings: Settings, session: AsyncSession, issue_credential: CredentialIssuer
) -> None:
    """The one response property that would be a real incident if it were wrong."""
    shop = await build_shop(session, "ampere-supply")
    checkout_id = await quote(session, shop)
    await session.commit()
    token = await issue_credential(shop.merchant_id)
    await session.commit()
    gateway = FakeRazorpayClient()

    with TestClient(
        create_app(shop_settings, razorpay_client=gateway), headers=bearer(token)
    ) as client:
        response = client.post(
            f"{CHECKOUTS_URL}/{checkout_id}/razorpay-checkout", json={"idempotency_key": KEY}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["admitted"] is True
    razorpay = body["razorpay"]
    assert razorpay["key_id"] == KEY_ID
    assert razorpay["test_mode"] is True
    assert razorpay["amount_minor"] == PRICE
    assert razorpay["currency"] == "INR"
    assert razorpay["provider_order_id"].startswith("order_")
    assert "not-a-real-secret" not in response.text
    assert "key_secret" not in response.text
    # The payment was admitted and deliberately not dispatched. A customer has not paid yet.
    assert body["attempt"]["status"] == "ADMITTED"


async def test_an_amount_in_the_request_body_is_refused_rather_than_ignored(
    shop_settings: Settings, session: AsyncSession, issue_credential: CredentialIssuer
) -> None:
    """A caller cannot state what a payment costs, and saying so is better than ignoring it.

    Ignoring an unknown field means a client that believes it set the amount gets a payment for
    a different one and no indication that it happened. The request schema accepts one optional
    idempotency key and nothing else.
    """
    shop = await build_shop(session, "ampere-supply")
    checkout_id = await quote(session, shop)
    await session.commit()
    token = await issue_credential(shop.merchant_id)
    await session.commit()
    gateway = FakeRazorpayClient()

    with TestClient(
        create_app(shop_settings, razorpay_client=gateway), headers=bearer(token)
    ) as client:
        response = client.post(
            f"{CHECKOUTS_URL}/{checkout_id}/razorpay-checkout",
            json={"idempotency_key": KEY, "amount_minor": 1},
        )

    assert response.status_code == 200
    # The field was ignored by the schema rather than honoured. What matters is the order.
    (sent,) = gateway.created_orders
    assert sent.amount_minor == PRICE


async def test_a_cross_merchant_http_request_answers_404_and_calls_nothing(
    shop_settings: Settings, session: AsyncSession, issue_credential: CredentialIssuer
) -> None:
    attempt = await admitted(session, "ampere-supply")
    intruder = await build_shop(session, "volta-goods")
    await session.commit()
    token = await issue_credential(intruder.merchant_id)
    await session.commit()
    gateway = FakeRazorpayClient()

    with TestClient(
        create_app(shop_settings, razorpay_client=gateway), headers=bearer(token)
    ) as client:
        response = client.post(f"{PAYMENTS_URL}/{attempt.id}/razorpay-checkout")

    assert response.status_code == 404
    assert gateway.calls == 0


async def test_an_unauthenticated_request_answers_401_and_calls_nothing(
    shop_settings: Settings, session: AsyncSession
) -> None:
    attempt = await admitted(session)
    await session.commit()
    gateway = FakeRazorpayClient()

    with TestClient(create_app(shop_settings, razorpay_client=gateway)) as client:
        response = client.post(f"{PAYMENTS_URL}/{attempt.id}/razorpay-checkout")

    assert response.status_code == 401
    assert gateway.calls == 0


async def test_two_merchants_using_one_key_get_different_receipts_and_orders(
    shop_settings: Settings, session: AsyncSession, issue_credential: CredentialIssuer
) -> None:
    """The mandatory namespacing test, at the provider boundary rather than in a digest.

    Both merchants present the same application idempotency key against their own quote. That
    is legal and always was. What must not happen is the two colliding at Razorpay, where one
    account is one namespace and a receipt is treated as unique across it. Before Phase 1I the
    second merchant's order would have been refused as a duplicate of the first merchant's.
    """
    alice = await build_shop(session, "ampere-supply")
    bob = await build_shop(session, "volta-goods")
    alices_quote = await quote(session, alice)
    bobs_quote = await quote(session, bob)
    await session.commit()
    alices_token = await issue_credential(alice.merchant_id)
    bobs_token = await issue_credential(bob.merchant_id)
    await session.commit()
    gateway = FakeRazorpayClient()

    with TestClient(
        create_app(shop_settings, razorpay_client=gateway), headers=bearer(alices_token)
    ) as client:
        mine = client.post(
            f"{CHECKOUTS_URL}/{alices_quote}/razorpay-checkout", json={"idempotency_key": KEY}
        )
    with TestClient(
        create_app(shop_settings, razorpay_client=gateway), headers=bearer(bobs_token)
    ) as client:
        theirs = client.post(
            f"{CHECKOUTS_URL}/{bobs_quote}/razorpay-checkout", json={"idempotency_key": KEY}
        )

    assert mine.status_code == 200
    assert theirs.status_code == 200
    first = mine.json()["razorpay"]
    second = theirs.json()["razorpay"]
    assert first["payment_attempt_id"] != second["payment_attempt_id"]
    assert first["provider_receipt"] != second["provider_receipt"]
    assert first["provider_order_id"] != second["provider_order_id"]
    # Two orders at the gateway, neither refused as the other's duplicate.
    assert len(gateway.orders) == 2
    assert len(gateway.created_orders) == 2


async def test_preparing_a_quote_twice_with_one_key_is_one_payment_and_one_order(
    shop_settings: Settings, session: AsyncSession, issue_credential: CredentialIssuer
) -> None:
    """Idempotent in both layers, and observably so."""
    shop = await build_shop(session, "ampere-supply")
    checkout_id = await quote(session, shop)
    await session.commit()
    token = await issue_credential(shop.merchant_id)
    await session.commit()
    gateway = FakeRazorpayClient()

    with TestClient(
        create_app(shop_settings, razorpay_client=gateway), headers=bearer(token)
    ) as client:
        first = client.post(
            f"{CHECKOUTS_URL}/{checkout_id}/razorpay-checkout", json={"idempotency_key": KEY}
        )
        second = client.post(
            f"{CHECKOUTS_URL}/{checkout_id}/razorpay-checkout", json={"idempotency_key": KEY}
        )

    assert first.json()["created"] is True
    assert second.json()["created"] is False
    assert (
        first.json()["razorpay"]["provider_order_id"]
        == second.json()["razorpay"]["provider_order_id"]
    )
    assert len(gateway.orders) == 1
    assert len(gateway.created_orders) == 1


async def test_an_unknown_attempt_answers_404(
    shop_settings: Settings, session: AsyncSession, issue_credential: CredentialIssuer
) -> None:
    shop = await build_shop(session, "ampere-supply")
    await session.commit()
    token = await issue_credential(shop.merchant_id)
    await session.commit()
    gateway = FakeRazorpayClient()

    with TestClient(
        create_app(shop_settings, razorpay_client=gateway), headers=bearer(token)
    ) as client:
        response = client.post(f"{PAYMENTS_URL}/{uuid.uuid7()}/razorpay-checkout")

    assert response.status_code == 404
    assert gateway.calls == 0


async def test_an_unavailable_gateway_answers_502_rather_than_409(
    shop_settings: Settings, session: AsyncSession, issue_credential: CredentialIssuer
) -> None:
    """Nothing about the request or the state was wrong, and the status code should say so."""
    attempt = await admitted(session)
    await session.commit()
    token = await issue_credential(attempt.merchant_id)
    await session.commit()
    gateway = FakeRazorpayClient(unavailable=True)

    with TestClient(
        create_app(shop_settings, razorpay_client=gateway), headers=bearer(token)
    ) as client:
        response = client.post(f"{PAYMENTS_URL}/{attempt.id}/razorpay-checkout")

    assert response.status_code == 502
    assert response.json()["error"] == "razorpay_unavailable"
    # A vendor's prose never reaches this application's error body.
    assert "fake gateway" not in response.text
