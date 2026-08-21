"""Preparing a checkout for an execution that does not exist yet.

The rule under test is one sentence: a checkout becomes execution ready only when the money
is authorized, the purchase is authorized, the quote is still usable and the stock is held.
Every test here is a way of failing one of those four and finding that no stock was taken.

The count that matters in almost every case is zero reservation rows. A checkout that may
not proceed must never hold a merchant's inventory, and counting rows is how a test says
that without trusting a return value.
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from agentrank_api.audit.repository import AuditRepository
from agentrank_api.checkout.authorization import CheckoutAuthorizationViolation
from agentrank_api.checkout.execution import CheckoutExecutionService
from agentrank_api.checkout.execution_authorization import ExecutionAuthorizationViolation
from agentrank_api.checkout.intent_authorization import IntentViolationCode
from agentrank_api.checkout.models import CheckoutSession
from agentrank_api.checkout.quote import QuotedLine
from agentrank_api.checkout.repository import CheckoutRepository
from agentrank_api.checkout.service import CheckoutService
from agentrank_api.commerce.repository import CatalogRepository, MerchantRepository
from agentrank_api.constraints.repository import IntentConstraintRepository
from agentrank_api.constraints.rules import ConstraintOperator, IntentConstraintSpec
from agentrank_api.database import create_session_factory
from agentrank_api.errors import NotFoundError
from agentrank_api.inventory.models import InventoryReservation
from agentrank_api.inventory.service import (
    RESERVATION_RESOURCE,
    InventoryViolationCode,
)
from agentrank_api.mandates.models import SpendingMandate
from agentrank_api.mandates.repository import MandateRepository

pytestmark = pytest.mark.anyio

NOW = datetime.now(UTC)
HOUR = timedelta(hours=1)
PRICE = 499900
BLACK = IntentConstraintSpec.required_attribute("color", ConstraintOperator.EQ, "black")


@dataclass(frozen=True, slots=True)
class Shop:
    """A merchant whose mandate and constraints both permit one black charger."""

    merchant_id: uuid.UUID
    mandate: SpendingMandate
    black: uuid.UUID
    blue: uuid.UUID


async def build_shop(
    session: AsyncSession,
    *,
    stock: int = 1,
    ceiling: int = PRICE,
    valid_until: datetime | None = None,
    constrained: bool = True,
) -> Shop:
    merchant = await MerchantRepository(session).create(slug="ampere-supply", name="Ampere")
    mandate = await MandateRepository(session).create(
        merchant_id=merchant.id,
        max_total_amount_minor=ceiling,
        currency="INR",
        valid_from=NOW - HOUR,
        valid_until=valid_until or NOW + HOUR,
    )
    if constrained:
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
        inventory_quantity=stock,
        attributes={"color": "black"},
    )
    blue = await catalog.create_variant(
        product=product,
        sku="AMP-BLUE",
        price_amount_minor=PRICE,
        currency="INR",
        inventory_quantity=stock,
        attributes={"color": "blue"},
    )
    await session.commit()
    return Shop(merchant_id=merchant.id, mandate=mandate, black=black.id, blue=blue.id)


@pytest.fixture
async def shop(session: AsyncSession) -> Shop:
    return await build_shop(session)


@pytest.fixture
async def committed(catalog_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A second session, for reading what actually reached the database."""
    factory = create_session_factory(catalog_engine)
    async with factory() as other:
        yield other


async def quote(
    session: AsyncSession,
    shop: Shop,
    *,
    variant_id: uuid.UUID | None = None,
    quantity: int = 1,
    expires_at: datetime | None = None,
) -> CheckoutSession:
    checkout = await CheckoutRepository(session).create(
        merchant_id=shop.merchant_id,
        mandate_id=shop.mandate.id,
        currency="INR",
        lines=[
            QuotedLine(
                variant_id=variant_id or shop.black,
                quantity=quantity,
                unit_price_amount_minor=PRICE,
                product_category="chargers",
                variant_attributes={"color": "blue" if variant_id == shop.blue else "black"},
            )
        ],
        expires_at=expires_at or NOW + HOUR,
    )
    await session.commit()
    return checkout


async def reservation_count(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(InventoryReservation)) or 0)


async def test_a_checkout_both_gates_allow_becomes_execution_ready(
    session: AsyncSession, committed: AsyncSession, shop: Shop
) -> None:
    """The only case that may hold stock, and the only one that reaches the database."""
    checkout = await quote(session, shop)

    readiness = await CheckoutExecutionService(session).prepare_execution(
        checkout.id, merchant_id=checkout.merchant_id, at=NOW
    )

    assert readiness.ready
    assert readiness.authorization.authorized
    assert readiness.inventory_violations == ()
    assert readiness.reservation is not None
    assert readiness.reservation.checkout_id == checkout.id
    assert await reservation_count(committed) == 1

    events = await AuditRepository(committed).list_for_resource(
        resource_type=RESERVATION_RESOURCE, resource_id=readiness.reservation.id
    )
    assert [event.event_type for event in events] == ["inventory.reserved"]


