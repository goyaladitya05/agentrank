"""The one place an HTTP request reaches Razorpay.

Five operations, and deliberately not a sixth. Create an order, find one by receipt, fetch one
by identifier, list the payments on one, fetch one payment. That is exactly what preparing a
Standard Checkout and confirming its result require. There is no refund, no capture, no payout,
no subscription, no payment link, no settlement and no webhook registration, because nothing
calls them and a method with no caller is a guess about a vendor's API rather than a boundary.

No service builds a URL, sets a header or reads a status code. Everything above this module
talks to `RazorpayClient`, gets frozen entities back, and gets one of three error classes when
something goes wrong. That is what makes the whole integration testable without a network: the
fake in `agentrank_api.razorpay.fake` implements this and counts calls.

## Why a REST adapter and not the SDK

Razorpay publishes a Python SDK. It is a synchronous `requests` client, and this application is
asynchronous end to end: an async SQLAlchemy session, an async FastAPI handler, an async
provider interface whose whole point is that a network call happens with no transaction open.
Calling a blocking client from inside the event loop would stall every other request for the
duration of a payment gateway round trip, and the alternative of pushing it to a thread pool
adds a concurrency model to avoid writing five HTTP calls. The SDK also brings its own exception
taxonomy, which would have to be translated into the three classes above anyway.

So this is about four hundred lines of adapter against five documented endpoints, with the same
`httpx2` already in the dependency tree for the test client, and no new dependency at all. See
docs/decisions.md.

## What this adapter refuses to do

It does not retry. Not on a timeout, not on a 5xx, not on a 429. `create_order` is a side effect
and a retry is how two orders come to exist for one payment; the recovery path is to ask what
exists under the deterministic receipt, which is a different operation with a different name.
The read operations could safely be retried and are not, because a retry policy that applies to
some methods and not others is a footgun sitting inside a boundary whose entire purpose is being
obvious.

It does not log. A transport that logged request bodies would eventually log a key, and one that
logged responses would log a buyer's contact details. What is worth recording is recorded by the
service, in the audit trail, in this application's own vocabulary.

The key secret is unwrapped once, at construction, into an `httpx2.BasicAuth` that holds it.
Nothing here formats it into a string, puts it in a header by hand, or returns it.
"""

from types import TracebackType
from typing import Any, Protocol, Self

import httpx2

from agentrank_api.config import RazorpayCredentials
from agentrank_api.razorpay.entities import (
    NewOrder,
    RazorpayOrder,
    RazorpayPayment,
    parse_collection,
)
from agentrank_api.razorpay.errors import (
    RazorpayRefusedError,
    RazorpayUnavailableError,
    RazorpayUnreadableError,
)

# Razorpay caps a listing at 100 and defaults it to 10. A receipt is unique on the account, so
# one exact match is all there can be, and asking for a handful leaves room for the documented
# behaviour of the filter without ever walking a page.
RECEIPT_SEARCH_COUNT = 10

# The status codes that prove nothing. A gateway timeout, a rate limit and a server error may
# each have been preceded by the operation actually happening, so none of them may be treated as
# a definitive refusal and none may be answered by sending the request again.
_AMBIGUOUS_STATUSES = frozenset({408, 425, 429}) | frozenset(range(500, 600))


class RazorpayClient(Protocol):
    """The narrow contract every Razorpay transport implements.

    A `Protocol` rather than a base class, matching `PaymentProvider`: there is no behaviour to
    inherit, and structural typing means a transport is anything with these methods rather than
    anything that remembered to subclass.

    Every method is asked outside any database transaction. A transaction held open across a
    payment gateway round trip holds its locks for as long as the gateway takes.
    """

    async def create_order(self, order: NewOrder) -> RazorpayOrder:
        """Create one Razorpay Order, or fail in a way that says what may be concluded.

        Not idempotent, and this interface does not pretend otherwise. Razorpay treats the
        receipt as unique on the account, so a second call under one receipt is refused rather
        than replayed, and a caller that did not hear back must recover with
        `find_order_by_receipt` rather than by creating again under a fresh receipt.

        Raises `RazorpayUnavailableError` when nothing may be concluded, `RazorpayRefusedError` when
        Razorpay answered definitively, and `RazorpayUnreadableError` when the answer was not an
        order.
        """
        ...

    async def find_order_by_receipt(self, receipt: str) -> RazorpayOrder | None:
        """The order carrying exactly this receipt, or None if the account has none.

        The recovery half of order creation. `receipt` is the derived operation reference, so
        this asks Razorpay a question this application can always reconstruct: is there already
        an order for this payment attempt.

        Exactly this receipt. Razorpay documents the filter as retrieving orders that contain
        the value, so the match is narrowed here rather than trusted, and an inexact hit is not
        a hit.
        """
        ...

    async def fetch_order(self, order_id: str) -> RazorpayOrder | None:
        """One order by identifier, or None if Razorpay has no such order."""
        ...

    async def fetch_order_payments(self, order_id: str) -> tuple[RazorpayPayment, ...]:
        """Every payment Razorpay has recorded against one order, successful or not."""
        ...

    async def fetch_payment(self, payment_id: str) -> RazorpayPayment | None:
        """One payment by identifier, or None if Razorpay has no such payment.

        The authoritative half of confirming a Standard Checkout result. A verified signature
        proves a callback is authentic; this is what says what actually happened.
        """
        ...


