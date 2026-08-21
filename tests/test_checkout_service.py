"""Checkout creation: what gets quoted, what gets refused, and what commits together.

The load bearing property is the price snapshot. A quote that silently follows the catalog
is not a quote, and a payment made against one would be a payment for a price nobody was
shown.
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from agentrank_api.audit.models import ActorType, AuditEvent
from agentrank_api.audit.repository import AuditRepository
from agentrank_api.checkout.models import CheckoutSession, CheckoutStatus
from agentrank_api.checkout.service import (
    CHECKOUT_RESOURCE,
    CheckoutItem,
    CheckoutService,
    NewCheckout,
)
from agentrank_api.commerce.models import Merchant, Product, Variant
from agentrank_api.commerce.repository import CatalogRepository, MerchantRepository
from agentrank_api.database import create_session_factory
from agentrank_api.errors import ConflictError, NotFoundError
from agentrank_api.mandates.models import SpendingMandate
from agentrank_api.mandates.repository import MandateRepository

pytestmark = pytest.mark.anyio

NOW = datetime.now(UTC)
HOUR = timedelta(hours=1)
CHARGER = 499900
CABLE = 129900


@dataclass(frozen=True, slots=True)
class Shop:
    """One merchant with a mandate and a small catalog of deliberate shapes."""

    merchant_id: uuid.UUID
    mandate: SpendingMandate
    charger: Variant
    cable: Variant
    euro: Variant
    inactive_variant: Variant
    variant_of_inactive_product: Variant


async def build_shop(session: AsyncSession, slug: str) -> Shop:
    merchant = await MerchantRepository(session).create(slug=slug, name=slug.title())
    mandate = await MandateRepository(session).create(
        merchant_id=merchant.id,
        max_total_amount_minor=5_000_000,
        currency="INR",
        valid_from=NOW,
        valid_until=NOW + HOUR,
    )
    catalog = CatalogRepository(session)

    async def product(external_id: str, *, is_active: bool = True) -> Product:
        return await catalog.create_product(
            merchant_id=merchant.id,
            external_id=f"{slug}-{external_id}",
            title=external_id.title(),
            is_active=is_active,
        )

    live = await product("live")
    retired = await product("retired", is_active=False)

    charger = await catalog.create_variant(
        product=live,
        sku=f"{slug}-charger",
        price_amount_minor=CHARGER,
        currency="INR",
        inventory_quantity=5,
    )
    cable = await catalog.create_variant(
        product=live,
        sku=f"{slug}-cable",
        price_amount_minor=CABLE,
        currency="INR",
        inventory_quantity=10,
    )
    euro = await catalog.create_variant(
        product=live,
        sku=f"{slug}-euro",
        price_amount_minor=4999,
        currency="EUR",
        inventory_quantity=5,
    )
    inactive_variant = await catalog.create_variant(
        product=live,
        sku=f"{slug}-refurbished",
        price_amount_minor=CHARGER,
        currency="INR",
        inventory_quantity=5,
        is_active=False,
    )
    orphan = await catalog.create_variant(
        product=retired,
        sku=f"{slug}-retired",
        price_amount_minor=CHARGER,
        currency="INR",
        inventory_quantity=5,
    )
    await session.commit()
    return Shop(
        merchant_id=merchant.id,
        mandate=mandate,
        charger=charger,
        cable=cable,
        euro=euro,
        inactive_variant=inactive_variant,
        variant_of_inactive_product=orphan,
    )


@pytest.fixture
async def shop(session: AsyncSession) -> Shop:
    return await build_shop(session, "ampere-supply")


@pytest.fixture
async def committed(catalog_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A second session, for reading what actually reached the database."""
    factory = create_session_factory(catalog_engine)
    async with factory() as other:
        yield other


def request_for(shop: Shop, *items: CheckoutItem, **overrides: object) -> NewCheckout:
    fields: dict[str, object] = {
        "merchant_id": shop.merchant_id,
        "mandate_id": shop.mandate.id,
        "items": items or (CheckoutItem(variant_id=shop.charger.id, quantity=1),),
        "expires_at": NOW + HOUR,
    }
    return NewCheckout(**(fields | overrides))  # type: ignore[arg-type]


