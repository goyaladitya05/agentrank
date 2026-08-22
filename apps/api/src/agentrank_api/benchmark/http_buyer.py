"""The same eight buyer operations, over the wire, as an authenticated merchant integration.

`MerchantBuyerSurface` calls the application services directly. This calls the endpoints those
services sit behind, with a merchant API credential in an Authorization header, and it is what an
executor outside this process is given. There is no second API here: every method is one existing
route, and a benchmark that measured a private shortcut would be measuring something no buyer can
actually use.

The whole reason it exists is that a protocol is not a boundary. An in process surface holds a
session factory, and anything holding it can open a session with one call, so "the executor
cannot reach the database" rests on nobody reaching for it. Over HTTP there is nothing to reach:
the process on the other end has a base URL, a bearer token and a merchant identifier, and every
answer it gets is a serialized view model. Authentication and merchant scoping are the ones Phase
1H built, unchanged, so a benchmark credential is an ordinary merchant credential and a call for
somebody else's resource is a 404 exactly as it is for anybody.

Translation back into this application's own exception types is the load bearing part, and it is
deliberately the inverse of the handlers `create_app` installs:

```text
404   NotFoundError        the resource named does not exist for this merchant
409   ConflictError        the state refused a well formed request
401   AuthenticationError  this caller is not a merchant
502   UpstreamError        a system neither party controls did not cooperate
else  MerchantSurfaceError the surface did not answer at all
```

An executor reads `NotFoundError` and `ConflictError` to tell a variant the merchant does not
sell from one it has run out of, so an HTTP surface that returned something of its own would be
changing what a buyer can see. And `MerchantSurfaceError` is not one of the application's
exceptions on purpose: a 500, a connection that was refused, a request that timed out and a body
nothing could parse are the same fact about a merchant, and none of them is a business answer.

Nothing here logs the token, puts it in an exception message, or writes it to a repr. The client
is constructed with the header already on it, which is what keeps it out of every call site.
"""

import uuid
from types import TracebackType
from typing import Any, Self

import httpx2

from agentrank_api.checkout.schemas import (
    CheckoutView,
    CreateCheckoutRequest,
    ExecutionPreparationView,
)
from agentrank_api.commerce.schemas import (
    ProductDetail,
    ProductSearchRequest,
    ProductSearchResponse,
)
from agentrank_api.constraints.schemas import (
    CreateIntentConstraintsRequest,
    IntentConstraintSetView,
)
from agentrank_api.errors import (
    AgentRankError,
    AuthenticationError,
    ConflictError,
    NotFoundError,
    UpstreamError,
)
from agentrank_api.mandates.schemas import CreateMandateRequest, MandateView
from agentrank_api.payments.schemas import CreatePaymentRequest, PaymentView

COMMERCE = "/api/v1/commerce"

# Long enough that a payment through the real kernel under load does not lapse, short enough that
# a merchant which has stopped answering is reported rather than waited on forever. A benchmark
# that hung on one mission would produce no result at all, which is worse than a recorded fault.
DEFAULT_TIMEOUT = 30.0


class MerchantSurfaceError(AgentRankError):
    """The merchant surface did not answer, and what it did instead was not a business answer.

    Its own class rather than one of the four the application raises, because none of them fits
    and reusing one would launder a failure into a refusal. A 500, a refused connection, a
    request that timed out and a body nothing could parse all mean the merchant surface failed
    rather than said no, which is what the tool boundary attributes to the merchant.

    It carries no response body. A merchant's prose in this application's exception is a
    merchant's prose in a benchmark report, and it changes without notice.
    """

    def __init__(self, operation: str, detail: str) -> None:
        super().__init__(f"{operation}: {detail}")
        self.operation = operation
        self.detail = detail


def authenticated_client(
    base_url: str, token: str, *, timeout: float = DEFAULT_TIMEOUT
) -> httpx2.AsyncClient:
    """A client that presents one merchant credential on every request.

    The header is set here and never at a call site, so no method in this module handles the
    secret and nothing can log it by accident.
    """
    return httpx2.AsyncClient(
        base_url=base_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    )


