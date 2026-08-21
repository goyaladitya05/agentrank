"""Two requests carrying one idempotency key, arriving at the same instant.

A retry is the ordinary shape of a payment client. The first request times out somewhere above
this system, the client sends the same key again, and the two are then in flight together. The
contract says they are one logical operation, so both have to be answered with the one payment,
and exactly one provider call may ever happen.

The gap this file closes is narrow and was real. Admission resolves the identity and commits;
dispatch takes the attempt lock separately. Between those two, both requests can read ADMITTED,
and the one that loses the lock used to get a 409 `payment_in_progress` for a request that had
done nothing wrong and whose payment was proceeding perfectly well. The answer it should get is
the payment.

Every interleaving here is forced with the gate the other concurrency tests use, so nothing
passes by accident.
"""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentrank_api.audit.repository import AuditRepository
from agentrank_api.checkout.execution import CheckoutExecutionService
from agentrank_api.checkout.models import CheckoutSession, CheckoutStatus
from agentrank_api.checkout.quote import QuotedLine
from agentrank_api.checkout.repository import CheckoutRepository
from agentrank_api.commerce.models import Variant
from agentrank_api.commerce.repository import CatalogRepository, MerchantRepository
from agentrank_api.constraints.repository import IntentConstraintRepository
from agentrank_api.constraints.rules import ConstraintOperator, IntentConstraintSpec
from agentrank_api.inventory.models import ReservationStatus
from agentrank_api.inventory.repository import InventoryReservationRepository
from agentrank_api.mandates.models import SpendingMandate
from agentrank_api.mandates.repository import MandateRepository
from agentrank_api.payments.admission import PAYMENT_RESOURCE, PaymentAdmissionService
from agentrank_api.payments.fake import FakeOutcome, FakePaymentProvider
from agentrank_api.payments.models import PaymentAttempt, PaymentAttemptStatus
from agentrank_api.payments.repository import PaymentAttemptRepository
from agentrank_api.payments.service import PaymentResult, PaymentService

pytestmark = pytest.mark.anyio

NOW = datetime.now(UTC)
HOUR = timedelta(hours=1)
PRICE = 499900
STOCK = 5
KEY = "pay-ampere-0001"
BLACK = IntentConstraintSpec.required_attribute("color", ConstraintOperator.EQ, "black")

# A concurrent test that goes wrong blocks on a row lock rather than failing, so every gather
# is bounded. Generous enough never to fire on a healthy database.
CONCURRENCY_TIMEOUT = 30

# How long both requests are watched before concluding they are genuinely queued on the gate.
LOCK_WAIT = 1.5


@dataclass(frozen=True, slots=True)
class Shop:
    merchant_id: uuid.UUID
    mandate: SpendingMandate
    black: uuid.UUID


async def build_shop(session: AsyncSession) -> Shop:
    merchant = await MerchantRepository(session).create(slug="ampere-supply", name="Ampere")
    mandate = await MandateRepository(session).create(
        merchant_id=merchant.id,
        max_total_amount_minor=PRICE,
        currency="INR",
        valid_from=NOW - HOUR,
        valid_until=NOW + HOUR,
    )
    await IntentConstraintRepository(session).create(
        merchant_id=merchant.id, mandate_id=mandate.id, specs=[BLACK]
    )
    catalog = CatalogRepository(session)
    product = await catalog.create_product(
        merchant_id=merchant.id, external_id="amp-1", title="Charger", category="chargers"
    )
    black = await catalog.create_variant(
        product=product,
        sku="AMP-BLACK",
        price_amount_minor=PRICE,
        currency="INR",
        inventory_quantity=STOCK,
        attributes={"color": "black"},
    )
    await session.commit()
    return Shop(merchant_id=merchant.id, mandate=mandate, black=black.id)


async def prepared(session: AsyncSession, shop: Shop) -> CheckoutSession:
    checkout = await CheckoutRepository(session).create(
        merchant_id=shop.merchant_id,
        mandate_id=shop.mandate.id,
        currency="INR",
        lines=[
            QuotedLine(
                variant_id=shop.black,
                quantity=1,
                unit_price_amount_minor=PRICE,
                product_category="chargers",
                variant_attributes={"color": "black"},
            )
        ],
        expires_at=NOW + HOUR,
    )
    await session.commit()
    readiness = await CheckoutExecutionService(session).prepare_execution(checkout.id, at=NOW)
    assert readiness.ready
    return checkout