async def test_a_quote_is_priced_from_the_catalog_and_recorded(
    session: AsyncSession, committed: AsyncSession, shop: Shop
) -> None:
    checkout = await CheckoutService(session).create_checkout(request_for(shop))

    stored = await committed.get(CheckoutSession, checkout.id)
    assert stored is not None
    assert stored.currency == "INR"
    assert stored.total_amount_minor == CHARGER
    assert stored.status is CheckoutStatus.OPEN

    events = await AuditRepository(committed).list_for_resource(
        resource_type=CHECKOUT_RESOURCE, resource_id=checkout.id
    )
    assert [event.event_type for event in events] == ["checkout.created"]
    assert events[0].actor_type is ActorType.BUYER
    assert events[0].payload["total_amount_minor"] == CHARGER
    assert events[0].payload["total_quantity"] == 1
    assert events[0].payload["lines"] == [
        {
            "variant_id": str(shop.charger.id),
            "quantity": 1,
            "unit_price_amount_minor": CHARGER,
        }
    ]


async def test_a_later_catalog_price_change_does_not_move_an_existing_quote(
    session: AsyncSession, shop: Shop
) -> None:
    """The whole reason a line stores a price rather than pointing at one."""
    service = CheckoutService(session)
    checkout = await service.create_checkout(
        request_for(shop, CheckoutItem(variant_id=shop.charger.id, quantity=2))
    )
    assert checkout.total_amount_minor == 2 * CHARGER

    shop.charger.price_amount_minor = 519900
    await session.commit()

    session.expunge_all()
    reread = await service.get_checkout(checkout.id)
    assert reread.lines[0].unit_price_amount_minor == CHARGER
    assert reread.subtotal_amount_minor == 2 * CHARGER
    assert reread.total_amount_minor == 2 * CHARGER


async def test_a_multi_line_quote_adds_up(session: AsyncSession, shop: Shop) -> None:
    checkout = await CheckoutService(session).create_checkout(
        request_for(
            shop,
            CheckoutItem(variant_id=shop.charger.id, quantity=2),
            CheckoutItem(variant_id=shop.cable.id, quantity=3),
            shipping_amount_minor=39900,
            discount_amount_minor=10000,
        )
    )

    subtotal = 2 * CHARGER + 3 * CABLE
    assert checkout.subtotal_amount_minor == subtotal
    assert checkout.shipping_amount_minor == 39900
    assert checkout.discount_amount_minor == 10000
    assert checkout.total_amount_minor == subtotal + 39900 - 10000
    # Five units across two lines. Quantity is never the number of lines.
    assert sum(line.quantity for line in checkout.lines) == 5


async def test_variants_priced_in_two_currencies_are_refused(
    session: AsyncSession, shop: Shop
) -> None:
    """No conversion anywhere. A mixed selection is refused, not resolved."""
    with pytest.raises(ConflictError) as refused:
        await CheckoutService(session).create_checkout(
            request_for(
                shop,
                CheckoutItem(variant_id=shop.charger.id, quantity=1),
                CheckoutItem(variant_id=shop.euro.id, quantity=1),
            )
        )
    assert refused.value.reason == "mixed_currencies"


async def test_a_quantity_above_available_inventory_is_refused(
    session: AsyncSession, shop: Shop
) -> None:
    with pytest.raises(ConflictError) as refused:
        await CheckoutService(session).create_checkout(
            request_for(shop, CheckoutItem(variant_id=shop.charger.id, quantity=6))
        )
    assert refused.value.reason == "insufficient_inventory"


async def test_quoting_does_not_take_stock_off_the_shelf(
    session: AsyncSession, committed: AsyncSession, shop: Shop
) -> None:
    """Inventory is compared, never decremented and never reserved."""
    await CheckoutService(session).create_checkout(
        request_for(shop, CheckoutItem(variant_id=shop.charger.id, quantity=5))
    )

    stored = await committed.get(Variant, shop.charger.id)
    assert stored is not None
    assert stored.inventory_quantity == 5


async def test_an_inactive_variant_and_an_inactive_product_are_both_refused(
    session: AsyncSession, shop: Shop
) -> None:
    service = CheckoutService(session)

    with pytest.raises(ConflictError) as variant_gone:
        await service.create_checkout(
            request_for(shop, CheckoutItem(variant_id=shop.inactive_variant.id, quantity=1))
        )
    assert variant_gone.value.reason == "variant_inactive"

    with pytest.raises(ConflictError) as product_gone:
        await service.create_checkout(
            request_for(
                shop, CheckoutItem(variant_id=shop.variant_of_inactive_product.id, quantity=1)
            )
        )
    assert product_gone.value.reason == "product_inactive"


