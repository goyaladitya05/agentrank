"""The Razorpay transport, exercised against a mock at the socket rather than a mock of itself.

Every request here goes through the real `HttpRazorpayClient` and stops at an `httpx2`
transport that answers from a table. That is the only way to assert the things worth asserting
about an adapter: the method, the path, the query string, the body shape and the authorization
header are all part of the contract with the vendor, and a test that stubbed the client would
check none of them.

Three groups of assertions.

The first is that the documented API is what this speaks: minor unit amounts, a receipt on the
order, `GET /orders?receipt=`, `GET /orders/{id}/payments`, basic authentication carrying the key
pair.

The second is error classification, which is the part that costs money if it is wrong. A
timeout, a 429 and a 502 must all be ambiguous, because a gateway that did not answer may still
have created the order and treating that as a definitive failure is how a second order for one
payment comes to exist. A 400 must be definitive.

The third is that responses are parsed rather than trusted. This application decides whether a
merchant's stock leaves the shelf partly on the strength of these fields, so an amount that
arrives as a string is a refusal rather than a value some later comparison quietly mishandles.
"""

import base64
import json
from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr

from agentrank_api.config import RazorpayCredentials
from agentrank_api.razorpay.client import HttpRazorpayClient
from agentrank_api.razorpay.entities import NewOrder
from agentrank_api.razorpay.errors import (
    RazorpayRefusedError,
    RazorpayUnavailableError,
    RazorpayUnreadableError,
)

pytestmark = pytest.mark.anyio

KEY_ID = "rzp_test_0123456789abcd"
KEY_SECRET = "not-a-real-secret"
BASE_URL = "https://api.razorpay.com/v1"
RECEIPT = "ar_abcdefghijklmnopqrstuvwxyz234567"
AMOUNT = 499900

ORDER_BODY: dict[str, Any] = {
    "id": "order_ABC123",
    "entity": "order",
    "amount": AMOUNT,
    "amount_paid": 0,
    "amount_due": AMOUNT,
    "currency": "INR",
    "receipt": RECEIPT,
    "status": "created",
    "attempts": 0,
    "notes": {},
    "created_at": 1756000000,
}

PAYMENT_BODY: dict[str, Any] = {
    "id": "pay_XYZ789",
    "entity": "payment",
    "amount": AMOUNT,
    "currency": "INR",
    "status": "captured",
    "order_id": "order_ABC123",
    "method": "card",
    "captured": True,
    "error_code": None,
    "error_description": None,
    "created_at": 1756000100,
}


def credentials() -> RazorpayCredentials:
    return RazorpayCredentials(
        key_id=KEY_ID,
        key_secret=SecretStr(KEY_SECRET),
        api_base_url=BASE_URL,
        timeout_seconds=5.0,
    )


