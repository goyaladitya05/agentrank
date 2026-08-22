"""The trusted boundary a buyer's tool calls pass through, and what it remembers about them.

An executor acts on the merchant only by calling `BuyerCommerceSurface`. That makes the surface
the one place where what actually happened is observable by somebody other than the executor, and
this module is what stands there: a decorator that answers exactly what the surface answered,
records what came back, and decides an origin when a call failed.

Trusted means it runs on the runner's side of whatever boundary the executor is behind. In
process it is a wrapper the operator command line builds, which is trusted because the executor
beside it is. Out of process the same job belongs to whatever supervises the worker, and the
worker can reach neither the ledger nor this module.

Three outcomes and the middle one is the load bearing distinction:

```text
ANSWERED   the merchant answered
REFUSED    the merchant said no, for a reason it named. An answer, not a fault
FAILED     the merchant surface did not answer, or the harness could not ask
```

Collapsing REFUSED into FAILED would report a commerce readiness finding every time a merchant
declined to quote for something it does not sell, which is most of what a benchmark measures. It
is the same line an HTTP transport draws between a 4xx that names a reason and a 5xx, which is
why an out of process buyer can apply it without any of this code.

`ExecutionWitness` is what the runner asks. It is deliberately narrow: whether a fault occurred
and whether a payment was ever attempted, both from evidence, and nothing about what was bought.
Nothing an executor produces reaches it, and there is no method on it an executor could call to
put something in.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from agentrank_api.benchmark.buyer import BuyerCommerceSurface
from agentrank_api.benchmark.faults import ExecutionFault, FaultOrigin
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
    AuthenticationError,
    ConflictError,
    NotFoundError,
    UpstreamError,
)
from agentrank_api.mandates.schemas import CreateMandateRequest, MandateView
from agentrank_api.payments.schemas import CreatePaymentRequest, PaymentView


class BuyerOperation(StrEnum):
    """The eight things a buyer may do, named so a fault can say which one it happened at.

    The same eight `BuyerCommerceSurface` declares, written out rather than read off a frame, so
    that a ninth operation has to be placed here before anything can be recorded about it.
    """

    SEARCH_PRODUCTS = "search_products"
    GET_PRODUCT = "get_product"
    AUTHORIZE_SPENDING = "authorize_spending"
    STATE_REQUIREMENTS = "state_requirements"
    CREATE_CHECKOUT = "create_checkout"
    GET_CHECKOUT = "get_checkout"
    PREPARE_CHECKOUT = "prepare_checkout"
    COMPLETE_CHECKOUT = "complete_checkout"


class ToolOutcome(StrEnum):
    """What one call came to."""

    ANSWERED = "ANSWERED"
    REFUSED = "REFUSED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One buyer operation and what came back, as trusted code saw it.

    No request body and no response body. This is evidence about the boundary rather than a
    trace of the mission, and a structured trace is Phase 2C's to design. What is here is what
    an origin can honestly be decided from.

    `origin` is set exactly when the outcome is FAILED, and it is decided where the failure was
    observed rather than reconstructed afterwards from a string.
    """

    operation: BuyerOperation
    outcome: ToolOutcome
    detail: str | None = None
    origin: FaultOrigin | None = None

    def __post_init__(self) -> None:
        if (self.outcome is ToolOutcome.FAILED) != (self.origin is not None):
            raise ValueError("a failed call names an origin and no other outcome carries one")


class ExecutionWitness(Protocol):
    """What the runner may ask about how one mission's execution actually went.

    Two questions and no more, both answered from evidence gathered on the trusted side.

    `fault` is the interruption to attribute, or None when nothing failed. `payment_attempted`
    is what stops a crash being tidied away: a mission that reached the payment call may have
    moved money, and that is never a mission to record and carry on from.
    """

    def begin(self) -> None:
        """Forget the previous mission. Called by the runner before an executor is handed one."""
        ...

    def fault(self) -> ExecutionFault | None: ...

    def payment_attempted(self) -> bool: ...


class ToolLedger:
    """Every buyer call one mission made, recorded where the executor cannot reach it.

    Held by the trusted side and handed to the runner as the witness. An executor is given the
    surface that writes to this and never the ledger itself, so there is no method it can call
    to add a call that did not happen or to remove one that did.

    The fault reported is the first FAILED call rather than the last. An executor stops at its
    first refusal, so the first failure is the one that ended the mission, and reporting a later
    one would name a call that only happened because the first was swallowed.
    """

    def __init__(self) -> None:
        self._calls: list[ToolCall] = []
        self._payment_attempted = False

    def begin(self) -> None:
        self._calls = []
        self._payment_attempted = False

    def record(self, call: ToolCall) -> None:
        self._calls.append(call)

    def note_payment_attempt(self) -> None:
        """Remember that the payment call was dispatched, before anything is known about it.

        Recorded on the way in rather than on the way out, because a payment call that never
        returned is exactly the one worth knowing about: it may have moved money, and a witness
        that only knew about calls which came back would have no record that one was made.
        """
        self._payment_attempted = True

    @property
    def calls(self) -> tuple[ToolCall, ...]:
        """Everything recorded for the current mission, in the order it happened."""
        return tuple(self._calls)

    def fault(self) -> ExecutionFault | None:
        for call in self._calls:
            if call.outcome is not ToolOutcome.FAILED:
                continue
            assert call.origin is not None  # the constructor requires it
            return ExecutionFault(
                origin=call.origin,
                detail=call.detail or f"{call.operation.value} did not answer",
                operation=call.operation.value,
            )
        return None

    def payment_attempted(self) -> bool:
        return self._payment_attempted


