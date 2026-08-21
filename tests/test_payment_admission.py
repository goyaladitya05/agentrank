"""Admitting a payment, and every reason not to.

Admission is the moment this phase exists for. Everything a provider call will rest on is
established here, under locks, and the `PaymentAttempt` proving it is written before those
locks are released. So these tests are mostly about refusals: each one is a way the world can
be in which no attempt may exist, and each asserts that no row was written at all rather than
that a flag came back false.

The one thing no test here does is call a provider. There is none yet, which is the point:
admission is complete on its own, and an attempt that has been admitted and never dispatched
is a valid, recoverable state rather than a half finished operation.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentrank_api.audit.models import ActorType
from agentrank_api.audit.repository import AuditRepository
from agentrank_api.checkout.authorization import CheckoutAuthorizationViolation
from agentrank_api.checkout.execution import CheckoutExecutionService
from agentrank_api.checkout.execution_authorization import ExecutionAuthorizationViolation
from agentrank_api.checkout.intent_authorization import IntentViolationCode
from agentrank_api.checkout.models import CheckoutSession, CheckoutStatus
from agentrank_api.checkout.quote import QuotedLine
from agentrank_api.checkout.repository import CheckoutRepository
from agentrank_api.checkout.service import CheckoutService
from agentrank_api.commerce.repository import CatalogRepository, MerchantRepository
from agentrank_api.constraints.repository import IntentConstraintRepository
from agentrank_api.constraints.rules import ConstraintOperator, IntentConstraintSpec
from agentrank_api.errors import ConflictError
from agentrank_api.inventory.models import InventoryReservation, ReservationStatus
from agentrank_api.inventory.repository import InventoryReservationRepository
from agentrank_api.inventory.service import ReleaseReason
from agentrank_api.locking import respects_lock_order
from agentrank_api.mandates.models import MandateStatus, SpendingMandate
from agentrank_api.mandates.repository import MandateRepository
from agentrank_api.payments.admission import (
    PAYMENT_RESOURCE,
    AdmissionRefusal,
    PaymentAdmissionService,
)
from agentrank_api.payments.models import PaymentAttempt, PaymentAttemptStatus
from agentrank_api.payments.rules import generate_idempotency_key, validate_idempotency_key

pytestmark = pytest.mark.anyio

NOW = datetime.now(UTC)
HOUR = timedelta(hours=1)
PRICE = 499900
KEY = "pay-ampere-0001"
OTHER_KEY = "pay-ampere-0002"
BLACK = IntentConstraintSpec.required_attribute("color", ConstraintOperator.EQ, "black")


@dataclass(frozen=True, slots=True)
class Shop:
    """A merchant whose mandate and constraints both permit exactly one black charger."""

    merchant_id: uuid.UUID
    mandate: SpendingMandate
    black: uuid.UUID


async def build_shop(
    session: AsyncSession,
    slug: str = "ampere-supply",
    *,
    inventory: int = 3,
    constrained: bool = True,
) -> Shop:
    merchant = await MerchantRepository(session).create(slug=slug, name=slug.title())
    mandate = await MandateRepository(session).create(
        merchant_id=merchant.id,
        max_total_amount_minor=PRICE,
        currency="INR",
        valid_from=NOW - HOUR,
        valid_until=NOW + HOUR,
    )
    if constrained:
        await IntentConstraintRepository(session).create(
            merchant_id=merchant.id, mandate_id=mandate.id, specs=[BLACK]
        )
    catalog = CatalogRepository(session)
    product = await catalog.create_product(
        merchant_id=merchant.id, external_id=f"{slug}-1", title="Charger", category="chargers"
    )
    black = await catalog.create_variant(
        product=product,
        sku=f"{slug}-black",
        price_amount_minor=PRICE,
        currency="INR",
        inventory_quantity=inventory,
        attributes={"color": "black"},
    )
    await session.commit()
    return Shop(merchant_id=merchant.id, mandate=mandate, black=black.id)


async def quote(
    session: AsyncSession, shop: Shop, *, expires_at: datetime | None = None
) -> CheckoutSession:
    """A quote written straight through the repository.

    Deliberately not through `CheckoutService`, so the only events these tests see are the
    ones the code under test appends.
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
        expires_at=expires_at or NOW + HOUR,
    )
    await session.commit()
    return checkout