async def test_financially_denied_holds_no_stock(
    session: AsyncSession, committed: AsyncSession, shop: Shop
) -> None:
    """Two units is twice the ceiling, and semantically it is exactly what was asked for."""
    checkout = await quote(session, shop, quantity=2)

    readiness = await CheckoutExecutionService(session).prepare_execution(
        checkout.id, merchant_id=checkout.merchant_id, at=NOW
    )

    assert not readiness.ready
    assert readiness.authorization.financial.violations == (
        CheckoutAuthorizationViolation.MAX_TOTAL_EXCEEDED,
    )
    assert readiness.authorization.intent is not None
    assert readiness.authorization.intent.satisfied
    assert readiness.reservation is None
    assert await reservation_count(committed) == 0


async def test_semantically_denied_holds_no_stock(
    session: AsyncSession, committed: AsyncSession, shop: Shop
) -> None:
    """A blue charger at the same price: the money is fine and the purchase is not."""
    checkout = await quote(session, shop, variant_id=shop.blue)

    readiness = await CheckoutExecutionService(session).prepare_execution(
        checkout.id, merchant_id=checkout.merchant_id, at=NOW
    )

    assert not readiness.ready
    assert readiness.authorization.financial.allowed
    assert readiness.authorization.intent is not None
    assert [violation.code for violation in readiness.authorization.intent.violations] == [
        IntentViolationCode.REQUIRED_ATTRIBUTE_MISMATCH
    ]
    assert await reservation_count(committed) == 0


async def test_both_gates_denying_holds_no_stock(
    session: AsyncSession, committed: AsyncSession, shop: Shop
) -> None:
    checkout = await quote(session, shop, variant_id=shop.blue, quantity=2)

    readiness = await CheckoutExecutionService(session).prepare_execution(
        checkout.id, merchant_id=checkout.merchant_id, at=NOW
    )

    assert not readiness.ready
    assert not readiness.authorization.financial.allowed
    assert readiness.authorization.intent is not None
    assert not readiness.authorization.intent.satisfied
    assert await reservation_count(committed) == 0


async def test_a_mandate_with_no_constraint_set_holds_no_stock(
    session: AsyncSession, committed: AsyncSession
) -> None:
    """Fail closed. A mandate that was never qualified authorizes no purchase at all."""
    unqualified = await build_shop(session, constrained=False)
    checkout = await quote(session, unqualified)

    readiness = await CheckoutExecutionService(session).prepare_execution(
        checkout.id, merchant_id=checkout.merchant_id, at=NOW
    )

    assert not readiness.ready
    assert readiness.authorization.intent is None
    assert readiness.authorization.violations == (
        ExecutionAuthorizationViolation.INTENT_CONSTRAINTS_MISSING,
    )
    # The money was fine, which is exactly why this case is the dangerous one.
    assert readiness.authorization.financial.allowed
    assert await reservation_count(committed) == 0


async def test_a_cancelled_checkout_holds_no_stock(
    session: AsyncSession, committed: AsyncSession, shop: Shop
) -> None:
    checkout = await quote(session, shop)
    await CheckoutService(session).cancel_checkout(checkout.id, merchant_id=checkout.merchant_id)

    readiness = await CheckoutExecutionService(session).prepare_execution(
        checkout.id, merchant_id=checkout.merchant_id, at=NOW
    )

    assert not readiness.ready
    assert (
        CheckoutAuthorizationViolation.CHECKOUT_NOT_OPEN
        in readiness.authorization.financial.violations
    )
    assert await reservation_count(committed) == 0


async def test_an_expired_checkout_holds_no_stock(
    session: AsyncSession, committed: AsyncSession, shop: Shop
) -> None:
    """Refused before inventory, so no row is written and no lock is taken."""
    checkout = await quote(session, shop, expires_at=NOW + HOUR)

    readiness = await CheckoutExecutionService(session).prepare_execution(
        checkout.id, merchant_id=checkout.merchant_id, at=NOW + 2 * HOUR
    )

    assert not readiness.ready
    assert (
        CheckoutAuthorizationViolation.CHECKOUT_EXPIRED
        in readiness.authorization.financial.violations
    )
    assert await reservation_count(committed) == 0


async def test_a_revoked_mandate_holds_no_stock(
    session: AsyncSession, committed: AsyncSession, shop: Shop
) -> None:
    checkout = await quote(session, shop)
    await MandateRepository(session).revoke(shop.mandate)
    await session.commit()

    readiness = await CheckoutExecutionService(session).prepare_execution(
        checkout.id, merchant_id=checkout.merchant_id, at=NOW
    )

    assert not readiness.ready
    assert (
        CheckoutAuthorizationViolation.MANDATE_NOT_ACTIVE
        in readiness.authorization.financial.violations
    )
    assert await reservation_count(committed) == 0