class HttpBuyerCommerceSurface:
    """Everything a buyer may do at one merchant, over that merchant's own commerce API.

    Constructed with the merchant it is acting for rather than discovering it, because there is
    no whoami route and adding one would be adding an endpoint for the benchmark's convenience.
    The credential decides what the server will actually allow; this identifier only says what
    the caller believes, and an executor that disagreed with it is checked by the executor
    itself and, failing that, by every 404 the server gives.
    """

    def __init__(self, client: httpx2.AsyncClient, *, merchant_id: uuid.UUID) -> None:
        self._client = client
        self._merchant_id = merchant_id

    @property
    def merchant_id(self) -> uuid.UUID:
        return self._merchant_id

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._client.aclose()

    async def search_products(self, request: ProductSearchRequest) -> ProductSearchResponse:
        body = await self._post(
            "search_products", f"{COMMERCE}/products/search", request.model_dump(mode="json")
        )
        return ProductSearchResponse.model_validate(body)

    async def get_product(self, product_id: uuid.UUID) -> ProductDetail:
        body = await self._get("get_product", f"{COMMERCE}/products/{product_id}")
        return ProductDetail.model_validate(body)

    async def authorize_spending(self, request: CreateMandateRequest) -> MandateView:
        body = await self._post(
            "authorize_spending", f"{COMMERCE}/mandates", request.model_dump(mode="json")
        )
        return MandateView.model_validate(body)

    async def state_requirements(
        self, mandate_id: uuid.UUID, request: CreateIntentConstraintsRequest
    ) -> IntentConstraintSetView:
        body = await self._post(
            "state_requirements",
            f"{COMMERCE}/mandates/{mandate_id}/constraints",
            request.model_dump(mode="json"),
        )
        return IntentConstraintSetView.model_validate(body)

    async def create_checkout(self, request: CreateCheckoutRequest) -> CheckoutView:
        body = await self._post(
            "create_checkout", f"{COMMERCE}/checkouts", request.model_dump(mode="json")
        )
        return CheckoutView.model_validate(body)

    async def get_checkout(self, checkout_id: uuid.UUID) -> CheckoutView:
        body = await self._get("get_checkout", f"{COMMERCE}/checkouts/{checkout_id}")
        return CheckoutView.model_validate(body)

    async def prepare_checkout(self, checkout_id: uuid.UUID) -> ExecutionPreparationView:
        body = await self._post(
            "prepare_checkout", f"{COMMERCE}/checkouts/{checkout_id}/prepare-execution", None
        )
        return ExecutionPreparationView.model_validate(body)

    async def complete_checkout(
        self, checkout_id: uuid.UUID, request: CreatePaymentRequest
    ) -> PaymentView:
        body = await self._post(
            "complete_checkout",
            f"{COMMERCE}/checkouts/{checkout_id}/payments",
            request.model_dump(mode="json"),
        )
        return PaymentView.model_validate(body)

    async def _get(self, operation: str, path: str) -> Any:
        return await self._answer(operation, self._client.build_request("GET", path))

    async def _post(self, operation: str, path: str, payload: Any) -> Any:
        request = self._client.build_request("POST", path, json={} if payload is None else payload)
        return await self._answer(operation, request)

    async def _answer(self, operation: str, request: httpx2.Request) -> Any:
        """Send one request and turn the response back into an answer or an exception.

        Every transport failure is a `MerchantSurfaceError`. That includes a timeout, which is
        the one worth naming: a request that timed out may still have been carried out, and for
        the payment call that means money may have moved. Nothing here decides what to do about
        that. The tool boundary records that the call was dispatched, and the runner refuses to
        tidy away a mission that reached it.
        """
        try:
            response = await self._client.send(request)
        except httpx2.HTTPError as transport:
            raise MerchantSurfaceError(operation, type(transport).__name__) from transport

        if response.status_code < 400:
            try:
                return response.json()
            except ValueError as unreadable:
                raise MerchantSurfaceError(operation, "the response body was not JSON") from (
                    unreadable
                )
        raise _refusal(operation, response)


def _refusal(operation: str, response: httpx2.Response) -> AgentRankError:
    """The exception this status code means, using this application's own vocabulary.

    The inverse of the handlers `create_app` installs, so an executor sees the same exception it
    would have seen in process and its own handling of a refusal keeps working unchanged.

    A body that does not parse is not a reason to guess. A 404 with an unreadable body is still a
    404, and the fields it would have carried are filled with what is known rather than invented.
    """
    body = _fields(response)
    reason = str(body.get("error") or "unspecified")
    detail = str(body.get("detail") or f"{operation} was refused")
    resource = body.get("resource")
    identifier = body.get("identifier")

    if response.status_code == 401:
        return AuthenticationError()
    if response.status_code == 404:
        return NotFoundError(
            str(resource) if resource else "resource",
            str(identifier) if identifier else operation,
        )
    if response.status_code == 409:
        return ConflictError(
            reason,
            detail,
            resource=None if resource is None else str(resource),
            identifier=None if identifier is None else str(identifier),
        )
    if response.status_code == 502:
        return UpstreamError(reason, detail)
    # 400, 422, 429, 500 and everything else. A request this application built and the merchant
    # would not answer is the merchant surface failing rather than refusing, and a benchmark that
    # called a 422 a business answer would report a commerce finding for its own bad request.
    return MerchantSurfaceError(operation, f"HTTP {response.status_code}")


def _fields(response: httpx2.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
