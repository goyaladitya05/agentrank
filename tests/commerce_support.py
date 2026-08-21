"""Builders that produce real commerce state through the real services.

Not fixtures, because what the Phase 1I tests need varies per test and a fixture cannot be
called twice with different arguments in one test. Two merchants presenting the same
idempotency key is exactly that shape, and it is the shape most of these files are about.

Nothing here inserts a row directly. Every object comes out of the service that owns it, so a
test built on these is testing the application rather than a hand assembled database. That
costs a little speed and buys the only thing worth having: a fixture that stops being valid
when the code it imitates changes.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.checkout.execution import CheckoutExecutionService
from agentrank_api.checkout.service import CheckoutItem, CheckoutService, NewCheckout
from agentrank_api.commerce.repository import CatalogRepository, MerchantRepository
from agentrank_api.constraints.repository import IntentConstraintRepository
from agentrank_api.constraints.rules import ConstraintOperator, IntentConstraintSpec
from agentrank_api.mandates.repository import MandateRepository
from agentrank_api.payments.admission import PaymentAdmissionService
from agentrank_api.payments.models import PaymentAttempt

HOUR = timedelta(hours=1)
PRICE = 499900
CURRENCY = "INR"
BLACK = IntentConstraintSpec.required_attribute("color", ConstraintOperator.EQ, "black")


@dataclass(frozen=True, slots=True)
class Shop:
    """One merchant with everything a payment needs behind it."""

    merchant_id: uuid.UUID
    mandate_id: uuid.UUID
    variant_id: uuid.UUID
    price: int


async def build_shop(
    session: AsyncSession,
    slug: str,
    *,
    price: int = PRICE,
    inventory: int = 3,
    now: datetime | None = None,
) -> Shop:
    """A merchant, an active mandate, a matching constraint set and one purchasable variant.

    Committed before it returns, so a caller may open its own transactions afterwards without
    the setup being rolled back underneath them.
    """
    at = now or datetime.now(UTC)
    merchant = await MerchantRepository(session).create(slug=slug, name=slug)
    mandate = await MandateRepository(session).create(
        merchant_id=merchant.id,
        max_total_amount_minor=price,
        currency=CURRENCY,
        valid_from=at - HOUR,
        valid_until=at + HOUR,
    )
    await IntentConstraintRepository(session).create(
        merchant_id=merchant.id, mandate_id=mandate.id, specs=[BLACK]
    )
    catalog = CatalogRepository(session)
    product = await catalog.create_product(
        merchant_id=merchant.id, external_id=f"{slug}-1", title="Charger", category="chargers"
    )
    variant = await catalog.create_variant(
        product=product,
        sku=f"{slug}-black".upper(),
        price_amount_minor=price,
        currency=CURRENCY,
        inventory_quantity=inventory,
        attributes={"color": "black"},
    )
    await session.commit()
    return Shop(
        merchant_id=merchant.id,
        mandate_id=mandate.id,
        variant_id=variant.id,
        price=price,
    )


async def quote(
    session: AsyncSession, shop: Shop, *, quantity: int = 1, now: datetime | None = None
) -> uuid.UUID:
    """A priced, held quote for this shop, ready to be paid for.

    Creation and preparation together, because every caller here wants both and a quote with no
    hold is not something a payment may be admitted against.
    """
    at = now or datetime.now(UTC)
    checkout = await CheckoutService(session).create_checkout(
        NewCheckout(
            merchant_id=shop.merchant_id,
            mandate_id=shop.mandate_id,
            items=(CheckoutItem(variant_id=shop.variant_id, quantity=quantity),),
            expires_at=at + HOUR,
        )
    )
    readiness = await CheckoutExecutionService(session).prepare_execution(
        checkout.id, merchant_id=shop.merchant_id
    )
    assert readiness.ready
    return checkout.id


async def admit(
    session: AsyncSession, shop: Shop, checkout_id: uuid.UUID, *, key: str
) -> PaymentAttempt:
    """An ADMITTED payment attempt, with no provider involved.

    Admission alone, deliberately. `PaymentService.pay` admits and then dispatches, and a
    dispatch against the wired fake would settle the payment before an interactive checkout
    could exist.
    """
    admission = await PaymentAdmissionService(session).admit_payment(
        checkout_id, merchant_id=shop.merchant_id, idempotency_key=key
    )
    attempt = admission.attempt
    assert attempt is not None, admission.refusal
    return attempt