def client_answering(
    handler: Callable[[httpx2.Request], httpx2.Response],
) -> tuple[HttpRazorpayClient, list[httpx2.Request]]:
    """A real adapter whose socket is a function, plus the requests that reached it."""
    seen: list[httpx2.Request] = []

    def record(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return handler(request)

    transport = httpx2.MockTransport(record)
    inner = httpx2.AsyncClient(
        base_url=BASE_URL,
        auth=httpx2.BasicAuth(KEY_ID, KEY_SECRET),
        transport=transport,
    )
    return HttpRazorpayClient(credentials(), client=inner), seen


def answering(status: int, body: Any) -> Callable[[httpx2.Request], httpx2.Response]:
    return lambda _: httpx2.Response(status, json=body)


def an_order() -> NewOrder:
    return NewOrder(
        amount_minor=AMOUNT,
        currency="INR",
        receipt=RECEIPT,
        notes={"agentrank_payment_attempt_id": "01a02696-916e-703a-8ab5-60c7a93eee4f"},
    )


async def test_creating_an_order_speaks_the_documented_api() -> None:
    """Method, path, minor unit amount, receipt and notes, exactly as documented."""
    client, seen = client_answering(answering(200, ORDER_BODY))

    order = await client.create_order(an_order())

    assert order.id == "order_ABC123"
    assert order.amount_minor == AMOUNT
    assert order.currency == "INR"
    assert order.receipt == RECEIPT
    assert order.status == "created"

    (sent,) = seen
    assert sent.method == "POST"
    assert sent.url.path == "/v1/orders"
    body = json.loads(sent.content)
    assert body == {
        "amount": AMOUNT,
        "currency": "INR",
        "receipt": RECEIPT,
        "notes": {"agentrank_payment_attempt_id": "01a02696-916e-703a-8ab5-60c7a93eee4f"},
    }


async def test_the_key_pair_is_sent_as_basic_authentication() -> None:
    """Razorpay authenticates with the key id and secret as an HTTP basic pair.

    Asserted at the header rather than at the constructor, because what matters is what leaves
    the process. The secret is unwrapped exactly once, into the auth object, and this is the
    only place in the suite that looks at the result.
    """
    client, seen = client_answering(answering(200, ORDER_BODY))

    await client.create_order(an_order())

    (sent,) = seen
    expected = base64.b64encode(f"{KEY_ID}:{KEY_SECRET}".encode()).decode()
    assert sent.headers["authorization"] == f"Basic {expected}"


async def test_finding_an_order_by_receipt_filters_exactly() -> None:
    """Razorpay documents the receipt filter as a contains match, so narrow it here.

    A near miss is somebody else's order, and using one would attach this application's payment
    attempt to a provider order it did not create.
    """
    other = ORDER_BODY | {"id": "order_OTHER", "receipt": f"{RECEIPT}_suffix"}
    client, seen = client_answering(
        answering(200, {"entity": "collection", "count": 2, "items": [other, ORDER_BODY]})
    )

    found = await client.find_order_by_receipt(RECEIPT)

    assert found is not None
    assert found.id == "order_ABC123"
    (sent,) = seen
    assert sent.method == "GET"
    assert sent.url.path == "/v1/orders"
    assert sent.url.params["receipt"] == RECEIPT


async def test_finding_an_order_that_only_contains_the_receipt_is_not_a_hit() -> None:
    other = ORDER_BODY | {"id": "order_OTHER", "receipt": f"{RECEIPT}_suffix"}
    client, _ = client_answering(
        answering(200, {"entity": "collection", "count": 1, "items": [other]})
    )

    assert await client.find_order_by_receipt(RECEIPT) is None


async def test_fetching_a_missing_order_is_absence_rather_than_a_refusal() -> None:
    """A 404 on a fetch is an ordinary answer and must not look like a broken integration."""
    client, _ = client_answering(answering(404, {"error": {"code": "BAD_REQUEST_ERROR"}}))

    assert await client.fetch_order("order_MISSING") is None
    assert await client.fetch_payment("pay_MISSING") is None


async def test_fetching_the_payments_on_an_order() -> None:
    failed = PAYMENT_BODY | {
        "id": "pay_FAILED",
        "status": "failed",
        "captured": False,
        "error_code": "BAD_REQUEST_ERROR",
    }
    client, seen = client_answering(
        answering(200, {"entity": "collection", "count": 2, "items": [PAYMENT_BODY, failed]})
    )

    payments = await client.fetch_order_payments("order_ABC123")

    assert [payment.id for payment in payments] == ["pay_XYZ789", "pay_FAILED"]
    assert payments[0].status == "captured"
    assert payments[0].captured is True
    assert payments[1].status == "failed"
    assert payments[1].error_code == "BAD_REQUEST_ERROR"
    (sent,) = seen
    assert sent.url.path == "/v1/orders/order_ABC123/payments"


async def test_fetching_one_payment() -> None:
    client, seen = client_answering(answering(200, PAYMENT_BODY))

    payment = await client.fetch_payment("pay_XYZ789")

    assert payment is not None
    assert payment.order_id == "order_ABC123"
    assert payment.amount_minor == AMOUNT
    assert payment.currency == "INR"
    (sent,) = seen
    assert sent.url.path == "/v1/payments/pay_XYZ789"


async def test_a_definitive_refusal_carries_the_documented_envelope() -> None:
    """A duplicate receipt is the refusal this integration will actually meet.

    The code and the description are kept because an operator reading a log needs them. They
    never reach a response body: a vendor's prose in this application's error shape is a
    vendor's prose in a buyer agent's parser.
    """
    body = {
        "error": {
            "code": "BAD_REQUEST_ERROR",
            "description": (
                "An order with the same receipt value has already been created on this account"
            ),
        }
    }
    client, _ = client_answering(answering(400, body))

    with pytest.raises(RazorpayRefusedError) as refused:
        await client.create_order(an_order())

    assert refused.value.status_code == 400
    assert refused.value.code == "BAD_REQUEST_ERROR"
    assert "same receipt" in refused.value.description


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
async def test_a_gateway_that_may_still_have_acted_is_ambiguous(status: int) -> None:
    """The expensive classification, stated as its own test.

    A 5xx from a payment gateway does not mean the order was not created. Treating it as a
    definitive failure and creating another one is how one payment attempt comes to have two
    provider orders, which is exactly what the deterministic receipt exists to prevent.
    """
    client, _ = client_answering(answering(status, {"error": {"code": "SERVER_ERROR"}}))

    with pytest.raises(RazorpayUnavailableError):
        await client.create_order(an_order())


async def test_a_transport_failure_is_ambiguous_rather_than_an_exception_to_the_rule() -> None:
    def explode(_: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectTimeout("no answer")

    client, _ = client_answering(explode)

    with pytest.raises(RazorpayUnavailableError):
        await client.create_order(an_order())


@pytest.mark.parametrize(
    "body",
    [
        {"id": "order_ABC123", "amount": "499900", "currency": "INR", "status": "created"},
        {"id": "order_ABC123", "amount": True, "currency": "INR", "status": "created"},
        {"id": "", "amount": AMOUNT, "currency": "INR", "status": "created"},
        {"id": "order_ABC123", "amount": AMOUNT, "currency": "INR"},
        ["not", "an", "object"],
    ],
)
async def test_a_response_that_is_not_the_entity_is_refused(body: Any) -> None:
    """Untrusted input, and specifically input a stock decision partly rests on.

    A boolean amount is in the list because `bool` is a subclass of `int` in Python, so the
    obvious isinstance check accepts `True` as an amount of one.
    """
    client, _ = client_answering(answering(200, body))

    with pytest.raises(RazorpayUnreadableError):
        await client.create_order(an_order())


async def test_a_collection_without_items_is_refused_rather_than_read_as_empty() -> None:
    """Because "no payments on this order" and "the shape changed" call for opposite actions."""
    client, _ = client_answering(answering(200, {"entity": "collection", "count": 0}))

    with pytest.raises(RazorpayUnreadableError):
        await client.fetch_order_payments("order_ABC123")


def test_an_order_request_refuses_what_razorpay_would_reject() -> None:
    """Refused here rather than round tripped, because a rejection costs a network call.

    The receipt bound is Razorpay's documented 40 characters. The derived operation reference is
    35, so this is headroom rather than a limit anything real is near, and it is checked so that
    a future change to the derivation fails in a unit test rather than at a gateway.
    """
    with pytest.raises(ValueError, match="positive"):
        NewOrder(amount_minor=0, currency="INR", receipt=RECEIPT, notes={})
    with pytest.raises(ValueError, match="ISO 4217"):
        NewOrder(amount_minor=AMOUNT, currency="inr", receipt=RECEIPT, notes={})
    with pytest.raises(ValueError, match="characters"):
        NewOrder(amount_minor=AMOUNT, currency="INR", receipt="x" * 41, notes={})
    with pytest.raises(ValueError, match="at most 15 notes"):
        NewOrder(
            amount_minor=AMOUNT,
            currency="INR",
            receipt=RECEIPT,
            notes={f"k{index}": "v" for index in range(16)},
        )
