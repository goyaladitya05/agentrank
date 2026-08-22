"""The fake gateway is a test instrument, so its contract is pinned.

Everything from Feature 3 onwards asserts things like "exactly one order was created" and
"Razorpay was never called". Those assertions are worth exactly as much as this object, and two
of its behaviours are load bearing rather than convenient.

The first is that a receipt is unique on the account, so a second create under one receipt is
refused the way Razorpay refuses one. A fake that returned a fresh order every time would let a
duplicate order bug pass every test in the phase.

The second is `fail_next_create`, which records the order and then behaves like a response that
never arrived. That is the exact state a lost reply leaves the world in, it is the case the
deterministic receipt exists for, and a real gateway cannot be asked to produce it on demand.
"""

import pytest

from agentrank_api.razorpay.entities import NewOrder
from agentrank_api.razorpay.errors import RazorpayRefusedError, RazorpayUnavailableError
from agentrank_api.razorpay.fake import FakeRazorpayClient

pytestmark = pytest.mark.anyio

RECEIPT = "ar_abcdefghijklmnopqrstuvwxyz234567"
AMOUNT = 499900


def an_order(receipt: str = RECEIPT, *, amount_minor: int = AMOUNT) -> NewOrder:
    return NewOrder(amount_minor=amount_minor, currency="INR", receipt=receipt, notes={})


async def test_the_fake_satisfies_the_client_protocol() -> None:
    """Structural typing is only useful if something checks the structure."""
    from agentrank_api.razorpay.client import RazorpayClient

    client: RazorpayClient = FakeRazorpayClient()

    assert client is not None


async def test_an_order_is_created_with_what_it_was_asked_for() -> None:
    client = FakeRazorpayClient()

    order = await client.create_order(an_order())

    assert order.amount_minor == AMOUNT
    assert order.currency == "INR"
    assert order.receipt == RECEIPT
    assert order.status == "created"
    assert client.orders[order.id] == order


async def test_a_second_create_under_one_receipt_is_refused() -> None:
    """Razorpay treats a receipt as unique on the account, and so does this."""
    client = FakeRazorpayClient()
    await client.create_order(an_order())

    with pytest.raises(RazorpayRefusedError) as refused:
        await client.create_order(an_order())

    assert refused.value.status_code == 400
    assert len(client.orders) == 1
    # Both calls are recorded, including the refused one. The difference between "refused before
    # calling" and "refused after calling" is the whole point of the counters.
    assert len(client.created_orders) == 2


async def test_a_lost_create_response_still_leaves_the_order_behind() -> None:
    """The case the deterministic receipt exists for."""
    client = FakeRazorpayClient(fail_next_create=True)

    with pytest.raises(RazorpayUnavailableError):
        await client.create_order(an_order())

    recovered = await client.find_order_by_receipt(RECEIPT)
    assert recovered is not None
    assert recovered.receipt == RECEIPT
    # Armed once rather than forever, so the recovery path in a later test is not fighting it.
    assert client.fail_next_create is False


async def test_every_call_is_counted_including_the_ones_that_fail() -> None:
    client = FakeRazorpayClient(unavailable=True)

    for attempt in (
        lambda: client.create_order(an_order()),
        lambda: client.find_order_by_receipt(RECEIPT),
        lambda: client.fetch_order("order_1"),
        lambda: client.fetch_order_payments("order_1"),
        lambda: client.fetch_payment("pay_1"),
    ):
        with pytest.raises(RazorpayUnavailableError):
            await attempt()

    assert client.calls == 5


async def test_payments_are_listed_against_their_own_order() -> None:
    client = FakeRazorpayClient()
    mine = await client.create_order(an_order())
    theirs = await client.create_order(an_order("ar_someone_else"))
    client.add_payment(order_id=mine.id, status="captured", amount_minor=AMOUNT)
    client.add_payment(order_id=theirs.id, status="captured", amount_minor=AMOUNT)

    found = await client.fetch_order_payments(mine.id)

    assert [payment.order_id for payment in found] == [mine.id]
    assert found[0].captured is True


async def test_captured_defaults_to_agreeing_with_the_status_and_can_be_forced_apart() -> None:
    """Auto capture is Razorpay's default, and a disagreement has to be stated to happen."""
    client = FakeRazorpayClient()

    agreeing = client.add_payment(order_id="order_1", status="authorized", amount_minor=AMOUNT)
    forced = client.add_payment(
        order_id="order_1", status="authorized", amount_minor=AMOUNT, captured=True
    )

    assert agreeing.captured is False
    assert forced.captured is True
