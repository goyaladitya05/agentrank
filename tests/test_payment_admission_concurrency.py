"""Admitting a payment while something else tries to admit, withdraw or cancel it.

Every test here forces one interleaving rather than hoping for it. The technique is the one
the earlier concurrency tests use: a third transaction holds a lock while the racing
operations start, so they are provably in flight and queued before any of them can decide
anything. Two coroutines gathered without a gate take their turns by accident, and a test
that passes by accident would pass with no locking at all.

What is being defended is narrow and expensive to get wrong. Two requests must not produce
two payments for one thing, a checkout must not be withdrawn out from under a payment that
may already be at a provider, and a mandate that authorizes one purchase must not end up
paying for two.
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
from agentrank_api.checkout.service import CheckoutService
from agentrank_api.commerce.repository import CatalogRepository, MerchantRepository
from agentrank_api.constraints.repository import IntentConstraintRepository
from agentrank_api.constraints.rules import ConstraintOperator, IntentConstraintSpec
from agentrank_api.errors import ConflictError
from agentrank_api.inventory.models import ReservationStatus
from agentrank_api.inventory.repository import InventoryReservationRepository
from agentrank_api.mandates.models import SpendingMandate
from agentrank_api.mandates.repository import MandateRepository
from agentrank_api.mandates.service import MandateService
from agentrank_api.payments.admission import (
    PAYMENT_RESOURCE,
    AdmissionRefusal,
    PaymentAdmission,
    PaymentAdmissionService,
)
from agentrank_api.payments.models import PaymentAttempt, PaymentAttemptStatus

pytestmark = pytest.mark.anyio

NOW = datetime.now(UTC)
HOUR = timedelta(hours=1)
PRICE = 499900
KEY = "pay-ampere-0001"
OTHER_KEY = "pay-ampere-0002"
BLACK = IntentConstraintSpec.required_attribute("color", ConstraintOperator.EQ, "black")

# A concurrent test that goes wrong blocks on a row lock rather than failing, so every gather
# is bounded. Generous enough never to fire on a healthy database.
CONCURRENCY_TIMEOUT = 30

# How long an attempt is watched before concluding it is genuinely waiting on a lock. An
# attempt that is really blocked never completes, so a longer window cannot make these tests
# flaky, and an implementation that took no lock would finish here in milliseconds.
LOCK_WAIT = 1.5


@dataclass(frozen=True, slots=True)
class Shop:
    """A merchant whose mandate and constraints both permit one black charger."""

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
        inventory_quantity=4,
        attributes={"color": "black"},
    )
    await session.commit()
    return Shop(merchant_id=merchant.id, mandate=mandate, black=black.id)


async def prepared(session: AsyncSession, shop: Shop) -> CheckoutSession:
    """A quote with stock already held for it, written without the checkout service.

    Deliberately not through `CheckoutService.create_checkout`, so the only payment events
    these tests see are the ones the code under test appends.
    """
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
    readiness = await CheckoutExecutionService(session).prepare_execution(
        checkout.id, merchant_id=checkout.merchant_id, at=NOW
    )
    assert readiness.ready
    return checkout


@pytest.fixture
async def shop(session: AsyncSession) -> Shop:
    return await build_shop(session)


async def admit_in_new_session(
    factory: async_sessionmaker[AsyncSession],
    checkout_id: uuid.UUID,
    merchant_id: uuid.UUID,
    *,
    key: str,
) -> PaymentAdmission:
    """One admission on its own connection. It commits or rolls back before returning."""
    async with factory() as session:
        return await PaymentAdmissionService(session).admit_payment(
            checkout_id, merchant_id=merchant_id, idempotency_key=key, at=NOW
        )


async def cancel_in_new_session(
    factory: async_sessionmaker[AsyncSession], checkout_id: uuid.UUID, merchant_id: uuid.UUID
) -> CheckoutSession | ConflictError:
    async with factory() as session:
        try:
            return await CheckoutService(session).cancel_checkout(
                checkout_id, merchant_id=merchant_id
            )
        except ConflictError as refused:
            return refused


async def revoke_in_new_session(
    factory: async_sessionmaker[AsyncSession], mandate_id: uuid.UUID, merchant_id: uuid.UUID
) -> SpendingMandate:
    async with factory() as session:
        return await MandateService(session).revoke_mandate(mandate_id, merchant_id=merchant_id)


async def still_waiting(*attempts: asyncio.Task[object]) -> bool:
    """Whether every attempt is still blocked, watched for a bounded window."""
    done, _ = await asyncio.wait(set(attempts), timeout=LOCK_WAIT)
    return not done


async def attempt_count(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(PaymentAttempt)) or 0)


async def test_two_identical_requests_admit_one_payment(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession], shop: Shop
) -> None:
    """The duplicate request case, forced rather than hoped for.

    Both carry the same idempotency key, so they are the same logical operation. Exactly one
    attempt may exist, both callers must be told about that one, and only one
    `payment.admitted` event may be written. A gate holds the checkout so both are provably
    queued before either can read whether an attempt exists.
    """
    checkout = await prepared(session, shop)

    async with asyncio.timeout(CONCURRENCY_TIMEOUT):
        async with factory() as gate:
            await CheckoutRepository(gate).get_for_update(
                checkout.id, merchant_id=checkout.merchant_id
            )
            attempts: list[asyncio.Task[PaymentAdmission]] = [
                asyncio.create_task(
                    admit_in_new_session(factory, checkout.id, shop.merchant_id, key=KEY)
                ),
                asyncio.create_task(
                    admit_in_new_session(factory, checkout.id, shop.merchant_id, key=KEY)
                ),
            ]
            assert await still_waiting(*attempts)
            await gate.rollback()

        first, second = await asyncio.gather(*attempts)

    assert first.admitted
    assert second.admitted
    assert first.attempt is not None
    assert second.attempt is not None
    assert first.attempt.id == second.attempt.id
    # One of them wrote it and the other found it. Which one is not determined and does not
    # matter; that exactly one wrote it does.
    assert [first.created, second.created].count(True) == 1

    async with factory() as reader:
        assert await attempt_count(reader) == 1
        events = await AuditRepository(reader).list_for_resource(
            resource_type=PAYMENT_RESOURCE, resource_id=first.attempt.id
        )
        assert [event.event_type for event in events] == ["payment.admitted"]


async def test_two_different_keys_admit_at_most_one_payment(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession], shop: Shop
) -> None:
    """Two identities racing for one checkout. One wins and the other is told why.

    Not two attempts and a hope that the provider's own idempotency sorts it out: the two
    keys are different, so provider idempotency would not help at all.
    """
    checkout = await prepared(session, shop)

    async with asyncio.timeout(CONCURRENCY_TIMEOUT):
        async with factory() as gate:
            await CheckoutRepository(gate).get_for_update(
                checkout.id, merchant_id=checkout.merchant_id
            )
            attempts: list[asyncio.Task[PaymentAdmission]] = [
                asyncio.create_task(
                    admit_in_new_session(factory, checkout.id, shop.merchant_id, key=KEY)
                ),
                asyncio.create_task(
                    admit_in_new_session(factory, checkout.id, shop.merchant_id, key=OTHER_KEY)
                ),
            ]
            assert await still_waiting(*attempts)
            await gate.rollback()

        outcomes = await asyncio.gather(*attempts)

    admitted = [outcome for outcome in outcomes if outcome.admitted]
    refused = [outcome for outcome in outcomes if not outcome.admitted]
    assert len(admitted) == 1
    assert len(refused) == 1
    assert refused[0].refusal is AdmissionRefusal.PAYMENT_IN_PROGRESS

    async with factory() as reader:
        assert await attempt_count(reader) == 1


async def test_two_checkouts_under_one_mandate_admit_one_payment(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession], shop: Shop
) -> None:
    """The single purchase mandate rule under contention.

    Two candidate checkouts, both prepared, both authorized, racing toward a provider. Only
    one may be admitted, because two admissions mean two provider calls and two charges that
    only one row could ever record. The mandate is the gate, and both admissions take it
    first, so they serialize on it rather than on the checkout they do not share.
    """
    first = await prepared(session, shop)
    second = await prepared(session, shop)

    async with asyncio.timeout(CONCURRENCY_TIMEOUT):
        async with factory() as gate:
            await MandateRepository(gate).get_for_update(
                shop.mandate.id, merchant_id=shop.merchant_id
            )
            attempts: list[asyncio.Task[PaymentAdmission]] = [
                asyncio.create_task(
                    admit_in_new_session(factory, first.id, shop.merchant_id, key=KEY)
                ),
                asyncio.create_task(
                    admit_in_new_session(factory, second.id, shop.merchant_id, key=OTHER_KEY)
                ),
            ]
            assert await still_waiting(*attempts)
            await gate.rollback()

        outcomes = await asyncio.gather(*attempts)

    admitted = [outcome for outcome in outcomes if outcome.admitted]
    assert len(admitted) == 1
    refused = next(outcome for outcome in outcomes if not outcome.admitted)
    assert refused.refusal is AdmissionRefusal.MANDATE_PAYMENT_IN_PROGRESS

    async with factory() as reader:
        assert await attempt_count(reader) == 1
        # The loser's hold is untouched: refusing wrote nothing at all.
        held = await InventoryReservationRepository(reader).get_holding_for_checkout(
            refused.checkout_id
        )
        assert held is not None
        assert held.status is ReservationStatus.ACTIVE


async def test_a_cancellation_in_flight_blocks_and_then_refuses_admission(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession], shop: Shop
) -> None:
    """Cancellation wins the race, and admission observes it rather than reading past it.

    An unlocked read would see the last committed row, which still says OPEN, and would admit
    a payment for a quote that is about to be withdrawn.
    """
    checkout = await prepared(session, shop)

    async with asyncio.timeout(CONCURRENCY_TIMEOUT):
        async with factory() as canceller:
            withdrawn = await CheckoutRepository(canceller).get_for_update(
                checkout.id, merchant_id=checkout.merchant_id
            )
            assert withdrawn is not None
            assert await CheckoutRepository(canceller).cancel(withdrawn) is True

            attempt = asyncio.create_task(
                admit_in_new_session(factory, checkout.id, shop.merchant_id, key=KEY)
            )
            assert await still_waiting(attempt)
            await canceller.commit()

        admission = await attempt

    assert not admission.admitted
    assert admission.refusal is AdmissionRefusal.NOT_AUTHORIZED

    async with factory() as reader:
        assert await attempt_count(reader) == 0


async def test_a_revocation_in_flight_blocks_and_then_refuses_admission(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession], shop: Shop
) -> None:
    """The same race one level up the lock order."""
    checkout = await prepared(session, shop)

    async with asyncio.timeout(CONCURRENCY_TIMEOUT):
        async with factory() as revoker:
            withdrawn = await MandateRepository(revoker).get_for_update(
                shop.mandate.id, merchant_id=shop.merchant_id
            )
            assert withdrawn is not None
            assert await MandateRepository(revoker).revoke(withdrawn) is True

            attempt = asyncio.create_task(
                admit_in_new_session(factory, checkout.id, shop.merchant_id, key=KEY)
            )
            assert await still_waiting(attempt)
            await revoker.commit()

        admission = await attempt

    assert not admission.admitted
    assert admission.refusal is AdmissionRefusal.NOT_AUTHORIZED

    async with factory() as reader:
        assert await attempt_count(reader) == 0


async def test_admission_holds_the_checkout_against_a_cancellation(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession], shop: Shop
) -> None:
    """Admission wins, and the withdrawal applies to the world it left behind.

    A gate holds the variant rows, so the admission is provably past both gates and holding
    the mandate and the checkout while it waits. A cancellation started at that point must
    wait for it, and must then find a payment it cannot cancel around.

    This is the interleaving the whole `payment_in_progress` refusal exists for. Without the
    lock, the cancellation would decide on an open checkout with no attempt and release the
    hold while the payment was being written.
    """
    checkout = await prepared(session, shop)

    async with asyncio.timeout(CONCURRENCY_TIMEOUT):
        async with factory() as gate:
            await InventoryReservationRepository(gate).lock_variants(
                merchant_id=shop.merchant_id, variant_ids=[shop.black]
            )
            attempt = asyncio.create_task(
                admit_in_new_session(factory, checkout.id, shop.merchant_id, key=KEY)
            )
            assert await still_waiting(attempt)

            withdrawal = asyncio.create_task(
                cancel_in_new_session(factory, checkout.id, shop.merchant_id)
            )
            assert await still_waiting(withdrawal)
            await gate.rollback()

        admission = await attempt
        outcome = await withdrawal

    assert admission.admitted
    assert isinstance(outcome, ConflictError)
    assert outcome.reason == "payment_in_progress"

    async with factory() as reader:
        found = await CheckoutRepository(reader).get(checkout.id, merchant_id=checkout.merchant_id)
        assert found is not None
        assert found.status is CheckoutStatus.OPEN
        # The hold is bound to the payment and was not given back underneath it.
        held = await InventoryReservationRepository(reader).get_holding_for_checkout(checkout.id)
        assert held is not None
        assert held.status is ReservationStatus.COMMITTED


async def test_a_revocation_after_admission_does_not_invalidate_the_attempt(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession], shop: Shop
) -> None:
    """Admission was the authorization instant, and revocation is not retroactive.

    A payment admitted while the mandate was active stays admitted after it is revoked. What
    revocation does is stop the next admission, which is asserted here too so that the two
    directions are not confused for each other.
    """
    checkout = await prepared(session, shop)
    # Prepared before the revocation, because preparation needs an active mandate too. What
    # is under test is admission after revocation, not preparation after it.
    second = await prepared(session, shop)

    async with asyncio.timeout(CONCURRENCY_TIMEOUT):
        async with factory() as gate:
            await InventoryReservationRepository(gate).lock_variants(
                merchant_id=shop.merchant_id, variant_ids=[shop.black]
            )
            attempt = asyncio.create_task(
                admit_in_new_session(factory, checkout.id, shop.merchant_id, key=KEY)
            )
            assert await still_waiting(attempt)

            revocation = asyncio.create_task(
                revoke_in_new_session(factory, shop.mandate.id, shop.merchant_id)
            )
            assert await still_waiting(revocation)
            await gate.rollback()

        admission = await attempt
        await revocation

    assert admission.admitted
    assert admission.attempt is not None
    admitted_id = admission.attempt.id

    async with factory() as reader:
        found = await reader.get(PaymentAttempt, admitted_id)
        assert found is not None
        # Still admitted, still dispatchable. Time and withdrawal work forwards.
        assert found.status is PaymentAttemptStatus.ADMITTED

    # And the next one is refused, which is what revocation is actually for.
    later = await admit_in_new_session(factory, second.id, shop.merchant_id, key=OTHER_KEY)
    assert not later.admitted
    assert later.refusal is AdmissionRefusal.NOT_AUTHORIZED