async def test_an_expired_mandate_holds_no_stock(
    session: AsyncSession, committed: AsyncSession
) -> None:
    """The mandate lapses first, while the quote is still good for another hour."""
    lapsing = await build_shop(session, valid_until=NOW + HOUR)
    checkout = await quote(session, lapsing, expires_at=NOW + 3 * HOUR)

    readiness = await CheckoutExecutionService(session).prepare_execution(
        checkout.id, merchant_id=checkout.merchant_id, at=NOW + 2 * HOUR
    )

    assert not readiness.ready
    assert (
        CheckoutAuthorizationViolation.MANDATE_EXPIRED
        in readiness.authorization.financial.violations
    )
    assert await reservation_count(session) == 0


async def test_an_authorized_checkout_with_no_stock_left_is_not_ready(
    session: AsyncSession, committed: AsyncSession, shop: Shop
) -> None:
    """Both gates allow and the shelf is empty, which is a different refusal entirely."""
    service = CheckoutExecutionService(session)
    first, second = await quote(session, shop), await quote(session, shop)
    assert (await service.prepare_execution(first.id, merchant_id=first.merchant_id, at=NOW)).ready

    readiness = await service.prepare_execution(second.id, merchant_id=second.merchant_id, at=NOW)

    assert not readiness.ready
    assert readiness.authorization.authorized
    assert readiness.reservation is None
    violation = readiness.inventory_violations[0]
    assert violation.code is InventoryViolationCode.INSUFFICIENT_INVENTORY
    assert violation.variant_id == shop.black
    assert violation.requested_quantity == 1
    assert violation.available_quantity == 0
    # The first checkout's reservation, and nothing for the second.
    assert await reservation_count(committed) == 1


async def test_preparing_twice_holds_one_reservation(
    session: AsyncSession, committed: AsyncSession, shop: Shop
) -> None:
    """Idempotent, including in the trail."""
    service = CheckoutExecutionService(session)
    checkout = await quote(session, shop)

    first = await service.prepare_execution(checkout.id, merchant_id=checkout.merchant_id, at=NOW)
    second = await service.prepare_execution(checkout.id, merchant_id=checkout.merchant_id, at=NOW)

    assert first.ready
    assert second.ready
    assert first.reservation is not None
    assert second.reservation is not None
    assert second.reservation.id == first.reservation.id
    assert await reservation_count(committed) == 1

    events = await AuditRepository(committed).list_for_resource(
        resource_type=RESERVATION_RESOURCE, resource_id=first.reservation.id
    )
    assert [event.event_type for event in events] == ["inventory.reserved"]


async def test_the_reservation_never_outlives_the_checkout_or_the_mandate(
    session: AsyncSession,
) -> None:
    """Server derived expiry, from whichever of the two ends first."""
    service = CheckoutExecutionService(session)

    short_mandate = await build_shop(session, valid_until=NOW + HOUR)
    quoted_longer = await quote(session, short_mandate, expires_at=NOW + 3 * HOUR)
    readiness = await service.prepare_execution(
        quoted_longer.id, merchant_id=quoted_longer.merchant_id, at=NOW
    )
    assert readiness.reservation is not None
    assert readiness.reservation.expires_at == short_mandate.mandate.valid_until


async def test_the_reservation_expires_with_the_quote_when_that_ends_first(
    session: AsyncSession,
) -> None:
    long_mandate = await build_shop(session, valid_until=NOW + 6 * HOUR)
    quoted_shorter = await quote(session, long_mandate, expires_at=NOW + 2 * HOUR)

    readiness = await CheckoutExecutionService(session).prepare_execution(
        quoted_shorter.id, merchant_id=quoted_shorter.merchant_id, at=NOW
    )

    assert readiness.reservation is not None
    assert readiness.reservation.expires_at == quoted_shorter.expires_at


async def test_preparation_re_evaluates_rather_than_trusting_an_earlier_read(
    session: AsyncSession, committed: AsyncSession, shop: Shop
) -> None:
    """The load bearing property of having one path.

    An informational read says both gates allow. The mandate is then revoked. Preparing
    afterwards must decide again, against the state that exists now, and refuse.
    """
    service = CheckoutExecutionService(session)
    checkout = await quote(session, shop)

    assert (
        await service.execution_authorization(checkout.id, merchant_id=checkout.merchant_id, at=NOW)
    ).authorized

    await MandateRepository(session).revoke(shop.mandate)
    await session.commit()

    readiness = await service.prepare_execution(
        checkout.id, merchant_id=checkout.merchant_id, at=NOW
    )
    assert not readiness.ready
    assert await reservation_count(committed) == 0


async def test_the_informational_read_holds_nothing(
    session: AsyncSession, committed: AsyncSession, shop: Shop
) -> None:
    """A read that reserved stock would be a read that grants something."""
    checkout = await quote(session, shop)

    decision = await CheckoutExecutionService(session).execution_authorization(
        checkout.id, merchant_id=checkout.merchant_id, at=NOW
    )

    assert decision.authorized
    assert await reservation_count(committed) == 0


async def test_an_unknown_checkout_is_not_found(session: AsyncSession) -> None:
    with pytest.raises(NotFoundError) as unknown:
        await CheckoutExecutionService(session).prepare_execution(
            uuid.uuid7(), merchant_id=uuid.uuid7()
        )
    assert unknown.value.resource == "checkout"
