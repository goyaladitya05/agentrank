"""The commerce surface a benchmark executor shops through, and nothing wider.

An executor must not reach the database. If it could, "the merchant's data existed and the agent
could not act on it" would stop being a measurement: the harness would be reading the answer out
of the rows the agent was supposed to have to discover. So an executor is handed this and is
typed against the protocol below, which has no session, no repository and no ORM row on it.

That is a narrower guarantee than it first reads, and the difference is worth stating rather than
glossing. `MerchantBuyerSurface` holds a session factory, so anything holding one can open a
session with one call. Python offers no way to prevent that, and no arrangement of private
attributes makes it a boundary. What is actually enforced here is that an executor's module
imports nothing that could open a session and spells no oracle name, both checked against its
source, and that reaching one would take a deliberate act visible in review. The boundary that
does not rest on review is a separate process with no database credential, which is what an
untrusted executor gets, and this in process surface is deliberately not it.

What this is, precisely, is the application service layer with a merchant already bound to it,
returning the same view models the HTTP routes serialize. Every method here is one route's body:
the same service, the same command, the same merchant scoping, the same refusals, and now the
same session ownership. That is the whole reason it is not a second API. A buyer agent that
eventually drives the real endpoints will be exercising these exact operations, and a benchmark
that measured a private shortcut instead would be measuring something no buyer can use.

One session per operation, opened here and closed here, exactly as `get_session` does per HTTP
request. It used to be the runner's own session, handed in, which made a mission's commerce work
part of the run's transaction sequence and had three costs worth naming. An executor that raised
after leaving that transaction in an aborted state broke the operator's next call on it, so a run
that stopped that way had to be closed from a fresh process. A surface holding one session across
a whole run reads its own stale copies of rows that world preparation has since rewritten,
because a committed session does not expire what it has already loaded. And a benchmark whose in
process transport batched several buyer operations into one transaction was not measuring what an
HTTP buyer would experience, which is the thing it exists to predict.

It is not HTTP, and that is a statement about this class rather than about the benchmark.
`BuyerCommerceSurface` is a protocol, and the implementation an untrusted executor is given does
go over the wire, from a process with no database credential at all. This one exists because a
trusted in process path is the fast deterministic way to exercise the run machinery, and because
the two now have the same transaction shape, a result produced through either is produced under
the same rules.

The vocabulary is deliberately small and every method has a caller:

```text
search_products      browse this merchant's catalog
get_product          read one product with every variant it has
authorize_spending   create the SpendingMandate this purchase will be made under
state_requirements   qualify that mandate with what the purchase must satisfy
create_checkout      ask the merchant to quote a selection
get_checkout         read a quote back as the merchant now records it
prepare_checkout     require both authorization gates and hold the stock
complete_checkout    admit and dispatch the payment
```

There is no cancel, no revoke, no reconcile and no release. A benchmark mission that stops does
not tidy up after itself: what it left behind is evidence, and the next mission's world is
restored by `BenchmarkEnvironmentService` rather than by an executor being well behaved.

Merchant scope is a constructor argument and never a method argument. There is no method here a
caller could point at somebody else's shop, which is the same property the authenticated HTTP
surface has and it is achieved the same way: the merchant comes from the caller's identity rather
than from the request.
"""

import uuid
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentrank_api.checkout.execution import CheckoutExecutionService
from agentrank_api.checkout.schemas import (
    CheckoutView,
    CreateCheckoutRequest,
    ExecutionPreparationView,
)
from agentrank_api.checkout.service import CheckoutService
from agentrank_api.commerce.schemas import (
    ProductDetail,
    ProductSearchRequest,
    ProductSearchResponse,
)
from agentrank_api.commerce.service import CatalogService
from agentrank_api.constraints.schemas import (
    CreateIntentConstraintsRequest,
    IntentConstraintSetView,
)
from agentrank_api.constraints.service import IntentConstraintService
from agentrank_api.mandates.schemas import CreateMandateRequest, MandateView
from agentrank_api.mandates.service import MandateService
from agentrank_api.payments.provider import PaymentProvider
from agentrank_api.payments.schemas import CreatePaymentRequest, PaymentView
from agentrank_api.payments.service import PaymentService


class BuyerCommerceSurface(Protocol):
    """Everything a buyer may do at one merchant, and nothing else.

    A protocol rather than a base class, so an executor depends on the vocabulary rather than on
    the implementation, and an HTTP backed version can replace the in process one without any
    executor changing.
    """

    @property
    def merchant_id(self) -> uuid.UUID: ...

    async def search_products(self, request: ProductSearchRequest) -> ProductSearchResponse: ...

    async def get_product(self, product_id: uuid.UUID) -> ProductDetail: ...

    async def authorize_spending(self, request: CreateMandateRequest) -> MandateView: ...

    async def state_requirements(
        self, mandate_id: uuid.UUID, request: CreateIntentConstraintsRequest
    ) -> IntentConstraintSetView: ...

    async def create_checkout(self, request: CreateCheckoutRequest) -> CheckoutView: ...

    async def get_checkout(self, checkout_id: uuid.UUID) -> CheckoutView: ...

    async def prepare_checkout(self, checkout_id: uuid.UUID) -> ExecutionPreparationView: ...

    async def complete_checkout(
        self, checkout_id: uuid.UUID, request: CreatePaymentRequest
    ) -> PaymentView: ...