async def prepared(session: AsyncSession, shop: Shop, **kwargs: object) -> CheckoutSession:
    """A quote with stock already held for it, which is what admission requires."""
    checkout = await quote(session, shop, **kwargs)  # type: ignore[arg-type]
    readiness = await CheckoutExecutionService(session).prepare_execution(checkout.id, at=NOW)
    assert readiness.ready
    return checkout


@pytest.fixture
async def shop(session: AsyncSession) -> Shop:
    return await build_shop(session)


async def attempt_count(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(PaymentAttempt)) or 0)


async def test_a_prepared_checkout_is_admitted(session: AsyncSession, shop: Shop) -> None:
    """Both gates allow, stock is held, and the attempt exists before anything is dispatched."""
    checkout = await prepared(session, shop)

    admission = await PaymentAdmissionService(session).admit_payment(
        checkout.id, idempotency_key=KEY, at=NOW
    )

    assert admission.admitted
    assert admission.created
    assert admission.refusal is None
    assert admission.admitted_at is not None
    attempt = admission.attempt
    assert attempt is not None
    assert attempt.status is PaymentAttemptStatus.ADMITTED
    assert attempt.idempotency_key == KEY
    assert attempt.merchant_id == shop.merchant_id
    assert attempt.mandate_id == shop.mandate.id
    # Frozen, and equal to the quote by composite foreign key rather than by this comparison.
    assert attempt.amount_minor == checkout.total_amount_minor
    assert attempt.currency == checkout.currency
    # Certainly not dispatched. That is what makes an ADMITTED attempt safe to recover.
    assert attempt.dispatched_at is None


async def test_admission_commits_the_reservation_to_the_attempt(
    session: AsyncSession, shop: Shop
) -> None:
    """The hold stops being expiry governed, and the attempt names it."""
    checkout = await prepared(session, shop)

    admission = await PaymentAdmissionService(session).admit_payment(
        checkout.id, idempotency_key=KEY, at=NOW
    )
    assert admission.attempt is not None

    reservation = await InventoryReservationRepository(session).get_holding_for_checkout(
        checkout.id
    )
    assert reservation is not None
    assert reservation.status is ReservationStatus.COMMITTED
    assert admission.attempt.reservation_id == reservation.id
    # Committed, not consumed. Nothing has been bought.
    assert reservation.consumed_at is None
    assert (
        await session.scalar(
            select(InventoryReservation.checkout_id).where(
                InventoryReservation.id == reservation.id
            )
        )
        == checkout.id
    )


async def test_admission_appends_exactly_one_event(session: AsyncSession, shop: Shop) -> None:
    checkout = await prepared(session, shop)

    admission = await PaymentAdmissionService(session).admit_payment(
        checkout.id, idempotency_key=KEY, at=NOW
    )
    assert admission.attempt is not None

    events = await AuditRepository(session).list_for_resource(
        resource_type=PAYMENT_RESOURCE, resource_id=admission.attempt.id
    )
    assert [event.event_type for event in events] == ["payment.admitted"]
    event = events[0]
    # Derived from the attempt, never from anything a caller sent.
    assert event.merchant_id == shop.merchant_id
    assert event.actor_type is ActorType.BUYER
    assert event.payload["amount_minor"] == checkout.total_amount_minor
    assert event.payload["currency"] == "INR"
    assert event.payload["reservation_id"] == str(admission.attempt.reservation_id)
    # An identity that travels to a provider does not belong in a trail read in more places.
    assert "idempotency_key" not in event.payload


async def test_the_same_idempotency_key_returns_the_same_attempt(
    session: AsyncSession, shop: Shop
) -> None:
    """One logical operation, asked twice. No second attempt and no second event."""
    checkout = await prepared(session, shop)
    service = PaymentAdmissionService(session)

    first = await service.admit_payment(checkout.id, idempotency_key=KEY, at=NOW)
    second = await service.admit_payment(checkout.id, idempotency_key=KEY, at=NOW)

    assert first.attempt is not None
    assert second.attempt is not None
    assert second.attempt.id == first.attempt.id
    assert first.created is True
    assert second.created is False
    assert await attempt_count(session) == 1

    events = await AuditRepository(session).list_for_resource(
        resource_type=PAYMENT_RESOURCE, resource_id=first.attempt.id
    )
    assert [event.event_type for event in events] == ["payment.admitted"]


