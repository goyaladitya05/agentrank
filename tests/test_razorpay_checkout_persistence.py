"""What the database refuses about a Razorpay binding, and how a lost order is recovered.

Two subjects, one file, because the second depends on the first being structural.

The schema tests assert refusals rather than acceptances. Anybody can write a row that works;
what is worth a test is that a second order for one attempt, a binding claiming another
merchant's payment, an amount that differs from what was admitted, a rebinding to a different
Razorpay order and a change to a confirmed checkout are all impossible at the database rather
than merely absent from the service. Every one of them is asserted by trying it and watching
PostgreSQL refuse, because a constraint that is never exercised is a constraint that might have
been dropped in a migration nobody read.

The recovery tests assert the property the whole receipt derivation exists for: a create whose
response never arrived must resolve to the order that already exists rather than to a second
one.
"""

import uuid
from collections.abc import Callable
from dataclasses import replace

import pytest
from commerce_support import admit, build_shop, quote
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.payments.references import provider_operation_reference
from agentrank_api.razorpay.entities import NewOrder, RazorpayOrder
from agentrank_api.razorpay.errors import (
    RazorpayOrderMismatchError,
    RazorpayRefusedError,
    RazorpayUnavailableError,
)
from agentrank_api.razorpay.fake import FakeRazorpayClient
from agentrank_api.razorpay.models import RazorpayCheckout, RazorpayCheckoutStatus
from agentrank_api.razorpay.orders import obtain_order, require_matching_order
from agentrank_api.razorpay.repository import RazorpayCheckoutRepository

pytestmark = pytest.mark.anyio

KEY = "razorpay-0001"
OTHER_KEY = "razorpay-0002"


async def bound(session: AsyncSession, slug: str = "ampere-supply") -> RazorpayCheckout:
    """A shop, a quote, an admitted payment and a binding reserved against it."""
    shop = await build_shop(session, slug)
    attempt = await admit(session, shop, await quote(session, shop), key=KEY)
    binding = await RazorpayCheckoutRepository(session).create(
        merchant_id=attempt.merchant_id,
        payment_attempt_id=attempt.id,
        provider_receipt=provider_operation_reference(attempt.merchant_id, attempt.id),
        amount_minor=attempt.amount_minor,
        currency=attempt.currency,
    )
    await session.commit()
    return binding


async def test_a_binding_starts_preparing_with_no_order(session: AsyncSession) -> None:
    """The row is written before Razorpay is called, and it says so.

    PREPARING with no order identifier is the honest description of that instant, and writing it
    first is the whole reason a lost create response is recoverable.
    """
    binding = await bound(session)

    assert binding.status is RazorpayCheckoutStatus.PREPARING
    assert binding.provider_order_id is None
    assert binding.order_created_at is None
    assert binding.confirmed_at is None
    assert binding.provider_receipt.startswith("ar_")


async def test_one_attempt_cannot_have_two_bindings(session: AsyncSession) -> None:
    """One AgentRank payment maps to at most one logical Razorpay Order.

    Structural rather than a check in a service, because two requests preparing one checkout at
    the same instant would both pass a check and only one may create an order.
    """
    binding = await bound(session)
    repository = RazorpayCheckoutRepository(session)

    with pytest.raises(IntegrityError):
        await repository.create(
            merchant_id=binding.merchant_id,
            payment_attempt_id=binding.payment_attempt_id,
            provider_receipt="ar_a_completely_different_receipt",
            amount_minor=binding.amount_minor,
            currency=binding.currency,
        )
    await session.rollback()


async def test_a_receipt_belongs_to_one_binding(session: AsyncSession) -> None:
    """The local half of the guarantee Razorpay makes on the account."""
    first = await bound(session, "ampere-supply")
    shop = await build_shop(session, "volta-goods")
    other = await admit(session, shop, await quote(session, shop), key=OTHER_KEY)

    with pytest.raises(IntegrityError):
        await RazorpayCheckoutRepository(session).create(
            merchant_id=other.merchant_id,
            payment_attempt_id=other.id,
            provider_receipt=first.provider_receipt,
            amount_minor=other.amount_minor,
            currency=other.currency,
        )
    await session.rollback()


async def test_a_binding_cannot_claim_a_merchant_the_attempt_does_not_have(
    session: AsyncSession,
) -> None:
    """Merchant ownership is the composite foreign key, not a column somebody remembered."""
    shop = await build_shop(session, "ampere-supply")
    attempt = await admit(session, shop, await quote(session, shop), key=KEY)
    await session.commit()

    with pytest.raises(IntegrityError):
        await RazorpayCheckoutRepository(session).create(
            merchant_id=uuid.uuid7(),
            payment_attempt_id=attempt.id,
            provider_receipt=provider_operation_reference(attempt.merchant_id, attempt.id),
            amount_minor=attempt.amount_minor,
            currency=attempt.currency,
        )
    await session.rollback()