class MerchantBuyerSurface:
    """The trusted in process implementation, over the same services the routes call.

    Every method is a route body with the merchant already supplied. Nothing is reimplemented,
    nothing is relaxed, and no rule is decided here: the services own the transactions, the
    locks, the authorization gates and the refusals, exactly as they do over HTTP.

    Constructed with a session factory rather than a session, and every method opens one and
    closes it. That is what a route does, and doing anything else here would make an in process
    benchmark measure transaction boundaries no buyer over the wire could ever get. It also
    means this surface no longer shares the runner's session: the runner's transactions and the
    buyer's are separate by construction, so an executor cannot leave the runner unable to
    record what it just did.

    `credential_id` is not passed, and the omission is honest rather than an oversight. A
    credential proves which merchant integration made an HTTP request. There is no request and
    no credential here, so the audit trail records the role and no evidence this process does not
    have. That is the same thing the operator command line does.
    """

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        merchant_id: uuid.UUID,
        provider: PaymentProvider,
    ) -> None:
        if isinstance(sessions, AsyncSession):  # type: ignore[unreachable]
            # The arrangement this class was changed to prevent, refused where it is made rather
            # than three calls later with an opaque "not callable". Handing a surface the
            # runner's own session put a mission's commerce inside the run's transaction
            # sequence, which is what let a broken executor stop the run being recorded.
            raise TypeError(
                "a buyer surface opens its own session per operation and takes a session"
                " factory, not a session"
            )
        self._sessions = sessions
        self._merchant_id = merchant_id
        self._provider = provider

    @property
    def merchant_id(self) -> uuid.UUID:
        return self._merchant_id

    async def search_products(self, request: ProductSearchRequest) -> ProductSearchResponse:
        """Browse this merchant's catalog, with the filters the request states.

        Inactive products and inactive variants are excluded unless the request asks for them,
        which is the ordinary buyer view: a buyer cannot buy what the merchant has withdrawn.
        """
        async with self._sessions() as session:
            matches = await CatalogService(session).search_products(
                request.to_criteria(self._merchant_id)
            )
            return ProductSearchResponse.from_matches(matches, limit=request.limit)

    async def get_product(self, product_id: uuid.UUID) -> ProductDetail:
        """Read one product with every variant it has, active or not.

        Wider than a search hit on purpose, and it is the read a buyer makes when they open a
        product rather than a listing. A variant the merchant has withdrawn is visible here and
        is still not purchasable, which is a distinction an executor has to be able to make.
        """
        async with self._sessions() as session:
            product = await CatalogService(session).get_product(
                product_id, merchant_id=self._merchant_id
            )
            return ProductDetail.from_model(product)

    async def authorize_spending(self, request: CreateMandateRequest) -> MandateView:
        """Create the single purchase authorization this buyer will spend under."""
        async with self._sessions() as session:
            mandate = await MandateService(session).create_mandate(
                request.to_command(self._merchant_id)
            )
            return MandateView.from_model(mandate)

    async def state_requirements(
        self, mandate_id: uuid.UUID, request: CreateIntentConstraintsRequest
    ) -> IntentConstraintSetView:
        """Qualify a mandate with what the purchase made under it must satisfy.

        Separate from the mandate for the reason the schema keeps them separate: money is
        authorized by the mandate and what may be bought is authorized by these, and a ceiling
        with two homes is a ceiling that can disagree with itself.
        """
        async with self._sessions() as session:
            constraint_set = await IntentConstraintService(session).create_constraints(
                request.to_command(mandate_id, self._merchant_id)
            )
            return IntentConstraintSetView.from_model(constraint_set)

    async def create_checkout(self, request: CreateCheckoutRequest) -> CheckoutView:
        """Ask the merchant to quote a selection against a mandate.

        Prices come from the catalog and never from the request, so what a buyer thinks
        something costs has no bearing on what it costs. Creating a quote is not authorizing
        one: a total above what the mandate permits is quoted successfully and denied later.
        """
        async with self._sessions() as session:
            checkout = await CheckoutService(session).create_checkout(
                request.to_command(self._merchant_id)
            )
            return CheckoutView.from_model(checkout)

    async def get_checkout(self, checkout_id: uuid.UUID) -> CheckoutView:
        """Read one of this merchant's quotes back as the merchant now records it."""
        async with self._sessions() as session:
            checkout = await CheckoutService(session).get_checkout(
                checkout_id, merchant_id=self._merchant_id
            )
            return CheckoutView.from_model(checkout)

    async def prepare_checkout(self, checkout_id: uuid.UUID) -> ExecutionPreparationView:
        """Require both authorization gates and hold the stock, or say exactly why not.

        A denial and an empty shelf are both ordinary answers with `ready: false` and the
        reasons in the body, because they call for opposite next moves and an executor that
        cannot tell them apart is an executor that retries the same request.
        """
        async with self._sessions() as session:
            readiness = await CheckoutExecutionService(session).prepare_execution(
                checkout_id, merchant_id=self._merchant_id
            )
            return ExecutionPreparationView.from_readiness(readiness)

    async def complete_checkout(
        self, checkout_id: uuid.UUID, request: CreatePaymentRequest
    ) -> PaymentView:
        """Pay for this quote, or say exactly why it may not be paid for.

        The whole payment kernel, unchanged: both gates and an effective hold required under
        locks, one attempt written before any provider is called, and the provider reached only
        through that attempt. A refusal reaches no provider at all.
        """
        async with self._sessions() as session:
            result = await PaymentService(session, self._provider).pay(
                checkout_id, merchant_id=self._merchant_id, idempotency_key=request.resolve_key()
            )
            return PaymentView.from_admission(result.admission, result.attempt)