async def test_a_different_key_against_a_paying_checkout_is_refused(
    session: AsyncSession, shop: Shop
) -> None:
    """A second identity must not start a competing provider operation."""
    checkout = await prepared(session, shop)
    service = PaymentAdmissionService(session)
    await service.admit_payment(checkout.id, idempotency_key=KEY, at=NOW)

    rival = await service.admit_payment(checkout.id, idempotency_key=OTHER_KEY, at=NOW)

    assert not rival.admitted
    assert rival.refusal is AdmissionRefusal.PAYMENT_IN_PROGRESS
    assert rival.attempt is None
    assert await attempt_count(session) == 1


async def test_a_sibling_checkout_under_one_mandate_is_refused(
    session: AsyncSession, shop: Shop
) -> None:
    """A mandate authorizes one purchase, so it pays for one checkout at a time.

    Stricter than "one success" needs to be, and stricter on purpose. Allowing both to be
    admitted means both can be dispatched and both can succeed at a provider, and only one of
    them could ever be recorded.
    """
    first = await prepared(session, shop)
    second = await prepared(session, shop)
    service = PaymentAdmissionService(session)
    await service.admit_payment(first.id, idempotency_key=KEY, at=NOW)

    sibling = await service.admit_payment(second.id, idempotency_key=OTHER_KEY, at=NOW)

    assert not sibling.admitted
    assert sibling.refusal is AdmissionRefusal.MANDATE_PAYMENT_IN_PROGRESS
    assert await attempt_count(session) == 1


async def test_a_checkout_with_no_reservation_is_refused(session: AsyncSession, shop: Shop) -> None:
    """Admission requires a hold and never takes one.

    Holding stock is execution preparation's decision. A payment that reserved as a side
    effect would take stock off a shelf without anybody asking it to.
    """
    checkout = await quote(session, shop)

    admission = await PaymentAdmissionService(session).admit_payment(
        checkout.id, idempotency_key=KEY, at=NOW
    )

    assert admission.refusal is AdmissionRefusal.RESERVATION_MISSING
    assert await attempt_count(session) == 0


async def test_an_expired_reservation_is_refused_rather_than_committed(
    session: AsyncSession, shop: Shop
) -> None:
    """A lapsed hold is not made permanent. It would be stock promised to two buyers.

    Not reachable through execution preparation, which derives a reservation expiry as the
    earlier of the quote expiry and the mandate validity, so a lapsed hold belongs to a
    checkout the gates already refuse. The hold is therefore written directly with a short
    expiry of its own, because the unreachable case still needs an answer and the answer must
    not be "commit it anyway".
    """
    checkout = await quote(session, shop)
    reservation = await InventoryReservationRepository(session).create(
        merchant_id=shop.merchant_id,
        checkout_id=checkout.id,
        expires_at=NOW + timedelta(seconds=30),
        quantities={shop.black: 1},
    )
    await session.commit()
    # Read before the refusal rolls back and expires the object.
    reservation_id = reservation.id

    admission = await PaymentAdmissionService(session).admit_payment(
        checkout.id, idempotency_key=KEY, at=NOW + timedelta(minutes=1)
    )

    assert admission.refusal is AdmissionRefusal.RESERVATION_EXPIRED
    assert await attempt_count(session) == 0
    lapsed = await InventoryReservationRepository(session).get(reservation_id)
    assert lapsed is not None
    # Refused, not committed and not renewed. Committing it would take a claim that had
    # stopped holding stock and make it permanent.
    assert lapsed.status is ReservationStatus.ACTIVE


async def test_a_released_reservation_is_missing_rather_than_expired(
    session: AsyncSession, shop: Shop
) -> None:
    checkout = await prepared(session, shop)
    await CheckoutExecutionService(session).release_reservation(
        checkout.id, reason=ReleaseReason.RESERVATION_RECOVERED
    )

    admission = await PaymentAdmissionService(session).admit_payment(
        checkout.id, idempotency_key=KEY, at=NOW
    )

    assert admission.refusal is AdmissionRefusal.RESERVATION_MISSING
    assert await attempt_count(session) == 0


async def test_a_cancelled_checkout_is_refused(session: AsyncSession, shop: Shop) -> None:
    checkout = await prepared(session, shop)
    await CheckoutService(session).cancel_checkout(checkout.id)

    admission = await PaymentAdmissionService(session).admit_payment(
        checkout.id, idempotency_key=KEY, at=NOW
    )

    assert admission.refusal is AdmissionRefusal.NOT_AUTHORIZED
    assert (
        CheckoutAuthorizationViolation.CHECKOUT_NOT_OPEN
        in admission.authorization.financial.violations
    )
    assert await attempt_count(session) == 0