@pytest.fixture
async def shop(session: AsyncSession) -> Shop:
    return await build_shop(session)


@pytest.fixture
def provider() -> FakePaymentProvider:
    return FakePaymentProvider(default=FakeOutcome.SUCCESS)


async def pay_in_new_session(
    factory: async_sessionmaker[AsyncSession],
    checkout_id: uuid.UUID,
    provider: FakePaymentProvider,
    *,
    key: str = KEY,
) -> PaymentResult:
    """One payment request on its own connection, exactly as a route would run it."""
    async with factory() as session:
        return await PaymentService(session, provider).pay(checkout_id, idempotency_key=key)


async def still_waiting(*attempts: asyncio.Task[PaymentResult]) -> bool:
    """Whether every request is still blocked, watched for a bounded window."""
    done, _ = await asyncio.wait(set(attempts), timeout=LOCK_WAIT)
    return not done


async def stock(session: AsyncSession, variant_id: uuid.UUID) -> int:
    return int(
        await session.scalar(select(Variant.inventory_quantity).where(Variant.id == variant_id))
        or 0
    )


async def attempt_count(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(PaymentAttempt)) or 0)


async def test_a_same_key_retry_that_loses_the_dispatch_race_gets_the_payment(
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    shop: Shop,
    provider: FakePaymentProvider,
) -> None:
    """The exact interleaving the contract used to answer with a conflict.

    An attempt is admitted and left undispatched, which is the state a request whose process
    died after the admission commit leaves behind. Two retries carrying the same key then both
    resolve to it and both try to dispatch. A gate holds the attempt row so both are provably
    queued on the dispatch lock before either can decide anything.

    One of them wins and sends the payment. The other finds the attempt already IN_FLIGHT,
    which is the idempotent answer arriving a moment late rather than an error, and is given
    the payment. Neither raises, one attempt exists, and the provider was asked exactly once.
    """
    checkout = await prepared(session, shop)
    admission = await PaymentAdmissionService(session).admit_payment(
        checkout.id, idempotency_key=KEY, at=NOW
    )
    assert admission.attempt is not None
    attempt_id = admission.attempt.id
    checkout_id = checkout.id
    before = await stock(session, shop.black)

    async with asyncio.timeout(CONCURRENCY_TIMEOUT):
        async with factory() as gate:
            # The lock `dispatch` takes before it reads whether this attempt may be sent.
            await PaymentAttemptRepository(gate).get_for_update(attempt_id)
            retries: list[asyncio.Task[PaymentResult]] = [
                asyncio.create_task(pay_in_new_session(factory, checkout_id, provider)),
                asyncio.create_task(pay_in_new_session(factory, checkout_id, provider)),
            ]
            assert await still_waiting(*retries)
            await gate.rollback()

        first, second = await asyncio.gather(*retries)

    # Neither raised, and both are answered with the one payment.
    for result in (first, second):
        assert result.admission.admitted
        # Neither created it. The request that did is the one that died.
        assert result.admission.created is False
        assert result.attempt is not None
        assert result.attempt.id == attempt_id
        # And specifically not ADMITTED, which is what a caller would read as "nothing has
        # happened yet" about a payment that is at a provider.
        assert result.attempt.status is not PaymentAttemptStatus.ADMITTED

    # The assertion the whole change is for. One key, one logical payment, one provider call.
    assert provider.executions_for(KEY) == 1
    assert provider.charges == 1

    async with factory() as reader:
        assert await attempt_count(reader) == 1
        settled = await PaymentAttemptRepository(reader).get(attempt_id)
        assert settled is not None
        assert settled.status is PaymentAttemptStatus.SUCCEEDED
        paid = await CheckoutRepository(reader).get(checkout_id)
        assert paid is not None
        assert paid.status is CheckoutStatus.PAID
        consumed = await InventoryReservationRepository(reader).get(settled.reservation_id)
        assert consumed is not None
        assert consumed.status is ReservationStatus.CONSUMED
        # One unit, decremented by the request that actually dispatched.
        assert await stock(reader, shop.black) == before - 1
        recorded = [
            event.event_type
            for event in await AuditRepository(reader).list_for_resource(
                resource_type=PAYMENT_RESOURCE, resource_id=attempt_id
            )
        ]
        assert recorded == ["payment.admitted", "payment.succeeded"]


