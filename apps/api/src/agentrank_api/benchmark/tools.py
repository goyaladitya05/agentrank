"""The trusted boundary a buyer's tool calls pass through, and what it remembers about them.

An executor acts on the merchant only by calling `BuyerCommerceSurface`. That makes the surface
the one place where what actually happened is observable by somebody other than the executor, and
this module is what stands there: a decorator that answers exactly what the surface answered,
records what came back, and decides an origin when a call failed.

Trusted means it runs on the runner's side of whatever boundary the executor is behind, and in
process that is a statement about who wrote the executor rather than about what it could reach.
The surface an executor holds refers to the ledger, so an in process one can clear it with one
attribute access; no arrangement of private names in Python changes that. What this does
guarantee, on both paths, is that the executor does not decide the origin.

The boundary that does not rest on that is a process. An out of process executor is watched by
the server side record in `agentrank_api.benchmark.endpoint`, which nothing it can reach refers
to, and it has no route to this module at all.

Three outcomes and the middle one is the load bearing distinction:

```text
ANSWERED   the merchant answered
REFUSED    the merchant said no, for a reason it named. An answer, not a fault
FAILED     the merchant surface did not answer, or the harness could not ask
```

Collapsing REFUSED into FAILED would report a commerce readiness finding every time a merchant
declined to quote for something it does not sell, which is most of what a benchmark measures.
Collapsing the other way is the mistake that was made first: a 404 for a mandate this execution
created moments ago is not an answer about a merchant, and recording it as one published a
reasoning failure against a buyer whose own harness had broken. Which refusals are the merchant's
own catalog answering is written down as data in `agentrank_api.benchmark.faults`, read off the
merchant's machine readable codes, and identical over HTTP where the same codes arrive in the
error body.

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
from agentrank_api.benchmark.faults import (
    AUTHORIZATION_REFUSALS,
    CATALOG_REFUSALS,
    CATALOG_RESOURCES,
    ExecutionFault,
    FaultOrigin,
)
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

    Held by the trusted side and handed to the runner as the witness. In process that is a
    convention rather than a boundary, and the difference is worth stating plainly: the surface
    the executor holds refers to this, so an in process executor can reach it with one attribute
    access and clear it. An independent review found exactly that, and Python offers no
    arrangement of private names that would change it.

    What makes it a boundary is where the executor is. An out of process one is watched by the
    server side record in `agentrank_api.benchmark.endpoint`, which nothing it can reach refers
    to at all. This class is for the trusted in process path, and what it guarantees is that the
    executor does not decide the origin, not that it could not tamper with the record. See
    docs/shortcomings.md.

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
        _given(BuyerOperation.SEARCH_PRODUCTS, request, ProductSearchRequest)
        with self._watching(BuyerOperation.SEARCH_PRODUCTS):
            return await self._inner.search_products(request)

    async def get_product(self, product_id: uuid.UUID) -> ProductDetail:
        _given(BuyerOperation.GET_PRODUCT, product_id, uuid.UUID)
        with self._watching(BuyerOperation.GET_PRODUCT):
            return await self._inner.get_product(product_id)

    async def authorize_spending(self, request: CreateMandateRequest) -> MandateView:
        _given(BuyerOperation.AUTHORIZE_SPENDING, request, CreateMandateRequest)
        with self._watching(BuyerOperation.AUTHORIZE_SPENDING):
            return await self._inner.authorize_spending(request)

    async def state_requirements(
        self, mandate_id: uuid.UUID, request: CreateIntentConstraintsRequest
    ) -> IntentConstraintSetView:
        _given(BuyerOperation.STATE_REQUIREMENTS, mandate_id, uuid.UUID)
        _given(BuyerOperation.STATE_REQUIREMENTS, request, CreateIntentConstraintsRequest)
        with self._watching(BuyerOperation.STATE_REQUIREMENTS):
            return await self._inner.state_requirements(mandate_id, request)

    async def create_checkout(self, request: CreateCheckoutRequest) -> CheckoutView:
        _given(BuyerOperation.CREATE_CHECKOUT, request, CreateCheckoutRequest)
        with self._watching(BuyerOperation.CREATE_CHECKOUT):
            return await self._inner.create_checkout(request)

    async def get_checkout(self, checkout_id: uuid.UUID) -> CheckoutView:
        _given(BuyerOperation.GET_CHECKOUT, checkout_id, uuid.UUID)
        with self._watching(BuyerOperation.GET_CHECKOUT):
            return await self._inner.get_checkout(checkout_id)

    async def prepare_checkout(self, checkout_id: uuid.UUID) -> ExecutionPreparationView:
        _given(BuyerOperation.PREPARE_CHECKOUT, checkout_id, uuid.UUID)
        with self._watching(BuyerOperation.PREPARE_CHECKOUT):
            return await self._inner.prepare_checkout(checkout_id)

    async def complete_checkout(
        self, checkout_id: uuid.UUID, request: CreatePaymentRequest
    ) -> PaymentView:
        _given(BuyerOperation.COMPLETE_CHECKOUT, checkout_id, uuid.UUID)
        _given(BuyerOperation.COMPLETE_CHECKOUT, request, CreatePaymentRequest)
        with self._watching(BuyerOperation.COMPLETE_CHECKOUT):
            paid = await self._inner.complete_checkout(checkout_id, request)
        self._ledger.record(_admission(paid))
        return paid

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


class BuyerArgumentError(TypeError):
    """A caller handed this boundary something that is not the argument the operation takes.

    Its own class, and raised outside the watched scope, which is the point rather than a
    nicety. The surface's own code runs the argument: `MerchantBuyerSurface.search_products`
    calls `request.to_criteria(...)` on whatever it is given. An executor passing an object
    whose method raises could therefore choose which exception was observed inside the boundary,
    and with it which origin was attributed, which is the whole thing this benchmark had just
    moved out of its reach. Found by an independent review.

    So the type is checked before anything is watched. What comes back is neither an answer nor
    a fault: it is a caller that cannot call, and it propagates.
    """

    def __init__(self, operation: BuyerOperation, expected: type, given: object) -> None:
        super().__init__(
            f"{operation.value} takes {expected.__name__} and was given {type(given).__name__}"
        )
        self.operation = operation


def _given(operation: BuyerOperation, value: object, expected: type) -> None:
    """Refuse an argument that is not exactly what the operation takes, before anything is
    recorded.

    The type itself and not an `isinstance` check, and that correction is the point. A subclass
    of a Pydantic model has been through the model's own validation and can still override the
    method the surface calls: `MerchantBuyerSurface.search_products` calls `request.to_criteria`,
    so a two line subclass chose which exception was observed inside the boundary and with it
    which origin was attributed. An independent test audit found the first version of this guard
    open to exactly that.

    The cost is that a legitimate subclass would be refused. None exists, the view models are
    request bodies rather than a hierarchy, and refusing a caller that cannot call is the fail
    closed direction.
    """
    if type(value) is not expected:
        raise BuyerArgumentError(operation, expected, value)


def _admission(paid: PaymentView) -> ToolCall:
    """What a payment answer means when no attempt was admitted.

    A payment the authorization gates denied is the safety layer working and is a finding: it is
    recorded as an answer and the evaluator marks it `MANDATE_DENIED` from the authorization the
    executor reports. Every other admission refusal is about a mandate, a quote or a hold this
    execution created moments ago, so it says the caller's own state is wrong.

    Read off the merchant's own answer by trusted code rather than out of the executor's report,
    which is what makes it attribution rather than a self report. Before this existed, an
    expired reservation on a slow machine was published as a buyer reasoning failure and as lost
    demand against the merchant.
    """
    if paid.admitted or paid.refusal is None:
        return ToolCall(operation=BuyerOperation.COMPLETE_CHECKOUT, outcome=ToolOutcome.ANSWERED)
    if paid.refusal.value in AUTHORIZATION_REFUSALS:
        return ToolCall(
            operation=BuyerOperation.COMPLETE_CHECKOUT,
            outcome=ToolOutcome.REFUSED,
            detail=paid.refusal.value,
        )
    return ToolCall(
        operation=BuyerOperation.COMPLETE_CHECKOUT,
        outcome=ToolOutcome.FAILED,
        detail=paid.refusal.value,
        origin=FaultOrigin.HARNESS,
    )


def _refusal(operation: BuyerOperation, refused: NotFoundError | ConflictError) -> ToolCall:
    """A refusal, as either the merchant's answer or this caller's own state being wrong.

    Both arrive as a 404 or a 409, so the status cannot separate them and the merchant's own
    machine readable code is what does. A variant the merchant does not sell is an answer and is
    measured as one. A mandate this execution created moments ago having vanished is not an
    answer about anything, and recording it as one publishes a reasoning failure against a buyer
    whose harness broke, which is what happened before this split existed.

    Anything unclassified is the caller's own state. That is the fail closed direction: a refusal
    nobody has placed in `CATALOG_REFUSALS` is not evidence about a merchant.
    """
    if isinstance(refused, ConflictError):
        answered = refused.reason in CATALOG_REFUSALS
        detail = refused.reason
    else:
        answered = refused.resource in CATALOG_RESOURCES
        detail = f"{refused.resource} was not found"

    if answered:
        return ToolCall(operation=operation, outcome=ToolOutcome.REFUSED, detail=detail)
    return ToolCall(
        operation=operation,
        outcome=ToolOutcome.FAILED,
        detail=detail,
        origin=FaultOrigin.HARNESS,
    )