async def test_an_expired_checkout_is_refused(session: AsyncSession, shop: Shop) -> None:
    checkout = await prepared(session, shop)

    admission = await PaymentAdmissionService(session).admit_payment(
        checkout.id, idempotency_key=KEY, at=NOW + HOUR + HOUR
    )

    assert admission.refusal is AdmissionRefusal.NOT_AUTHORIZED
    assert (
        CheckoutAuthorizationViolation.CHECKOUT_EXPIRED
        in admission.authorization.financial.violations
    )
    assert await attempt_count(session) == 0


async def test_a_revoked_mandate_is_refused(session: AsyncSession, shop: Shop) -> None:
    """Revocation before admission stops the admission. After it, it stops the next one."""
    checkout = await prepared(session, shop)
    mandate = await MandateRepository(session).get_for_update(shop.mandate.id)
    assert mandate is not None
    await MandateRepository(session).revoke(mandate)
    await session.commit()

    admission = await PaymentAdmissionService(session).admit_payment(
        checkout.id, idempotency_key=KEY, at=NOW
    )

    assert admission.refusal is AdmissionRefusal.NOT_AUTHORIZED
    assert (
        CheckoutAuthorizationViolation.MANDATE_NOT_ACTIVE
        in admission.authorization.financial.violations
    )
    assert await attempt_count(session) == 0


async def test_an_expired_mandate_is_refused(session: AsyncSession, shop: Shop) -> None:
    checkout = await prepared(session, shop)

    admission = await PaymentAdmissionService(session).admit_payment(
        checkout.id, idempotency_key=KEY, at=NOW + HOUR + HOUR
    )

    assert admission.refusal is AdmissionRefusal.NOT_AUTHORIZED
    assert (
        CheckoutAuthorizationViolation.MANDATE_EXPIRED
        in admission.authorization.financial.violations
    )
    assert await attempt_count(session) == 0


async def test_a_mandate_with_no_constraint_set_is_refused(session: AsyncSession) -> None:
    """Absence of a semantic authorization is not a passed one, at the payment boundary too."""
    unqualified = await build_shop(session, "volt-supply", constrained=False)
    checkout = await quote(session, unqualified)

    admission = await PaymentAdmissionService(session).admit_payment(
        checkout.id, idempotency_key=KEY, at=NOW
    )

    assert admission.refusal is AdmissionRefusal.NOT_AUTHORIZED
    assert (
        ExecutionAuthorizationViolation.INTENT_CONSTRAINTS_MISSING
        in admission.authorization.violations
    )
    assert await attempt_count(session) == 0


async def test_a_semantic_denial_is_refused(session: AsyncSession) -> None:
    """The money is fine and the thing is wrong, which is a denial in its own vocabulary."""
    shop = await build_shop(session, "volt-supply")
    blue = await CatalogRepository(session).create_variant(
        product=(
            await CatalogRepository(session).create_product(
                merchant_id=shop.merchant_id,
                external_id="volt-2",
                title="Charger",
                category="chargers",
            )
        ),
        sku="volt-blue",
        price_amount_minor=PRICE,
        currency="INR",
        inventory_quantity=3,
        attributes={"color": "blue"},
    )
    checkout = await CheckoutRepository(session).create(
        merchant_id=shop.merchant_id,
        mandate_id=shop.mandate.id,
        currency="INR",
        lines=[
            QuotedLine(
                variant_id=blue.id,
                quantity=1,
                unit_price_amount_minor=PRICE,
                product_category="chargers",
                variant_attributes={"color": "blue"},
            )
        ],
        expires_at=NOW + HOUR,
    )
    await session.commit()

    admission = await PaymentAdmissionService(session).admit_payment(
        checkout.id, idempotency_key=KEY, at=NOW
    )

    assert admission.refusal is AdmissionRefusal.NOT_AUTHORIZED
    assert admission.authorization.intent is not None
    assert [violation.code for violation in admission.authorization.intent.violations] == [
        IntentViolationCode.REQUIRED_ATTRIBUTE_MISMATCH
    ]
    assert await attempt_count(session) == 0