async def test_another_merchants_mandate_and_variant_are_both_not_found(
    session: AsyncSession, shop: Shop
) -> None:
    """Isolation, answered as absence. A caller scoped to one merchant must not learn
    what another merchant has authorized or sells."""
    other = await build_shop(session, "volt-mart")
    service = CheckoutService(session)

    with pytest.raises(NotFoundError) as wrong_mandate:
        await service.create_checkout(request_for(shop, mandate_id=other.mandate.id))
    assert wrong_mandate.value.resource == "mandate"

    with pytest.raises(NotFoundError) as wrong_variant:
        await service.create_checkout(
            request_for(shop, CheckoutItem(variant_id=other.charger.id, quantity=1))
        )
    assert wrong_variant.value.resource == "variant"


async def test_an_unknown_merchant_mandate_or_variant_is_not_found(
    session: AsyncSession, shop: Shop
) -> None:
    service = CheckoutService(session)
    missing = uuid.uuid7()

    absent = Merchant(id=missing, slug="nobody", name="Nobody")
    with pytest.raises(NotFoundError) as unknown_merchant:
        await service.create_checkout(request_for(shop, merchant_id=absent.id))
    assert unknown_merchant.value.resource == "merchant"

    with pytest.raises(NotFoundError) as unknown_variant:
        await service.create_checkout(
            request_for(shop, CheckoutItem(variant_id=missing, quantity=1))
        )
    assert unknown_variant.value.resource == "variant"

    with pytest.raises(NotFoundError) as unknown_checkout:
        await service.get_checkout(missing)
    assert unknown_checkout.value.resource == "checkout"


async def test_a_malformed_request_is_refused_before_anything_is_read(shop: Shop) -> None:
    """Domain refusals, so an HTTP caller and a service caller get the same answer."""
    with pytest.raises(ValueError, match="at least one item"):
        request_for(shop, **{"items": ()})

    with pytest.raises(ValueError, match="quantity must be positive"):
        CheckoutItem(variant_id=shop.charger.id, quantity=0)

    with pytest.raises(ValueError, match="same variant twice"):
        request_for(
            shop,
            CheckoutItem(variant_id=shop.charger.id, quantity=1),
            CheckoutItem(variant_id=shop.charger.id, quantity=2),
        )


async def test_an_expiry_in_the_past_or_too_far_ahead_is_refused(
    session: AsyncSession, shop: Shop
) -> None:
    service = CheckoutService(session)

    with pytest.raises(ValueError, match="in the future"):
        await service.create_checkout(request_for(shop, expires_at=NOW - HOUR))

    with pytest.raises(ValueError, match="within"):
        await service.create_checkout(request_for(shop, expires_at=NOW + 48 * HOUR))


async def test_nothing_persists_when_the_audit_append_fails(
    session: AsyncSession,
    committed: AsyncSession,
    shop: Shop,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The transaction boundary, asserted rather than assumed.

    The checkout and its lines are written and flushed before the audit event, so if the
    two were not one transaction the rows would survive this.
    """

    async def refuse(*args: object, **kwargs: object) -> None:
        raise RuntimeError("audit is unavailable")

    monkeypatch.setattr(AuditRepository, "append", refuse)

    with pytest.raises(RuntimeError, match="audit is unavailable"):
        await CheckoutService(session).create_checkout(request_for(shop))

    # The reader can see committed rows, so an empty result below means nothing was
    # committed rather than that this session cannot see anything.
    assert await committed.get(Merchant, shop.merchant_id) is not None
    assert await committed.scalar(select(func.count()).select_from(CheckoutSession)) == 0
    assert await committed.scalar(select(func.count()).select_from(AuditEvent)) == 0


async def test_cancelling_records_one_event_and_repeating_records_none(
    session: AsyncSession, committed: AsyncSession, shop: Shop
) -> None:
    service = CheckoutService(session)
    checkout = await service.create_checkout(request_for(shop))
    quoted = checkout.total_amount_minor

    cancelled = await service.cancel_checkout(checkout.id)
    assert cancelled.status is CheckoutStatus.CANCELLED
    assert cancelled.cancelled_at is not None
    # Withdrawing a quote does not rewrite what was quoted.
    assert cancelled.total_amount_minor == quoted

    again = await service.cancel_checkout(checkout.id)
    assert again.cancelled_at == cancelled.cancelled_at

    events = await AuditRepository(committed).list_for_resource(
        resource_type=CHECKOUT_RESOURCE, resource_id=checkout.id
    )
    assert [event.event_type for event in events] == ["checkout.created", "checkout.cancelled"]
    # The cancellation and the event that records it share the transaction clock.
    assert events[1].occurred_at == cancelled.cancelled_at


async def test_cancelling_an_unknown_checkout_is_not_found(session: AsyncSession) -> None:
    with pytest.raises(NotFoundError) as unknown:
        await CheckoutService(session).cancel_checkout(uuid.uuid7())
    assert unknown.value.resource == "checkout"
