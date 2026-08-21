"""Giving up on a payment nobody can resolve, and being honest about what that means.

The provider in this file can never prove durable absence. It answers, it has no record, and it
has no visibility guarantee that would ever make that absence final, which is a real shape for a
real processor. Reconciliation is therefore permanently correct and permanently useless: the
attempt stays UNKNOWN, the hold stays committed and the mandate stays occupied.

Abandonment is the way out and it is not a proof of anything. Every assertion here is about
that: the rows move exactly as a failure would, the trail says explicitly that no provider
confirmed it, and the residual risk is recorded rather than argued away.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.audit.models import ActorType
from agentrank_api.audit.repository import AuditRepository
from agentrank_api.checkout.execution import CheckoutExecutionService
from agentrank_api.checkout.models import CheckoutSession, CheckoutStatus
from agentrank_api.checkout.quote import QuotedLine
from agentrank_api.checkout.repository import CheckoutRepository
from agentrank_api.commerce.models import Variant
from agentrank_api.commerce.repository import CatalogRepository, MerchantRepository
from agentrank_api.constraints.repository import IntentConstraintRepository
from agentrank_api.constraints.rules import ConstraintOperator, IntentConstraintSpec
from agentrank_api.errors import ConflictError, NotFoundError
from agentrank_api.inventory.models import ReservationStatus
from agentrank_api.inventory.repository import InventoryReservationRepository
from agentrank_api.inventory.service import RESERVATION_RESOURCE
from agentrank_api.mandates.models import SpendingMandate
from agentrank_api.mandates.repository import MandateRepository
from agentrank_api.payments.admission import PAYMENT_RESOURCE, PaymentAdmissionService
from agentrank_api.payments.execution import PaymentExecutionService
from agentrank_api.payments.fake import FakeOutcome, FakePaymentProvider
from agentrank_api.payments.models import OutcomeSource, PaymentAttempt, PaymentAttemptStatus
from agentrank_api.payments.recovery import (
    OPERATOR_ABANDONED,
    AbandonmentReason,
    PaymentRecoveryService,
)
from agentrank_api.payments.repository import PaymentAttemptRepository

pytestmark = pytest.mark.anyio

NOW = datetime.now(UTC)
HOUR = timedelta(hours=1)
PRICE = 499900
STOCK = 5
KEY = "pay-ampere-0001"
OTHER_KEY = "pay-ampere-0002"
BLACK = IntentConstraintSpec.required_attribute("color", ConstraintOperator.EQ, "black")


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


async def admitted(session: AsyncSession, shop: Shop, *, key: str = KEY) -> PaymentAttempt:
    checkout = await prepared(session, shop)
    admission = await PaymentAdmissionService(session).admit_payment(
        checkout.id, idempotency_key=key, at=NOW
    )
    assert admission.attempt is not None
    return admission.attempt


@pytest.fixture
async def shop(session: AsyncSession) -> Shop:
    return await build_shop(session)


@pytest.fixture
def provider() -> FakePaymentProvider:
    """A provider that can never prove an identity was never executed.

    No visibility window, so every query answers ABSENT however far the clock is advanced.
    This is the shape abandonment exists for, and building the fixture this way means no test
    here can accidentally be resolved by the provider instead.
    """
    return FakePaymentProvider(default=FakeOutcome.AMBIGUOUS, clock=NOW)


async def unresolved(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider, *, key: str = KEY
) -> PaymentAttempt:
    """An attempt in UNKNOWN, reached the way one really is reached."""
    attempt = await admitted(session, shop, key=key)
    await PaymentExecutionService(session, provider).dispatch(attempt.id)
    resolved = await PaymentAttemptRepository(session).get(attempt.id)
    assert resolved is not None
    assert resolved.status is PaymentAttemptStatus.UNKNOWN
    return resolved


async def stock(session: AsyncSession, variant_id: uuid.UUID) -> int:
    return int(
        await session.scalar(select(Variant.inventory_quantity).where(Variant.id == variant_id))
        or 0
    )


async def events(session: AsyncSession, resource_type: str, resource_id: uuid.UUID) -> list[str]:
    return [
        event.event_type
        for event in await AuditRepository(session).list_for_resource(
            resource_type=resource_type, resource_id=resource_id
        )
    ]


async def test_abandoning_an_unresolvable_payment_frees_the_stock_and_the_mandate(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider
) -> None:
    """The recovery, end to end, for a provider that will never say anything useful.

    Reconciliation is run first and correctly changes nothing, because that is the state the
    operator is deciding about. Then the decision: the attempt becomes terminal, the hold goes
    back, the checkout stays OPEN and the total is exactly what it was. The mandate is free
    afterwards, which is the thing that was actually stuck.
    """
    attempt = await unresolved(session, shop, provider)
    attempt_id, reservation_id, checkout_id = (
        attempt.id,
        attempt.reservation_id,
        attempt.checkout_id,
    )
    before = await stock(session, shop.black)

    provider.clock = NOW + 100 * HOUR
    still_unknown = await PaymentExecutionService(session, provider).reconcile(attempt_id)
    assert still_unknown.attempt.status is PaymentAttemptStatus.UNKNOWN

    given_up = await PaymentRecoveryService(session).abandon_payment_attempt(
        attempt_id, reason=AbandonmentReason.PROVIDER_CANNOT_CONFIRM
    )

    assert given_up.changed
    # No provider was involved in this and the result says so, which is the honest headline.
    assert given_up.provider_called is False
    assert given_up.attempt.status is PaymentAttemptStatus.FAILED
    assert given_up.attempt.failure_code == OPERATOR_ABANDONED
    assert given_up.attempt.outcome_source is OutcomeSource.OPERATOR
    assert given_up.attempt.resolved_at is not None

    reservation = await InventoryReservationRepository(session).get(reservation_id)
    assert reservation is not None
    assert reservation.status is ReservationStatus.RELEASED
    # Released, not consumed. Nothing was sold, because nobody knows whether anything was paid.
    assert await stock(session, shop.black) == before
    checkout = await CheckoutRepository(session).get(checkout_id)
    assert checkout is not None
    assert checkout.status is CheckoutStatus.OPEN
    assert await PaymentAttemptRepository(session).get_open_for_mandate(shop.mandate.id) is None
    # The identity was dispatched once and never again by any part of the recovery.
    assert provider.executions_for(KEY) == 1
    assert provider.charges == 0


async def test_the_trail_says_an_operator_decided_and_no_provider_confirmed(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider
) -> None:
    """The whole point of a distinct event, asserted rather than assumed.

    A reader skimming a payment's history has to be able to see that this terminal state is a
    judgement and not a provider's report. `payment.abandoned` rather than `payment.failed`
    says it in the event name, and the payload says it again in words that survive being read
    out of context.
    """
    attempt = await unresolved(session, shop, provider)
    attempt_id, reservation_id = attempt.id, attempt.reservation_id

    await PaymentRecoveryService(session).abandon_payment_attempt(
        attempt_id, reason=AbandonmentReason.PROVIDER_UNREACHABLE
    )

    assert await events(session, PAYMENT_RESOURCE, attempt_id) == [
        "payment.admitted",
        "payment.unknown",
        "payment.abandoned",
    ]
    recorded = await AuditRepository(session).list_for_resource(
        resource_type=PAYMENT_RESOURCE, resource_id=attempt_id
    )
    abandoned = next(event for event in recorded if event.event_type == "payment.abandoned")
    assert abandoned.payload["reason"] == "provider_unreachable"
    assert abandoned.payload["provider_confirmed"] is False
    assert abandoned.payload["failure_code"] == OPERATOR_ABANDONED
    assert abandoned.payload["amount_minor"] == PRICE
    assert "may later reveal" in str(abandoned.payload["residual_risk"])
    # Not the provider. It confirmed nothing and attributing this to it would be a lie.
    assert abandoned.actor_type is ActorType.SYSTEM

    holds = await AuditRepository(session).list_for_resource(
        resource_type=RESERVATION_RESOURCE, resource_id=reservation_id
    )
    release = next(event for event in holds if event.event_type == "inventory.released")
    assert release.payload["reason"] == "payment_abandoned"


async def test_abandoning_twice_releases_once(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider
) -> None:
    """A tool that lost its answer and asked again has not made a second decision."""
    attempt = await unresolved(session, shop, provider)
    attempt_id, reservation_id = attempt.id, attempt.reservation_id
    before = await stock(session, shop.black)
    service = PaymentRecoveryService(session)

    first = await service.abandon_payment_attempt(
        attempt_id, reason=AbandonmentReason.OPERATOR_DECISION
    )
    second = await service.abandon_payment_attempt(
        attempt_id, reason=AbandonmentReason.OPERATOR_DECISION
    )

    assert first.changed is True
    assert second.changed is False
    assert second.attempt.status is PaymentAttemptStatus.FAILED
    assert first.attempt.resolved_at == second.attempt.resolved_at
    reservation = await InventoryReservationRepository(session).get(reservation_id)
    assert reservation is not None
    assert reservation.released_at is not None
    assert await stock(session, shop.black) == before
    # One decision, one release, one event.
    assert (await events(session, PAYMENT_RESOURCE, attempt_id)).count("payment.abandoned") == 1
    assert (await events(session, RESERVATION_RESOURCE, reservation_id)).count(
        "inventory.released"
    ) == 1


async def test_a_payment_that_was_never_dispatched_cannot_be_abandoned(
    session: AsyncSession, shop: Shop
) -> None:
    """ADMITTED has provably never been sent, so there is nothing to give up on."""
    attempt = await admitted(session, shop)

    with pytest.raises(ConflictError) as refused:
        await PaymentRecoveryService(session).abandon_payment_attempt(
            attempt.id, reason=AbandonmentReason.OPERATOR_DECISION
        )

    assert refused.value.reason == "payment_not_dispatched"


async def test_a_payment_nobody_has_queried_yet_cannot_be_abandoned(
    session: AsyncSession, shop: Shop
) -> None:
    """Giving up before asking is a guess, not a recovery.

    IN_FLIGHT means the provider may hold the answer and has never been asked for it. Requiring
    UNKNOWN means at least one query has happened before anybody decides to stop querying.
    """
    attempt = await admitted(session, shop)
    attempt_id = attempt.id
    await _force_in_flight(session, attempt_id)

    with pytest.raises(ConflictError) as refused:
        await PaymentRecoveryService(session).abandon_payment_attempt(
            attempt_id, reason=AbandonmentReason.PROVIDER_UNREACHABLE
        )

    assert refused.value.reason == "payment_not_reconciled"
    still = await PaymentAttemptRepository(session).get(attempt_id)
    assert still is not None
    assert still.status is PaymentAttemptStatus.IN_FLIGHT


async def test_a_succeeded_payment_cannot_be_abandoned(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider
) -> None:
    """The one refusal that matters most. A paid purchase is not a thing to give up on."""
    provider.default = FakeOutcome.SUCCESS
    attempt = await admitted(session, shop)
    attempt_id, reservation_id = attempt.id, attempt.reservation_id
    await PaymentExecutionService(session, provider).dispatch(attempt_id)

    with pytest.raises(ConflictError) as refused:
        await PaymentRecoveryService(session).abandon_payment_attempt(
            attempt_id, reason=AbandonmentReason.OPERATOR_DECISION
        )

    assert refused.value.reason == "payment_already_succeeded"
    settled = await PaymentAttemptRepository(session).get(attempt_id)
    assert settled is not None
    assert settled.status is PaymentAttemptStatus.SUCCEEDED
    consumed = await InventoryReservationRepository(session).get(reservation_id)
    assert consumed is not None
    # The sale stands. Nothing about a refused abandonment may touch the stock.
    assert consumed.status is ReservationStatus.CONSUMED


async def test_a_declined_payment_is_not_reported_as_abandoned(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider
) -> None:
    """A terminal state reached another way is a different fact, refused rather than relabelled."""
    provider.default = FakeOutcome.DECLINE
    attempt = await admitted(session, shop)
    attempt_id = attempt.id
    await PaymentExecutionService(session, provider).dispatch(attempt_id)

    with pytest.raises(ConflictError) as refused:
        await PaymentRecoveryService(session).abandon_payment_attempt(
            attempt_id, reason=AbandonmentReason.OPERATOR_DECISION
        )

    assert refused.value.reason == "payment_already_failed"
    declined = await PaymentAttemptRepository(session).get(attempt_id)
    assert declined is not None
    assert declined.failure_code == "CARD_DECLINED"


async def test_an_unknown_attempt_that_does_not_exist_is_a_structured_not_found(
    session: AsyncSession,
) -> None:
    with pytest.raises(NotFoundError):
        await PaymentRecoveryService(session).abandon_payment_attempt(
            uuid.uuid7(), reason=AbandonmentReason.OPERATOR_DECISION
        )


async def test_an_abandoned_checkout_can_be_paid_for_again(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider
) -> None:
    """Abandoning a payment says nothing about the quote.

    The residual risk is that the abandoned payment may yet turn out to have succeeded, and
    this test is what that risk looks like in practice: the buyer pays again, and if the first
    one did go through the merchant has two payments for one basket. That is the price of the
    decision and it is the reason abandonment is deliberate rather than automatic.
    """
    attempt = await unresolved(session, shop, provider)
    checkout_id = attempt.checkout_id
    before = await stock(session, shop.black)
    await PaymentRecoveryService(session).abandon_payment_attempt(
        attempt.id, reason=AbandonmentReason.PROVIDER_CANNOT_CONFIRM
    )

    readiness = await CheckoutExecutionService(session).prepare_execution(checkout_id, at=NOW)
    assert readiness.ready
    provider.set_outcome(OTHER_KEY, FakeOutcome.SUCCESS)
    retry = await PaymentAdmissionService(session).admit_payment(
        checkout_id, idempotency_key=OTHER_KEY, at=NOW
    )
    assert retry.admitted
    assert retry.attempt is not None
    paid = await PaymentExecutionService(session, provider).dispatch(retry.attempt.id)

    assert paid.attempt.status is PaymentAttemptStatus.SUCCEEDED
    assert await stock(session, shop.black) == before - 1
    # Two identities, two dispatches, and the abandoned one was never re-sent.
    assert provider.executions_for(KEY) == 1
    assert provider.executions_for(OTHER_KEY) == 1


def test_abandonment_is_not_reachable_over_http() -> None:
    """The recovery path must not be an unauthenticated endpoint, and this is how that is kept.

    Nothing authenticates a caller anywhere in this application yet. An HTTP route that
    terminalized a payment would let anybody who can reach the process release stock that a
    real charge may be standing behind, which is strictly worse than the state it recovers
    from. The check is on the whole generated schema rather than on a route module, because a
    route added anywhere would show up here.

    It reads the OpenAPI document rather than `app.routes`. This version of FastAPI keeps an
    included router as a single `_IncludedRouter` entry rather than flattening it into
    `APIRoute` objects, so scanning `app.routes` for them finds nothing and every negative
    assertion made that way passed for the wrong reason. Corrected in Phase 1G, with a
    positive assertion beside the negative ones so that an empty set can never satisfy this
    test again.
    """
    from agentrank_api.main import create_app

    paths = set(create_app().openapi()["paths"])

    assert not any("abandon" in path for path in paths)
    assert not any("recovery" in path for path in paths)
    # The check that stops a vacuous pass: the payment surface really is published here.
    assert "/api/v1/commerce/payments/{attempt_id}/reconcile" in paths


async def _force_in_flight(session: AsyncSession, attempt_id: uuid.UUID) -> None:
    """Leave an attempt where a crash between the dispatch commit and the wire would leave it."""
    await session.execute(
        text(
            "UPDATE payment_attempt SET status = 'IN_FLIGHT', dispatched_at = now() WHERE id = :id"
        ),
        {"id": attempt_id},
    )
    await session.commit()
    session.expire_all()