async def test_a_financial_denial_is_refused(session: AsyncSession, shop: Shop) -> None:
    """The thing is right and the money is not."""
    checkout = await CheckoutRepository(session).create(
        merchant_id=shop.merchant_id,
        mandate_id=shop.mandate.id,
        currency="INR",
        lines=[
            QuotedLine(
                variant_id=shop.black,
                quantity=2,
                unit_price_amount_minor=PRICE,
                product_category="chargers",
                variant_attributes={"color": "black"},
            )
        ],
        expires_at=NOW + HOUR,
    )
    await session.commit()

    admission = await PaymentAdmissionService(session).admit_payment(
        checkout.id, idempotency_key=KEY, at=NOW
    )

    assert admission.refusal is AdmissionRefusal.NOT_AUTHORIZED
    assert (
        CheckoutAuthorizationViolation.MAX_TOTAL_EXCEEDED
        in admission.authorization.financial.violations
    )
    assert await attempt_count(session) == 0


async def test_a_mandate_already_consumed_is_refused(session: AsyncSession, shop: Shop) -> None:
    """The single purchase rule, refused with a reason before the index has to refuse it."""
    first = await prepared(session, shop)
    admission = await PaymentAdmissionService(session).admit_payment(
        first.id, idempotency_key=KEY, at=NOW
    )
    assert admission.attempt is not None
    await _force_succeeded(session, admission.attempt.id)

    second = await prepared(session, shop)
    later = await PaymentAdmissionService(session).admit_payment(
        second.id, idempotency_key=OTHER_KEY, at=NOW
    )

    assert not later.admitted
    assert later.refusal is AdmissionRefusal.MANDATE_ALREADY_CONSUMED
    assert await attempt_count(session) == 1


async def test_a_declined_attempt_leaves_room_for_a_new_one(
    session: AsyncSession, shop: Shop
) -> None:
    """A decline consumed nothing, so a fresh hold and a fresh key may be admitted."""
    checkout = await prepared(session, shop)
    service = PaymentAdmissionService(session)
    admission = await service.admit_payment(checkout.id, idempotency_key=KEY, at=NOW)
    assert admission.attempt is not None
    await _force_failed(session, admission.attempt.id)
    await _release(session, checkout.id)

    # A fresh reservation, exactly as a real retry would need: the old one is released.
    readiness = await CheckoutExecutionService(session).prepare_execution(checkout.id, at=NOW)
    assert readiness.ready

    again = await service.admit_payment(checkout.id, idempotency_key=OTHER_KEY, at=NOW)

    assert again.admitted
    assert again.created
    assert again.attempt is not None
    assert again.attempt.id != admission.attempt.id
    assert await attempt_count(session) == 2


async def test_admission_takes_its_locks_in_the_documented_order(
    session: AsyncSession, shop: Shop, row_locks: list[str]
) -> None:
    """Deadlock freedom is a property of the order, so the order is what is asserted."""
    checkout = await prepared(session, shop)
    row_locks.clear()

    admission = await PaymentAdmissionService(session).admit_payment(
        checkout.id, idempotency_key=KEY, at=NOW
    )

    assert admission.admitted
    assert respects_lock_order(row_locks)
    assert row_locks[:3] == ["spending_mandate", "checkout_session", "variant"]


async def test_a_cancellation_is_refused_while_a_payment_is_open(
    session: AsyncSession, shop: Shop
) -> None:
    """A quote with a payment that may reach a provider is not a withdrawable quote.

    The alternative was to let cancellation win and have the payment discover it, which means
    deciding what to do about a provider call already dispatched for a quote nobody wants.
    """
    checkout = await prepared(session, shop)
    await PaymentAdmissionService(session).admit_payment(checkout.id, idempotency_key=KEY, at=NOW)

    with pytest.raises(ConflictError) as refused:
        await CheckoutService(session).cancel_checkout(checkout.id)

    assert refused.value.reason == "payment_in_progress"
    reservation = await InventoryReservationRepository(session).get_holding_for_checkout(
        checkout.id
    )
    assert reservation is not None
    assert reservation.status is ReservationStatus.COMMITTED
    found = await CheckoutRepository(session).get(checkout.id)
    assert found is not None
    assert found.status is CheckoutStatus.OPEN


async def test_a_cancellation_succeeds_once_the_payment_has_definitively_failed(
    session: AsyncSession, shop: Shop
) -> None:
    checkout = await prepared(session, shop)
    admission = await PaymentAdmissionService(session).admit_payment(
        checkout.id, idempotency_key=KEY, at=NOW
    )
    assert admission.attempt is not None
    await _force_failed(session, admission.attempt.id)
    await _release(session, checkout.id)

    cancelled = await CheckoutService(session).cancel_checkout(checkout.id)

    assert cancelled.status is CheckoutStatus.CANCELLED