class HttpRazorpayClient:
    """A Razorpay transport over the documented REST API.

    One `httpx2.AsyncClient` for the life of the application, so connections are pooled rather
    than established per payment. It is closed by the application lifespan, which is why
    `aclose` exists and why it is not on the protocol: closing is a property of this
    implementation and the fake has nothing to close.
    """

    def __init__(
        self, credentials: RazorpayCredentials, *, client: httpx2.AsyncClient | None = None
    ) -> None:
        self._client = client or httpx2.AsyncClient(
            base_url=credentials.api_base_url,
            # Unwrapped once, into an object that holds it. Nothing below formats it.
            auth=httpx2.BasicAuth(credentials.key_id, credentials.key_secret.get_secret_value()),
            timeout=credentials.timeout_seconds,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def create_order(self, order: NewOrder) -> RazorpayOrder:
        body = await self._request("POST", "/orders", json=order.to_payload())
        return RazorpayOrder.parse(body)

    async def find_order_by_receipt(self, receipt: str) -> RazorpayOrder | None:
        body = await self._request(
            "GET", "/orders", params={"receipt": receipt, "count": RECEIPT_SEARCH_COUNT}
        )
        for item in parse_collection(body, "order"):
            found = RazorpayOrder.parse(item)
            # Exact, because the documented filter is a contains match and a near miss is
            # somebody else's order.
            if found.receipt == receipt:
                return found
        return None

    async def fetch_order(self, order_id: str) -> RazorpayOrder | None:
        body = await self._request("GET", f"/orders/{order_id}", absent_on=404)
        return None if body is None else RazorpayOrder.parse(body)

    async def fetch_order_payments(self, order_id: str) -> tuple[RazorpayPayment, ...]:
        body = await self._request("GET", f"/orders/{order_id}/payments")
        return tuple(RazorpayPayment.parse(item) for item in parse_collection(body, "payment"))

    async def fetch_payment(self, payment_id: str) -> RazorpayPayment | None:
        body = await self._request("GET", f"/payments/{payment_id}", absent_on=404)
        return None if body is None else RazorpayPayment.parse(body)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        absent_on: int | None = None,
    ) -> Any:
        """One round trip, with every failure mode sorted into what it lets a caller conclude.

        `absent_on` names the status that means "no such entity" for this call rather than "your
        request was wrong". It is a parameter rather than a blanket rule because a 404 on a
        fetch is an ordinary answer and a 404 on a create would be a broken base URL, and
        collapsing the two would turn a misconfiguration into a silent empty result.
        """
        try:
            response = await self._client.request(method, path, json=json, params=params)
        except httpx2.RequestError as failure:
            # Timeouts, resets, DNS, TLS. The request may have been performed.
            raise RazorpayUnavailableError(f"razorpay {method} {path} did not answer") from failure

        if response.status_code == absent_on:
            return None
        if response.status_code in _AMBIGUOUS_STATUSES:
            raise RazorpayUnavailableError(
                f"razorpay {method} {path} answered {response.status_code}"
            )
        if response.status_code >= 400:
            raise _refusal(response)
        return _body(response)


def _body(response: httpx2.Response) -> Any:
    try:
        return response.json()
    except ValueError as failure:
        raise RazorpayUnreadableError("razorpay answered with a body that is not JSON") from failure


def _refusal(response: httpx2.Response) -> RazorpayRefusedError:
    """A definitive refusal, with the documented envelope read out of it if it is there.

    Razorpay documents errors as `{"error": {"code", "description", ...}}`. A refusal whose body
    does not follow that is still a refusal: the status code already said so, and inventing an
    `RazorpayUnreadableError` here would replace a fact with a puzzle.
    """
    code = "UNKNOWN"
    description = response.text[:200]
    try:
        parsed = response.json()
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        error = parsed.get("error")
        if isinstance(error, dict):
            if isinstance(error.get("code"), str):
                code = error["code"]
            if isinstance(error.get("description"), str):
                description = error["description"]
    return RazorpayRefusedError(response.status_code, code, description)