async def test_two_simultaneous_first_requests_produce_one_payment(
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    shop: Shop,
    provider: FakePaymentProvider,
) -> None:
    """The same key from a standing start, with nothing admitted yet.

    Both requests go through the whole composition. The admission lock decides which of them
    writes the attempt, and whichever loses is answered with it rather than being refused. One
    provider call across both.
    """
    checkout = await prepared(session, shop)
    checkout_id = checkout.id
    before = await stock(session, shop.black)

    async with asyncio.timeout(CONCURRENCY_TIMEOUT):
        async with factory() as gate:
            # The first lock admission takes, so both requests queue before either can decide
            # whether an attempt already exists.
            await MandateRepository(gate).get_for_update(
                shop.mandate.id, merchant_id=shop.merchant_id
            )
            retries: list[asyncio.Task[PaymentResult]] = [
                asyncio.create_task(pay_in_new_session(factory, checkout_id, provider)),
                asyncio.create_task(pay_in_new_session(factory, checkout_id, provider)),
            ]
            assert await still_waiting(*retries)
            await gate.rollback()

        first, second = await asyncio.gather(*retries)

    assert first.attempt is not None
    assert second.attempt is not None
    assert first.attempt.id == second.attempt.id
    # Exactly one of them wrote it. Which one is not determined and does not matter.
    assert [first.admission.created, second.admission.created].count(True) == 1
    assert provider.executions_for(KEY) == 1
    assert provider.charges == 1

    async with factory() as reader:
        assert await attempt_count(reader) == 1
        assert await stock(reader, shop.black) == before - 1


async def test_a_retry_completes_a_payment_admitted_by_a_process_that_died(
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    shop: Shop,
    provider: FakePaymentProvider,
) -> None:
    """Crash after admission, recovered by the ordinary retry, and not by anything special.

    ADMITTED means the provider has provably never heard of the identity, whoever wrote it, so
    a retry may complete it. This is the behavior that would be lost by making the dispatch
    conditional on having created the attempt, which is why it has a test of its own beside
    the race above.
    """
    checkout = await prepared(session, shop)
    admission = await PaymentAdmissionService(session).admit_payment(
        checkout.id, idempotency_key=KEY, at=NOW
    )
    assert admission.attempt is not None
    attempt_id = admission.attempt.id
    checkout_id = checkout.id
    before = await stock(session, shop.black)
    assert provider.executions == []

    result = await pay_in_new_session(factory, checkout_id, provider)

    assert result.admission.created is False
    assert result.attempt is not None
    assert result.attempt.id == attempt_id
    assert result.attempt.status is PaymentAttemptStatus.SUCCEEDED
    assert provider.executions_for(KEY) == 1

    async with factory() as reader:
        assert await attempt_count(reader) == 1
        assert await stock(reader, shop.black) == before - 1


async def test_a_retry_of_an_unresolved_payment_is_answered_without_a_provider_call(
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    shop: Shop,
    provider: FakePaymentProvider,
) -> None:
    """Returning the existing attempt is never permission to dispatch it.

    An UNKNOWN payment may already have moved money. The retry is answered with it, because
    that is what the caller asked about, and nothing about being answered turns into a second
    provider operation.
    """
    provider.default = FakeOutcome.AMBIGUOUS
    checkout = await prepared(session, shop)
    checkout_id = checkout.id
    first = await pay_in_new_session(factory, checkout_id, provider)
    assert first.attempt is not None
    assert first.attempt.status is PaymentAttemptStatus.UNKNOWN

    second = await pay_in_new_session(factory, checkout_id, provider)

    assert second.attempt is not None
    assert second.attempt.id == first.attempt.id
    assert second.attempt.status is PaymentAttemptStatus.UNKNOWN
    assert second.admission.created is False
    # One execute across both requests, and the second one reached no provider at all.
    assert provider.executions_for(KEY) == 1