async def test_a_paid_checkout_cannot_be_cancelled(session: AsyncSession, shop: Shop) -> None:
    checkout = await prepared(session, shop)
    admission = await PaymentAdmissionService(session).admit_payment(
        checkout.id, idempotency_key=KEY, at=NOW
    )
    assert admission.attempt is not None
    await _force_succeeded(session, admission.attempt.id)
    await session.execute(
        text("UPDATE checkout_session SET status = 'PAID', paid_at = now() WHERE id = :id"),
        {"id": checkout.id},
    )
    await session.commit()

    with pytest.raises(ConflictError) as refused:
        await CheckoutService(session).cancel_checkout(checkout.id)

    assert refused.value.reason == "checkout_already_paid"


async def test_a_malformed_idempotency_key_is_refused_before_anything_is_read(
    session: AsyncSession, shop: Shop
) -> None:
    checkout = await prepared(session, shop)

    with pytest.raises(ValueError, match="idempotency key"):
        await PaymentAdmissionService(session).admit_payment(
            checkout.id, idempotency_key="short", at=NOW
        )

    assert await attempt_count(session) == 0


def test_a_generated_key_is_a_valid_one() -> None:
    """A caller that supplies none still gets an identity that can be compared everywhere."""
    key = generate_idempotency_key()

    validate_idempotency_key(key)
    assert key.startswith("ar-")
    assert key != generate_idempotency_key()


async def test_the_locked_mandate_read_defeats_a_stale_identity_map(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession], shop: Shop
) -> None:
    """`populate_existing` on the mandate lock, proven by the mechanism rather than assumed.

    Payment admission reads the mandate under a lock and decides whether a payment may
    happen from what it reads. If the row were locked and then served out of the session's
    identity map, the decision would be made from the attributes the object was loaded with,
    which is the exact failure the lock exists to prevent.

    The interleaving is forced: this session loads the mandate and holds a stale object,
    another connection revokes it and commits, and then this session takes the lock. Without
    `populate_existing` the second read returns ACTIVE.
    """
    repository = MandateRepository(session)
    stale = await repository.get(shop.mandate.id)
    assert stale is not None
    assert stale.status is MandateStatus.ACTIVE
    await session.commit()

    async with factory() as revoker:
        withdrawn = await MandateRepository(revoker).get_for_update(shop.mandate.id)
        assert withdrawn is not None
        await MandateRepository(revoker).revoke(withdrawn)
        await revoker.commit()

    fresh = await repository.get_for_update(shop.mandate.id)

    assert fresh is not None
    # The same Python object, because the identity map is per session. Different attributes,
    # because the locking read repopulates it.
    assert fresh is stale
    assert fresh.status is MandateStatus.REVOKED


async def _force_succeeded(session: AsyncSession, attempt_id: uuid.UUID) -> None:
    """Settle an attempt without a provider, which does not exist yet."""
    await session.execute(
        text(
            "UPDATE payment_attempt SET status = 'IN_FLIGHT', dispatched_at = now() WHERE id = :id"
        ),
        {"id": attempt_id},
    )
    await session.execute(
        text(
            "UPDATE payment_attempt SET status = 'SUCCEEDED', resolved_at = now(),"
            " outcome_source = 'EXECUTION', provider_reference = 'forced' WHERE id = :id"
        ),
        {"id": attempt_id},
    )
    await session.commit()


async def _force_failed(session: AsyncSession, attempt_id: uuid.UUID) -> None:
    await session.execute(
        text(
            "UPDATE payment_attempt SET status = 'IN_FLIGHT', dispatched_at = now() WHERE id = :id"
        ),
        {"id": attempt_id},
    )
    await session.execute(
        text(
            "UPDATE payment_attempt SET status = 'FAILED', resolved_at = now(),"
            " outcome_source = 'EXECUTION', failure_code = 'CARD_DECLINED' WHERE id = :id"
        ),
        {"id": attempt_id},
    )
    await session.commit()


async def _release(session: AsyncSession, checkout_id: uuid.UUID) -> None:
    """Give back a committed hold, as a definitive decline will."""
    repository = InventoryReservationRepository(session)
    reservation = await repository.get_holding_for_checkout(checkout_id)
    assert reservation is not None
    await repository.release(reservation)
    await session.commit()