@pytest.mark.parametrize("field", ["amount_minor", "currency"])
async def test_a_binding_cannot_carry_money_the_attempt_does_not(
    session: AsyncSession, field: str
) -> None:
    """The property the whole integration rests on, enforced by the schema.

    A provider order amount that could differ from the admitted amount is an order that could
    charge a customer something the mandate never authorized. Making it a foreign key rather
    than a copy means no service can get it wrong, including one written later by somebody who
    has not read this file.
    """
    shop = await build_shop(session, "ampere-supply")
    attempt = await admit(session, shop, await quote(session, shop), key=KEY)
    await session.commit()
    money: dict[str, object] = {
        "amount_minor": attempt.amount_minor,
        "currency": attempt.currency,
    }
    money[field] = 1 if field == "amount_minor" else "USD"

    with pytest.raises(IntegrityError):
        await RazorpayCheckoutRepository(session).create(
            merchant_id=attempt.merchant_id,
            payment_attempt_id=attempt.id,
            provider_receipt=provider_operation_reference(attempt.merchant_id, attempt.id),
            **money,  # type: ignore[arg-type]
        )
    await session.rollback()


async def test_binding_an_order_moves_the_checkout_to_awaiting_payment(
    session: AsyncSession,
) -> None:
    binding = await bound(session)
    repository = RazorpayCheckoutRepository(session)

    assert await repository.bind_order(binding, provider_order_id="order_ABC123") is True
    await session.commit()

    assert binding.status is RazorpayCheckoutStatus.AWAITING_PAYMENT
    assert binding.provider_order_id == "order_ABC123"
    assert binding.order_created_at is not None
    # Reported unchanged the second time, so a repeated preparation can say it created nothing.
    assert await repository.bind_order(binding, provider_order_id="order_ABC123") is False


async def test_a_binding_cannot_be_moved_to_another_order(session: AsyncSession) -> None:
    """The most expensive single update available in this table, refused twice over.

    Rebinding would point verification at an order the customer never paid, so a later callback
    for the real order would fail its signature check and a callback for the substituted one
    would pass. The repository refuses it and the guard trigger refuses it, and both exist
    because either alone could be bypassed.
    """
    binding = await bound(session)
    repository = RazorpayCheckoutRepository(session)
    await repository.bind_order(binding, provider_order_id="order_ABC123")
    await session.commit()

    with pytest.raises(ValueError, match="cannot be rebound"):
        await repository.bind_order(binding, provider_order_id="order_SOMEONE_ELSE")

    binding.provider_order_id = "order_SOMEONE_ELSE"
    with pytest.raises(DBAPIError, match="cannot be rebound"):
        await session.flush()
    await session.rollback()


async def test_one_provider_order_cannot_be_bound_to_two_attempts(
    session: AsyncSession,
) -> None:
    first = await bound(session, "ampere-supply")
    shop = await build_shop(session, "volta-goods")
    other = await admit(session, shop, await quote(session, shop), key=OTHER_KEY)
    repository = RazorpayCheckoutRepository(session)
    second = await repository.create(
        merchant_id=other.merchant_id,
        payment_attempt_id=other.id,
        provider_receipt=provider_operation_reference(other.merchant_id, other.id),
        amount_minor=other.amount_minor,
        currency=other.currency,
    )
    await repository.bind_order(first, provider_order_id="order_ABC123")
    await session.commit()

    # Refused at the flush inside the write, which is as early as PostgreSQL can see it.
    with pytest.raises(IntegrityError):
        await repository.bind_order(second, provider_order_id="order_ABC123")
    await session.rollback()


async def test_a_confirmed_checkout_cannot_be_changed(session: AsyncSession) -> None:
    """Terminal means no update succeeds, not merely that the status may not move."""
    binding = await bound(session)
    repository = RazorpayCheckoutRepository(session)
    await repository.bind_order(binding, provider_order_id="order_ABC123")
    assert await repository.mark_confirmed(binding, provider_payment_id="pay_XYZ") is True
    await session.commit()

    assert binding.status is RazorpayCheckoutStatus.CONFIRMED
    assert binding.confirmed_at is not None
    # Idempotent at the repository, so a repeated callback writes nothing.
    assert await repository.mark_confirmed(binding, provider_payment_id="pay_OTHER") is False

    binding.provider_payment_id = "pay_OTHER"
    with pytest.raises(DBAPIError, match="confirmed razorpay checkout cannot be changed"):
        await session.flush()
    await session.rollback()