class MeasuredBuyerSurface:
    """A `BuyerCommerceSurface` that answers exactly what it wraps, and remembers what happened.

    Every method delegates and returns the delegate's answer unchanged, so nothing an executor
    can observe differs from talking to the surface directly. What differs is that a second
    party now knows whether the merchant answered, refused or failed, which is the fact the
    evaluator needs and the one the executor must not be the source of.

    A refusal is re-raised as itself rather than translated. `ReferenceMissionExecutor` reads
    `NotFoundError` and `ConflictError` to tell a variant the merchant does not sell from one it
    has run out of, and a boundary that replaced those with something of its own would be
    changing what the buyer can see in order to record what it saw.
    """

    def __init__(self, inner: BuyerCommerceSurface, ledger: ToolLedger) -> None:
        self._inner = inner
        self._ledger = ledger

    @property
    def merchant_id(self) -> uuid.UUID:
        return self._inner.merchant_id

    async def search_products(self, request: ProductSearchRequest) -> ProductSearchResponse:
        with self._watching(BuyerOperation.SEARCH_PRODUCTS):
            return await self._inner.search_products(request)

    async def get_product(self, product_id: uuid.UUID) -> ProductDetail:
        with self._watching(BuyerOperation.GET_PRODUCT):
            return await self._inner.get_product(product_id)

    async def authorize_spending(self, request: CreateMandateRequest) -> MandateView:
        with self._watching(BuyerOperation.AUTHORIZE_SPENDING):
            return await self._inner.authorize_spending(request)

    async def state_requirements(
        self, mandate_id: uuid.UUID, request: CreateIntentConstraintsRequest
    ) -> IntentConstraintSetView:
        with self._watching(BuyerOperation.STATE_REQUIREMENTS):
            return await self._inner.state_requirements(mandate_id, request)

    async def create_checkout(self, request: CreateCheckoutRequest) -> CheckoutView:
        with self._watching(BuyerOperation.CREATE_CHECKOUT):
            return await self._inner.create_checkout(request)

    async def get_checkout(self, checkout_id: uuid.UUID) -> CheckoutView:
        with self._watching(BuyerOperation.GET_CHECKOUT):
            return await self._inner.get_checkout(checkout_id)

    async def prepare_checkout(self, checkout_id: uuid.UUID) -> ExecutionPreparationView:
        with self._watching(BuyerOperation.PREPARE_CHECKOUT):
            return await self._inner.prepare_checkout(checkout_id)

    async def complete_checkout(
        self, checkout_id: uuid.UUID, request: CreatePaymentRequest
    ) -> PaymentView:
        with self._watching(BuyerOperation.COMPLETE_CHECKOUT):
            return await self._inner.complete_checkout(checkout_id, request)

    @contextmanager
    def _watching(self, operation: BuyerOperation) -> Iterator[None]:
        """Record how one call went, and let whatever happened through untouched."""
        if operation is BuyerOperation.COMPLETE_CHECKOUT:
            self._ledger.note_payment_attempt()
        try:
            yield
        except (NotFoundError, ConflictError) as refused:
            self._ledger.record(_refusal(operation, refused))
            raise
        except AuthenticationError:
            # Our credential, not their catalog. A benchmark that reported its own expired key as
            # a merchant API error would publish a commerce finding about somebody else's shop.
            self._ledger.record(
                ToolCall(
                    operation=operation,
                    outcome=ToolOutcome.FAILED,
                    detail="the harness was not authorized to make this call",
                    origin=FaultOrigin.HARNESS,
                )
            )
            raise
        except UpstreamError as upstream:
            self._ledger.record(
                ToolCall(
                    operation=operation,
                    outcome=ToolOutcome.FAILED,
                    detail=upstream.reason,
                    origin=FaultOrigin.MERCHANT,
                )
            )
            raise
        except Exception as failed:
            # Everything a merchant surface can do other than answer or refuse. In process that
            # is an exception nothing modelled; over HTTP it is a 5xx, a transport error or a
            # body nothing could read. All of them mean the same thing about the merchant.
            self._ledger.record(
                ToolCall(
                    operation=operation,
                    outcome=ToolOutcome.FAILED,
                    detail=type(failed).__name__,
                    origin=FaultOrigin.MERCHANT,
                )
            )
            raise
        else:
            self._ledger.record(ToolCall(operation=operation, outcome=ToolOutcome.ANSWERED))


def _refusal(operation: BuyerOperation, refused: NotFoundError | ConflictError) -> ToolCall:
    """One business answer, with the machine readable code the merchant gave for it."""
    detail = (
        refused.reason
        if isinstance(refused, ConflictError)
        else f"{refused.resource} was not found"
    )
    return ToolCall(operation=operation, outcome=ToolOutcome.REFUSED, detail=detail)
