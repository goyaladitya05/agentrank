"""A Razorpay that behaves like Razorpay and keeps a note of everything it was asked.

Two things make this worth having rather than a mock. The first is that it holds real state: an
order created under a receipt is remembered, so a second create under that receipt is refused
exactly as Razorpay refuses one, and the recovery path can find it. A stub that returned a fresh
order every time would let a duplicate order bug pass.

The second is the counters. Several of the properties Phase 1I has to prove are about calls that
must not happen:

```text
a cross merchant request              zero creates, zero fetches, zero payment reads
a second preparation for one attempt  one create in total, not two
a lost create response                one create, then a find, and no second create
```

None of those is visible in the database afterwards. "No Razorpay order exists for merchant B"
is also true if the call was made and failed, and the difference between refusing before an
external call and refusing after one is the whole security property. So the counters are the
assertion surface, and they count every call including the ones that raise.

Failures are configured rather than random. `fail_next_create` makes the next create behave like
a response that never arrived while still recording the order, which is the case the recovery
path exists for and the one a real gateway cannot be asked to produce on demand.
"""

import itertools
from dataclasses import dataclass, field

from agentrank_api.razorpay.entities import NewOrder, RazorpayOrder, RazorpayPayment
from agentrank_api.razorpay.errors import RazorpayRefusedError, RazorpayUnavailableError

# What a Razorpay order and payment identifier look like. Prefixed the way the real ones are, so
# a value produced here is recognizable in a test failure, and suffixed with a counter so two are
# never equal.
ORDER_PREFIX = "order_"
PAYMENT_PREFIX = "pay_"

# Razorpay's own error shape for a receipt that has already been used on the account.
DUPLICATE_RECEIPT_CODE = "BAD_REQUEST_ERROR"
DUPLICATE_RECEIPT_DESCRIPTION = (
    "An order with the same receipt value has already been created on this account"
)


@dataclass
class FakeRazorpayClient:
    """An in process Razorpay, deterministic and inspectable.

    Not frozen, because the recorded calls are the point. Nothing here reads a clock or a random
    number: identifiers come from a counter, so two runs of one test produce the same values.
    """

    orders: dict[str, RazorpayOrder] = field(default_factory=dict)
    payments: dict[str, RazorpayPayment] = field(default_factory=dict)

    # Every call, in order, whether it succeeded or raised.
    created_orders: list[NewOrder] = field(default_factory=list)
    receipt_lookups: list[str] = field(default_factory=list)
    fetched_orders: list[str] = field(default_factory=list)
    fetched_order_payments: list[str] = field(default_factory=list)
    fetched_payments: list[str] = field(default_factory=list)

    # Make the next create record the order and then behave like a response that was lost. That
    # is the state a crash between a gateway write and its reply leaves the world in, and it is
    # the only interesting failure at this boundary.
    fail_next_create: bool = False
    # Make every call fail ambiguously, for the tests that assert an unavailable gateway does
    # not become a payment outcome.
    unavailable: bool = False

    _sequence: itertools.count[int] = field(default_factory=lambda: itertools.count(1))

    def add_payment(
        self,
        *,
        order_id: str,
        status: str,
        amount_minor: int,
        currency: str = "INR",
        captured: bool | None = None,
        payment_id: str | None = None,
        method: str | None = "card",
        error_code: str | None = None,
    ) -> RazorpayPayment:
        """Record a payment the way a completed Standard Checkout would have produced one.

        `captured` defaults to agreeing with the status, because that is what Razorpay does with
        auto capture on. A test that wants the two to disagree says so explicitly, which is the
        point: an integration that silently preferred one of two disagreeing facts would be
        choosing without saying so.
        """
        identifier = payment_id or f"{PAYMENT_PREFIX}{next(self._sequence):016d}"
        payment = RazorpayPayment(
            id=identifier,
            order_id=order_id,
            amount_minor=amount_minor,
            currency=currency,
            status=status,
            captured=status == "captured" if captured is None else captured,
            method=method,
            error_code=error_code,
            error_description=None if error_code is None else f"simulated {error_code}",
        )
        self.payments[identifier] = payment
        return payment

    async def create_order(self, order: NewOrder) -> RazorpayOrder:
        """Create one order, unless this receipt already has one.

        The duplicate refusal is the real behaviour and it is what makes the recovery path
        testable: Razorpay treats a receipt as unique on the account, so the second create under
        one receipt is a 400 rather than a replay of the first order.
        """
        self.created_orders.append(order)
        if self.unavailable:
            raise RazorpayUnavailableError("the fake gateway is configured unavailable")

        existing = self._by_receipt(order.receipt)
        if existing is not None:
            raise RazorpayRefusedError(400, DUPLICATE_RECEIPT_CODE, DUPLICATE_RECEIPT_DESCRIPTION)

        created = RazorpayOrder(
            id=f"{ORDER_PREFIX}{next(self._sequence):016d}",
            amount_minor=order.amount_minor,
            amount_paid_minor=0,
            currency=order.currency,
            receipt=order.receipt,
            status="created",
            attempts=0,
        )
        self.orders[created.id] = created

        if self.fail_next_create:
            # The order exists at the gateway and the caller is told nothing. Cleared here so a
            # test arms it once rather than forever.
            self.fail_next_create = False
            raise RazorpayUnavailableError("the create response was lost")
        return created

    async def find_order_by_receipt(self, receipt: str) -> RazorpayOrder | None:
        self.receipt_lookups.append(receipt)
        if self.unavailable:
            raise RazorpayUnavailableError("the fake gateway is configured unavailable")
        return self._by_receipt(receipt)

    async def fetch_order(self, order_id: str) -> RazorpayOrder | None:
        self.fetched_orders.append(order_id)
        if self.unavailable:
            raise RazorpayUnavailableError("the fake gateway is configured unavailable")
        return self.orders.get(order_id)

    async def fetch_order_payments(self, order_id: str) -> tuple[RazorpayPayment, ...]:
        self.fetched_order_payments.append(order_id)
        if self.unavailable:
            raise RazorpayUnavailableError("the fake gateway is configured unavailable")
        return tuple(payment for payment in self.payments.values() if payment.order_id == order_id)

    async def fetch_payment(self, payment_id: str) -> RazorpayPayment | None:
        self.fetched_payments.append(payment_id)
        if self.unavailable:
            raise RazorpayUnavailableError("the fake gateway is configured unavailable")
        return self.payments.get(payment_id)

    @property
    def calls(self) -> int:
        """Every call this gateway has received, of any kind.

        One number, because the assertion a cross merchant test wants to make is that nothing at
        all was asked, and enumerating five lists at every call site invites forgetting one.
        """
        return (
            len(self.created_orders)
            + len(self.receipt_lookups)
            + len(self.fetched_orders)
            + len(self.fetched_order_payments)
            + len(self.fetched_payments)
        )

    def _by_receipt(self, receipt: str) -> RazorpayOrder | None:
        return next((order for order in self.orders.values() if order.receipt == receipt), None)