async def test_a_merchant_cannot_read_another_merchants_binding(session: AsyncSession) -> None:
    """Absent rather than refused, so holding the identifier is worth nothing."""
    binding = await bound(session)
    repository = RazorpayCheckoutRepository(session)

    mine = await repository.get_for_attempt(
        binding.payment_attempt_id, merchant_id=binding.merchant_id
    )
    theirs = await repository.get_for_attempt(binding.payment_attempt_id, merchant_id=uuid.uuid7())

    assert mine is not None
    assert theirs is None


def an_order(receipt: str, *, amount_minor: int = 499900) -> NewOrder:
    return NewOrder(amount_minor=amount_minor, currency="INR", receipt=receipt, notes={})


async def test_a_lost_create_response_recovers_the_order_that_exists() -> None:
    """The case the deterministic receipt exists for, end to end at the order layer.

    The gateway created the order and the answer never arrived. Creating again under a fresh
    identity would leave two orders for one payment. Asking what exists under this receipt
    resolves it with no second create.
    """
    client = FakeRazorpayClient(fail_next_create=True)
    request = an_order("ar_recovery_receipt")

    obtained = await obtain_order(client, request)

    assert obtained.recovered is True
    assert obtained.order.receipt == "ar_recovery_receipt"
    assert len(client.created_orders) == 1
    assert client.receipt_lookups == ["ar_recovery_receipt"]
    assert len(client.orders) == 1


async def test_a_duplicate_receipt_refusal_recovers_rather_than_creating_again() -> None:
    """The second preparation of one attempt, from a process that lost its own row.

    Razorpay refuses a second order under one receipt with a `BAD_REQUEST_ERROR`, and the
    recovery does not read that refusal's prose. It asks a question whose answer is true or
    false whatever the wording is.
    """
    client = FakeRazorpayClient()
    request = an_order("ar_duplicate_receipt")
    first = await obtain_order(client, request)

    second = await obtain_order(client, request)

    assert first.recovered is False
    assert second.recovered is True
    assert second.order.id == first.order.id
    assert len(client.orders) == 1


async def test_a_refusal_with_nothing_behind_it_is_re_raised() -> None:
    """Recovery must not mask a genuine validation failure.

    If Razorpay refused and no order exists under the receipt, the refusal is the answer. A
    recovery path that swallowed it would turn a rejected amount into a mysterious absence.
    """

    class RefusingClient(FakeRazorpayClient):
        async def create_order(self, order: NewOrder) -> RazorpayOrder:
            self.created_orders.append(order)
            raise RazorpayRefusedError(400, "BAD_REQUEST_ERROR", "amount must be at least 100")

    client = RefusingClient()

    with pytest.raises(RazorpayRefusedError, match="at least 100"):
        await obtain_order(client, an_order("ar_never_created"))

    assert client.receipt_lookups == ["ar_never_created"]


async def test_a_gateway_that_will_not_answer_the_lookup_stays_ambiguous() -> None:
    """Nothing may be concluded, so nothing is, and specifically nothing is created twice."""
    client = FakeRazorpayClient(unavailable=True)

    with pytest.raises(RazorpayUnavailableError):
        await obtain_order(client, an_order("ar_unavailable"))

    assert len(client.created_orders) == 1
    assert len(client.receipt_lookups) == 1


@pytest.mark.parametrize(
    ("field", "tamper"),
    [
        ("amount_minor", lambda order: replace(order, amount_minor=100)),
        ("currency", lambda order: replace(order, currency="USD")),
        ("receipt", lambda order: replace(order, receipt="ar_somebody_elses_receipt")),
    ],
)
async def test_an_order_that_does_not_match_the_attempt_is_refused(
    field: str, tamper: Callable[[RazorpayOrder], RazorpayOrder]
) -> None:
    """Fail closed. A mismatched order is never presented to Standard Checkout.

    An order arrives from a remote system, including one recovered from a listing, and the
    amount on it is what a customer will actually be charged. Continuing with a mismatch would
    mean collecting a number the mandate never authorized and then honouring it.
    """
    client = FakeRazorpayClient()
    expected = an_order("ar_expected_receipt")
    obtained = await obtain_order(client, expected)

    with pytest.raises(RazorpayOrderMismatchError) as refused:
        require_matching_order(tamper(obtained.order), expected)

    assert refused.value.field == field


async def test_a_matching_order_passes_quietly() -> None:
    client = FakeRazorpayClient()
    request = an_order("ar_expected_receipt")

    obtained = await obtain_order(client, request)

    require_matching_order(obtained.order, request)
