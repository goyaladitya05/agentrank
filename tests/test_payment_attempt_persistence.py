"""Payment attempt invariants, asserted against the real schema.

These tests reach the database through the repository and the ORM, but what is under test is
the database. A `PaymentAttempt` is the row that decides whether a provider may be called and
for how much, so every rule protecting it has a test that tries to break it.

The two that matter most are here as several tests each. Money cannot drift: the amount and
the currency an attempt carries are the checkout's own total and currency, and the composite
foreign key refuses any other pair rather than trusting the code that wrote it. And the
lifecycle is a whitelist: every transition that exists is exercised, and a representative set
of the ones that do not are shown being refused, including the ones that would matter most if
they were possible.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.checkout.models import CheckoutSession, CheckoutStatus
from agentrank_api.checkout.quote import QuotedLine
from agentrank_api.checkout.repository import CheckoutRepository
from agentrank_api.commerce.models import Variant
from agentrank_api.commerce.repository import CatalogRepository, MerchantRepository
from agentrank_api.inventory.models import InventoryReservation, ReservationStatus
from agentrank_api.inventory.repository import InventoryReservationRepository
from agentrank_api.mandates.models import SpendingMandate
from agentrank_api.mandates.repository import MandateRepository
from agentrank_api.payments.models import OutcomeSource, PaymentAttempt, PaymentAttemptStatus
from agentrank_api.payments.repository import PaymentAttemptRepository

pytestmark = pytest.mark.anyio

NOW = datetime.now(UTC)
HOUR = timedelta(hours=1)
PRICE = 499900
KEY = "pay-ampere-0001"
REFERENCE = "fake_txn_0001"


@dataclass(frozen=True, slots=True)
class Payable:
    """A merchant, a quote, and stock already held for it: everything an attempt needs."""

    merchant_id: uuid.UUID
    mandate: SpendingMandate
    variant: Variant
    checkout: CheckoutSession
    reservation: InventoryReservation


async def build_payable(session: AsyncSession, slug: str, *, quantity: int = 1) -> Payable:
    merchant = await MerchantRepository(session).create(slug=slug, name=slug.title())
    mandate = await MandateRepository(session).create(
        merchant_id=merchant.id,
        max_total_amount_minor=PRICE * quantity,
        currency="INR",
        valid_from=NOW - HOUR,
        valid_until=NOW + HOUR,
    )
    catalog = CatalogRepository(session)
    product = await catalog.create_product(
        merchant_id=merchant.id, external_id=f"{slug}-1", title="Charger"
    )
    variant = await catalog.create_variant(
        product=product,
        sku=f"{slug}-sku",
        price_amount_minor=PRICE,
        currency="INR",
        inventory_quantity=10,
    )
    checkout = await CheckoutRepository(session).create(
        merchant_id=merchant.id,
        mandate_id=mandate.id,
        currency="INR",
        lines=[QuotedLine(variant_id=variant.id, quantity=quantity, unit_price_amount_minor=PRICE)],
        expires_at=NOW + HOUR,
    )
    reservation = await InventoryReservationRepository(session).create(
        merchant_id=merchant.id,
        checkout_id=checkout.id,
        expires_at=NOW + HOUR,
        quantities={variant.id: quantity},
    )
    await session.commit()
    return Payable(
        merchant_id=merchant.id,
        mandate=mandate,
        variant=variant,
        checkout=checkout,
        reservation=reservation,
    )


@pytest.fixture
async def payable(session: AsyncSession) -> Payable:
    return await build_payable(session, "ampere-supply")


async def admit(session: AsyncSession, payable: Payable, **overrides: object) -> PaymentAttempt:
    fields: dict[str, object] = {
        "merchant_id": payable.merchant_id,
        "checkout_id": payable.checkout.id,
        "mandate_id": payable.mandate.id,
        "reservation_id": payable.reservation.id,
        "idempotency_key": KEY,
        "amount_minor": payable.checkout.total_amount_minor,
        "currency": payable.checkout.currency,
    }
    return await PaymentAttemptRepository(session).create(**(fields | overrides))  # type: ignore[arg-type]


async def commit_reservation(session: AsyncSession, payable: Payable) -> None:
    """Put the hold into the state an admitted payment leaves it in."""
    repository = InventoryReservationRepository(session)
    reservation = await repository.get(payable.reservation.id)
    assert reservation is not None
    await repository.commit_to_payment(reservation)
    await session.flush()


async def test_an_attempt_persists_as_admitted(session: AsyncSession, payable: Payable) -> None:
    created = await admit(session, payable)
    await session.commit()
    session.expunge_all()

    found = await PaymentAttemptRepository(session).get(created.id)
    assert found is not None
    assert found.merchant_id == payable.merchant_id
    assert found.checkout_id == payable.checkout.id
    assert found.mandate_id == payable.mandate.id
    assert found.reservation_id == payable.reservation.id
    assert found.idempotency_key == KEY
    assert found.status is PaymentAttemptStatus.ADMITTED
    # The whole point of ADMITTED: no provider request has begun, and nothing pretends one
    # has.
    assert found.dispatched_at is None
    assert found.resolved_at is None
    assert found.outcome_source is None
    assert found.provider_reference is None
    assert found.failure_code is None
    assert found.is_open


async def test_the_amount_and_currency_are_frozen_from_the_checkout(
    session: AsyncSession, payable: Payable
) -> None:
    """What was authorized and what will be sent to a provider are the same two columns."""
    created = await admit(session, payable)
    await session.commit()

    assert created.amount_minor == payable.checkout.total_amount_minor
    assert created.currency == payable.checkout.currency


async def test_an_amount_that_is_not_the_checkout_total_is_refused(
    session: AsyncSession, payable: Payable
) -> None:
    """Freezing is structural. A caller cannot charge one rupee less than was quoted.

    Nothing in the application would write this. The composite foreign key is what makes it
    impossible rather than merely absent, which is the difference between a frozen amount and
    a copied one.
    """
    with pytest.raises(IntegrityError):
        await admit(session, payable, amount_minor=payable.checkout.total_amount_minor - 1)


async def test_a_currency_that_is_not_the_checkout_currency_is_refused(
    session: AsyncSession, payable: Payable
) -> None:
    with pytest.raises(IntegrityError):
        await admit(session, payable, currency="EUR")


async def test_an_attempt_cannot_name_another_merchants_checkout(session: AsyncSession) -> None:
    ampere = await build_payable(session, "ampere-supply")
    volt = await build_payable(session, "volt-supply")

    with pytest.raises(IntegrityError):
        await admit(session, ampere, merchant_id=volt.merchant_id)


async def test_an_attempt_cannot_claim_a_mandate_the_checkout_was_not_quoted_against(
    session: AsyncSession, payable: Payable
) -> None:
    other = await MandateRepository(session).create(
        merchant_id=payable.merchant_id,
        max_total_amount_minor=PRICE,
        currency="INR",
        valid_from=NOW - HOUR,
        valid_until=NOW + HOUR,
    )
    await session.commit()

    with pytest.raises(IntegrityError):
        await admit(session, payable, mandate_id=other.id)


async def test_an_attempt_cannot_be_bound_to_another_checkouts_reservation(
    session: AsyncSession,
) -> None:
    ampere = await build_payable(session, "ampere-supply")
    other = await build_payable(session, "ampere-two")

    with pytest.raises(IntegrityError):
        await admit(session, ampere, reservation_id=other.reservation.id)


async def test_one_idempotency_key_admits_one_attempt_per_checkout(
    session: AsyncSession, payable: Payable
) -> None:
    """One logical payment operation exists once, whatever a caller repeats."""
    await admit(session, payable)
    await session.commit()

    with pytest.raises(IntegrityError):
        await admit(session, payable)
        await session.flush()


async def test_a_blank_idempotency_key_is_refused(session: AsyncSession, payable: Payable) -> None:
    with pytest.raises(IntegrityError):
        await admit(session, payable, idempotency_key="   ")


async def test_a_short_idempotency_key_is_refused(session: AsyncSession, payable: Payable) -> None:
    """A key short enough to collide by accident is not an identity."""
    with pytest.raises(IntegrityError):
        await admit(session, payable, idempotency_key="abc")


@pytest.mark.parametrize(
    "assignment",
    [
        "merchant_id = gen_random_uuid()",
        "checkout_id = gen_random_uuid()",
        "mandate_id = gen_random_uuid()",
        "reservation_id = gen_random_uuid()",
        "amount_minor = 1",
        "currency = 'EUR'",
        "idempotency_key = 'rewritten-key-01'",
        "created_at = now()",
    ],
)
async def test_ownership_money_and_identity_are_immutable(
    session: AsyncSession, payable: Payable, assignment: str
) -> None:
    """What an attempt is for and what it costs cannot be edited after it was authorized.

    Written as raw SQL because the repository offers no way to try. The database is the layer
    that cannot be bypassed, and it is the one being asserted here.
    """
    created = await admit(session, payable)
    await session.commit()

    with pytest.raises(DBAPIError, match="ownership, money and identity are immutable"):
        await session.execute(
            # The assignment is one of the literals in the parameter list above, never
            # anything a caller supplied.
            text(f"UPDATE payment_attempt SET {assignment} WHERE id = :id"),  # noqa: S608
            {"id": created.id},
        )
    await session.rollback()


async def test_the_lifecycle_runs_admitted_to_in_flight_to_succeeded(
    session: AsyncSession, payable: Payable
) -> None:
    repository = PaymentAttemptRepository(session)
    created = await admit(session, payable)
    await commit_reservation(session, payable)

    assert await repository.mark_in_flight(created) is True
    assert created.status is PaymentAttemptStatus.IN_FLIGHT
    assert created.dispatched_at is not None
    assert created.resolved_at is None

    assert (
        await repository.mark_succeeded(
            created, provider_reference=REFERENCE, source=OutcomeSource.EXECUTION
        )
        is True
    )
    await session.commit()
    session.expunge_all()

    settled = await repository.get(created.id)
    assert settled is not None
    assert settled.status is PaymentAttemptStatus.SUCCEEDED
    assert settled.provider_reference == REFERENCE
    assert settled.outcome_source is OutcomeSource.EXECUTION
    assert settled.resolved_at is not None
    assert settled.is_terminal


async def test_a_definitive_failure_carries_a_code_and_resolves(
    session: AsyncSession, payable: Payable
) -> None:
    repository = PaymentAttemptRepository(session)
    created = await admit(session, payable)
    await commit_reservation(session, payable)
    await repository.mark_in_flight(created)

    assert (
        await repository.mark_failed(
            created, failure_code="CARD_DECLINED", source=OutcomeSource.EXECUTION
        )
        is True
    )
    await session.commit()

    assert created.status is PaymentAttemptStatus.FAILED
    assert created.failure_code == "CARD_DECLINED"
    assert created.resolved_at is not None
    assert created.is_terminal


async def test_an_ambiguous_outcome_is_unknown_and_is_not_resolved(
    session: AsyncSession, payable: Payable
) -> None:
    """UNKNOWN is not FAILED and it is not a resolution. Nothing stamps it as one."""
    repository = PaymentAttemptRepository(session)
    created = await admit(session, payable)
    await commit_reservation(session, payable)
    await repository.mark_in_flight(created)

    assert await repository.mark_unknown(created, source=OutcomeSource.EXECUTION) is True
    await session.commit()

    assert created.status is PaymentAttemptStatus.UNKNOWN
    assert created.resolved_at is None
    assert created.failure_code is None
    assert created.is_open
    assert not created.is_terminal


async def test_an_unknown_attempt_can_be_resolved_by_reconciliation(
    session: AsyncSession, payable: Payable
) -> None:
    repository = PaymentAttemptRepository(session)
    created = await admit(session, payable)
    await commit_reservation(session, payable)
    await repository.mark_in_flight(created)
    await repository.mark_unknown(created, source=OutcomeSource.EXECUTION)

    assert (
        await repository.mark_succeeded(
            created, provider_reference=REFERENCE, source=OutcomeSource.RECONCILIATION
        )
        is True
    )
    await session.commit()

    assert created.status is PaymentAttemptStatus.SUCCEEDED
    assert created.outcome_source is OutcomeSource.RECONCILIATION


async def test_recording_the_same_outcome_twice_changes_nothing(
    session: AsyncSession, payable: Payable
) -> None:
    """Reconciliation runs more than once by design, so a repeat has to be a no operation."""
    repository = PaymentAttemptRepository(session)
    created = await admit(session, payable)
    await commit_reservation(session, payable)
    await repository.mark_in_flight(created)
    await repository.mark_succeeded(
        created, provider_reference=REFERENCE, source=OutcomeSource.EXECUTION
    )
    await session.commit()
    stamped = created.resolved_at

    assert (
        await repository.mark_succeeded(
            created, provider_reference="different", source=OutcomeSource.RECONCILIATION
        )
        is False
    )
    await session.commit()

    assert created.resolved_at == stamped
    assert created.provider_reference == REFERENCE
    assert created.outcome_source is OutcomeSource.EXECUTION


async def test_an_admitted_attempt_cannot_receive_an_outcome(
    session: AsyncSession, payable: Payable
) -> None:
    """ADMITTED means provably never dispatched, so no provider can have answered it."""
    repository = PaymentAttemptRepository(session)
    created = await admit(session, payable)

    with pytest.raises(ValueError, match="cannot succeed"):
        await repository.mark_succeeded(
            created, provider_reference=REFERENCE, source=OutcomeSource.EXECUTION
        )


async def test_a_dispatched_attempt_cannot_be_dispatched_again(
    session: AsyncSession, payable: Payable
) -> None:
    repository = PaymentAttemptRepository(session)
    created = await admit(session, payable)
    await commit_reservation(session, payable)
    await repository.mark_in_flight(created)

    with pytest.raises(ValueError, match="cannot be dispatched"):
        await repository.mark_in_flight(created)


@pytest.mark.parametrize(
    ("start", "target"),
    [
        # The transition that would charge somebody twice: a settled payment reopened and
        # sent again.
        (PaymentAttemptStatus.SUCCEEDED, PaymentAttemptStatus.ADMITTED),
        (PaymentAttemptStatus.SUCCEEDED, PaymentAttemptStatus.IN_FLIGHT),
        # The transition that would take money back without a refund existing.
        (PaymentAttemptStatus.SUCCEEDED, PaymentAttemptStatus.FAILED),
        (PaymentAttemptStatus.FAILED, PaymentAttemptStatus.SUCCEEDED),
        (PaymentAttemptStatus.FAILED, PaymentAttemptStatus.IN_FLIGHT),
        # The transition that would turn "we do not know" into a certainty nobody established.
        (PaymentAttemptStatus.UNKNOWN, PaymentAttemptStatus.ADMITTED),
        (PaymentAttemptStatus.UNKNOWN, PaymentAttemptStatus.IN_FLIGHT),
        (PaymentAttemptStatus.ADMITTED, PaymentAttemptStatus.SUCCEEDED),
        (PaymentAttemptStatus.ADMITTED, PaymentAttemptStatus.UNKNOWN),
    ],
)
async def test_the_database_refuses_every_transition_that_is_not_on_the_whitelist(
    session: AsyncSession,
    payable: Payable,
    start: PaymentAttemptStatus,
    target: PaymentAttemptStatus,
) -> None:
    """The guard is a whitelist, so a status nobody thought about is refused by default.

    Written as raw SQL on purpose. The repository refuses most of these before the database
    sees them, and what is under test here is the layer that cannot be bypassed.
    """
    created = await admit(session, payable)
    await commit_reservation(session, payable)
    await session.commit()
    await _force_status(session, created.id, start)

    with pytest.raises(DBAPIError, match=r"cannot be changed|cannot go from"):
        await session.execute(
            text("UPDATE payment_attempt SET status = :status WHERE id = :id"),
            {"status": target.value, "id": created.id},
        )
    await session.rollback()


async def test_a_settled_attempt_refuses_an_update_that_changes_nothing_else(
    session: AsyncSession, payable: Payable
) -> None:
    """Terminal means no update at all, not merely no status change.

    A guard that only compared statuses would let a settled payment have its provider
    reference rewritten, which is the audit trail of the money moving.
    """
    created = await admit(session, payable)
    await commit_reservation(session, payable)
    await session.commit()
    await _force_status(session, created.id, PaymentAttemptStatus.SUCCEEDED)

    with pytest.raises(DBAPIError, match="cannot be changed"):
        await session.execute(
            text("UPDATE payment_attempt SET provider_reference = 'rewritten' WHERE id = :id"),
            {"id": created.id},
        )
    await session.rollback()


async def test_a_dispatch_time_cannot_be_moved(session: AsyncSession, payable: Payable) -> None:
    repository = PaymentAttemptRepository(session)
    created = await admit(session, payable)
    await commit_reservation(session, payable)
    await repository.mark_in_flight(created)
    await session.commit()

    with pytest.raises(DBAPIError, match="dispatch time cannot be moved"):
        await session.execute(
            text("UPDATE payment_attempt SET dispatched_at = now() WHERE id = :id"),
            {"id": created.id},
        )
    await session.rollback()


async def test_one_mandate_admits_one_successful_payment(session: AsyncSession) -> None:
    """The single purchase rule, structural rather than intended.

    Two checkouts under one mandate, both paid. The second insert is refused by the partial
    unique index, and no lock and no application check is involved in this test at all.
    """
    payable = await build_payable(session, "ampere-supply")
    second = await CheckoutRepository(session).create(
        merchant_id=payable.merchant_id,
        mandate_id=payable.mandate.id,
        currency="INR",
        lines=[
            QuotedLine(variant_id=payable.variant.id, quantity=1, unit_price_amount_minor=PRICE)
        ],
        expires_at=NOW + HOUR,
    )
    second_reservation = await InventoryReservationRepository(session).create(
        merchant_id=payable.merchant_id,
        checkout_id=second.id,
        expires_at=NOW + HOUR,
        quantities={payable.variant.id: 1},
    )
    first = await admit(session, payable)
    await session.commit()
    await _force_status(session, first.id, PaymentAttemptStatus.SUCCEEDED)
    await session.commit()

    rival = await admit(
        session,
        payable,
        checkout_id=second.id,
        reservation_id=second_reservation.id,
        idempotency_key="pay-ampere-0002",
    )
    await session.commit()

    with pytest.raises(IntegrityError):
        await _force_status(session, rival.id, PaymentAttemptStatus.SUCCEEDED)


async def test_one_mandate_admits_one_payment_at_a_time(session: AsyncSession) -> None:
    """A second candidate checkout under one mandate cannot reach a provider beside the
    first.

    This is what makes two concurrent successes unreachable rather than something that has to
    be caught after both providers have already been called.
    """
    payable = await build_payable(session, "ampere-supply")
    second = await CheckoutRepository(session).create(
        merchant_id=payable.merchant_id,
        mandate_id=payable.mandate.id,
        currency="INR",
        lines=[
            QuotedLine(variant_id=payable.variant.id, quantity=1, unit_price_amount_minor=PRICE)
        ],
        expires_at=NOW + HOUR,
    )
    second_reservation = await InventoryReservationRepository(session).create(
        merchant_id=payable.merchant_id,
        checkout_id=second.id,
        expires_at=NOW + HOUR,
        quantities={payable.variant.id: 1},
    )
    await admit(session, payable)
    await session.commit()

    with pytest.raises(IntegrityError):
        await admit(
            session,
            payable,
            checkout_id=second.id,
            reservation_id=second_reservation.id,
            idempotency_key="pay-ampere-0002",
        )
        await session.flush()


async def test_a_terminal_attempt_leaves_room_for_a_later_one(session: AsyncSession) -> None:
    """A declined payment consumed nothing, so the mandate may authorize another try."""
    payable = await build_payable(session, "ampere-supply")
    first = await admit(session, payable)
    await session.commit()
    await _force_status(session, first.id, PaymentAttemptStatus.FAILED)
    await session.commit()

    again = await admit(session, payable, idempotency_key="pay-ampere-0002")
    await session.commit()

    assert again.status is PaymentAttemptStatus.ADMITTED


async def test_the_lookups_find_an_attempt_by_every_identity_that_matters(
    session: AsyncSession, payable: Payable
) -> None:
    repository = PaymentAttemptRepository(session)
    created = await admit(session, payable)
    await session.commit()

    by_identity = await repository.get_by_identity(
        checkout_id=payable.checkout.id, idempotency_key=KEY
    )
    assert by_identity is not None
    assert by_identity.id == created.id

    assert (
        await repository.get_by_identity(
            checkout_id=payable.checkout.id, idempotency_key="pay-ampere-0002"
        )
        is None
    )

    open_attempt = await repository.get_open_for_mandate(payable.mandate.id)
    assert open_attempt is not None
    assert open_attempt.id == created.id

    assert await repository.get_succeeded_for_mandate(payable.mandate.id) is None
    assert await repository.get_succeeded_for_checkout(payable.checkout.id) is None
    assert [attempt.id for attempt in await repository.list_for_checkout(payable.checkout.id)] == [
        created.id
    ]


async def test_a_checkout_can_be_paid_and_paid_is_terminal(
    session: AsyncSession, payable: Payable
) -> None:
    """OPEN becomes PAID, and nothing follows it."""
    await session.execute(
        text("UPDATE checkout_session SET status = 'PAID', paid_at = now() WHERE id = :id"),
        {"id": payable.checkout.id},
    )
    await session.commit()
    session.expunge_all()

    found = await CheckoutRepository(session).get(
        payable.checkout.id, merchant_id=payable.checkout.merchant_id
    )
    assert found is not None
    assert found.status is CheckoutStatus.PAID
    assert found.paid_at is not None

    for target in ("OPEN", "CANCELLED"):
        with pytest.raises(DBAPIError, match="cannot be changed"):
            await session.execute(
                text("UPDATE checkout_session SET status = :status WHERE id = :id"),
                {"status": target, "id": payable.checkout.id},
            )
        await session.rollback()


async def test_a_cancelled_checkout_cannot_become_paid(
    session: AsyncSession, payable: Payable
) -> None:
    checkout = await CheckoutRepository(session).get_for_update(
        payable.checkout.id, merchant_id=payable.checkout.merchant_id
    )
    assert checkout is not None
    await CheckoutRepository(session).cancel(checkout)
    await session.commit()

    with pytest.raises(DBAPIError, match="cannot be changed"):
        await session.execute(
            text("UPDATE checkout_session SET status = 'PAID', paid_at = now() WHERE id = :id"),
            {"id": payable.checkout.id},
        )


async def test_a_reservation_runs_active_to_committed_to_consumed(
    session: AsyncSession, payable: Payable
) -> None:
    repository = InventoryReservationRepository(session)
    reservation = await repository.get(payable.reservation.id)
    assert reservation is not None

    assert await repository.commit_to_payment(reservation) is True
    assert reservation.status is ReservationStatus.COMMITTED
    # Commitment stamps nothing. The instant is the payment attempt's own creation time.
    assert reservation.released_at is None
    assert reservation.consumed_at is None

    assert await repository.consume(reservation) is True
    await session.commit()
    session.expunge_all()

    sold = await repository.get(payable.reservation.id)
    assert sold is not None
    assert sold.status is ReservationStatus.CONSUMED
    assert sold.consumed_at is not None


async def test_a_committed_reservation_can_be_released_after_a_decline(
    session: AsyncSession, payable: Payable
) -> None:
    repository = InventoryReservationRepository(session)
    reservation = await repository.get(payable.reservation.id)
    assert reservation is not None
    await repository.commit_to_payment(reservation)

    assert await repository.release(reservation) is True
    await session.commit()

    assert reservation.status is ReservationStatus.RELEASED
    assert reservation.released_at is not None


@pytest.mark.parametrize(
    ("start", "target"),
    [
        ("CONSUMED", "ACTIVE"),
        ("CONSUMED", "COMMITTED"),
        ("CONSUMED", "RELEASED"),
        ("RELEASED", "ACTIVE"),
        ("RELEASED", "COMMITTED"),
        ("RELEASED", "CONSUMED"),
        # A hold that was never bound to a payment cannot be sold.
        ("ACTIVE", "CONSUMED"),
        # A commitment cannot be quietly undone back into an expiry governed hold.
        ("COMMITTED", "ACTIVE"),
    ],
)
async def test_the_database_refuses_every_reservation_transition_off_the_whitelist(
    session: AsyncSession, payable: Payable, start: str, target: str
) -> None:
    await _force_reservation_status(session, payable.reservation.id, start)

    with pytest.raises(DBAPIError, match=r"cannot be changed|cannot go from"):
        await session.execute(
            text("UPDATE inventory_reservation SET status = :status WHERE id = :id"),
            {"status": target, "id": payable.reservation.id},
        )
    await session.rollback()


async def test_a_committed_reservation_still_holds_stock_after_its_expiry(
    session: AsyncSession, payable: Payable
) -> None:
    """Expiry stops governing a hold the moment a payment is admitted against it.

    Admission was the authorization instant. A provider operation that outlives the original
    quote is still the operation that was authorized, so the stock behind it stays held.
    """
    repository = InventoryReservationRepository(session)
    reservation = await repository.get(payable.reservation.id)
    assert reservation is not None
    await repository.commit_to_payment(reservation)
    await session.commit()

    long_after = NOW + timedelta(days=7)
    held = await repository.effective_reserved_quantities(
        variant_ids=[payable.variant.id], at=long_after
    )
    assert held == {payable.variant.id: 1}


async def test_a_consumed_reservation_stops_counting_as_a_hold(
    session: AsyncSession, payable: Payable
) -> None:
    """The whole answer to double subtraction.

    Once the units have come out of the variant total, counting them again as reserved would
    remove one purchase twice.
    """
    repository = InventoryReservationRepository(session)
    reservation = await repository.get(payable.reservation.id)
    assert reservation is not None
    await repository.commit_to_payment(reservation)
    await repository.consume(reservation)
    await session.commit()

    held = await repository.effective_reserved_quantities(variant_ids=[payable.variant.id], at=NOW)
    assert held == {}


async def test_consuming_stock_decrements_the_variant_exactly_once(
    session: AsyncSession, payable: Payable
) -> None:
    repository = InventoryReservationRepository(session)
    before = await session.scalar(
        select(Variant.inventory_quantity).where(Variant.id == payable.variant.id)
    )
    assert before == 10

    shortfalls = await repository.consume_stock(
        merchant_id=payable.merchant_id, quantities={payable.variant.id: 1}
    )
    await session.commit()

    assert shortfalls == {}
    after = await session.scalar(
        select(Variant.inventory_quantity).where(Variant.id == payable.variant.id)
    )
    assert after == 9


async def test_consuming_more_stock_than_exists_clamps_and_reports_the_shortfall(
    session: AsyncSession, payable: Payable
) -> None:
    """Unreachable today, and explicit rather than hidden.

    Nothing in this application writes `variant.inventory_quantity`, so a total below what is
    committed cannot happen yet. The day a merchant inventory endpoint exists it can, and the
    honest answer is that the money moved and the merchant is oversold. The total is never
    negative and the difference is reported for the caller to record.
    """
    repository = InventoryReservationRepository(session)
    await session.execute(
        text("UPDATE variant SET inventory_quantity = 0 WHERE id = :id"),
        {"id": payable.variant.id},
    )

    shortfalls = await repository.consume_stock(
        merchant_id=payable.merchant_id, quantities={payable.variant.id: 1}
    )
    await session.commit()

    assert shortfalls == {payable.variant.id: 1}
    after = await session.scalar(
        select(Variant.inventory_quantity).where(Variant.id == payable.variant.id)
    )
    assert after == 0


async def test_the_audit_trail_accepts_a_payment_provider_actor(session: AsyncSession) -> None:
    """A new actor exists because a payment provider now does. The check constraint knows."""
    from agentrank_api.audit.models import ActorType
    from agentrank_api.audit.repository import AuditRepository

    merchant = await MerchantRepository(session).create(slug="ampere-supply", name="Ampere")
    event = await AuditRepository(session).append(
        merchant_id=merchant.id,
        actor_type=ActorType.PAYMENT_PROVIDER,
        event_type="payment.succeeded",
        resource_type="payment_attempt",
        resource_id=uuid.uuid7(),
        payload={"status": "SUCCEEDED"},
    )
    await session.commit()

    assert event.actor_type is ActorType.PAYMENT_PROVIDER


async def _force_status(
    session: AsyncSession, attempt_id: uuid.UUID, status: PaymentAttemptStatus
) -> None:
    """Put an attempt into a state without walking there through the service.

    Every intermediate transition is written out, because the guard would refuse a jump, and
    the point of these tests is the state at the end rather than the route to it.
    """
    if status is PaymentAttemptStatus.ADMITTED:
        return
    await session.execute(
        text(
            "UPDATE payment_attempt SET status = 'IN_FLIGHT', dispatched_at = now()"
            " WHERE id = :id AND status = 'ADMITTED'"
        ),
        {"id": attempt_id},
    )
    if status is PaymentAttemptStatus.IN_FLIGHT:
        return
    if status is PaymentAttemptStatus.UNKNOWN:
        await session.execute(
            text(
                "UPDATE payment_attempt SET status = 'UNKNOWN', outcome_source = 'EXECUTION'"
                " WHERE id = :id"
            ),
            {"id": attempt_id},
        )
        return
    if status is PaymentAttemptStatus.SUCCEEDED:
        await session.execute(
            text(
                "UPDATE payment_attempt SET status = 'SUCCEEDED', resolved_at = now(),"
                " outcome_source = 'EXECUTION', provider_reference = :reference WHERE id = :id"
            ),
            {"id": attempt_id, "reference": REFERENCE},
        )
        return
    await session.execute(
        text(
            "UPDATE payment_attempt SET status = 'FAILED', resolved_at = now(),"
            " outcome_source = 'EXECUTION', failure_code = 'CARD_DECLINED' WHERE id = :id"
        ),
        {"id": attempt_id},
    )


async def _force_reservation_status(
    session: AsyncSession, reservation_id: uuid.UUID, status: str
) -> None:
    """Walk a reservation to a state through the transitions the guard permits."""
    if status == "ACTIVE":
        return
    if status in {"COMMITTED", "CONSUMED"}:
        await session.execute(
            text("UPDATE inventory_reservation SET status = 'COMMITTED' WHERE id = :id"),
            {"id": reservation_id},
        )
    if status == "CONSUMED":
        await session.execute(
            text(
                "UPDATE inventory_reservation SET status = 'CONSUMED', consumed_at = now()"
                " WHERE id = :id"
            ),
            {"id": reservation_id},
        )
    if status == "RELEASED":
        await session.execute(
            text(
                "UPDATE inventory_reservation SET status = 'RELEASED', released_at = now()"
                " WHERE id = :id"
            ),
            {"id": reservation_id},
        )
