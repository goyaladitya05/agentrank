"""Checkout application service.

Workflows live here: create a quote, read one back, and answer each of the two
authorization questions about it. The service coordinates the repositories, the domain
rules and the audit trail, and it owns the transaction. Routes call one method and
serialize the result.

The two authorization reads stay separate methods returning separate decisions. There is no
method that combines them, because the only caller for a combined answer is payment
execution, which does not exist. See docs/decisions.md.

Three rules shape this module:

- a quote is priced from the catalog, never from the request. A caller states which
  variants and how many; what they cost is not a caller's to say. The same applies to what
  they are: the category and the structured attributes are snapshotted from the catalog at
  the same moment the price is
- a checkout and the audit event recording it commit together or not at all
- creating a quote is not authorizing it. This service never asks whether the mandate
  permits the total, only that the mandate belongs to the same merchant. A quote for more
  than a buyer may spend is a valid merchant quote that is simply not authorized, and
  collapsing the two would remove the distinction the next phase is built on
"""

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.audit.models import ActorType
from agentrank_api.audit.repository import AuditRepository
from agentrank_api.checkout.authorization import (
    CheckoutAuthorizationDecision,
    authorize_checkout,
)
from agentrank_api.checkout.intent_authorization import (
    IntentConstraintDecision,
    evaluate_intent_constraints,
)
from agentrank_api.checkout.models import CheckoutSession, CheckoutStatus
from agentrank_api.checkout.quote import (
    MAX_CHECKOUT_LINES,
    QuotedLine,
    total_quantity,
    validate_checkout_expiry,
)
from agentrank_api.checkout.repository import CheckoutRepository
from agentrank_api.commerce.models import Variant
from agentrank_api.commerce.repository import CatalogRepository, MerchantRepository
from agentrank_api.constraints.repository import IntentConstraintRepository
from agentrank_api.errors import ConflictError, NotFoundError
from agentrank_api.mandates.repository import MandateRepository
from agentrank_api.money import validate_amount_minor

CHECKOUT_RESOURCE = "checkout_session"
CHECKOUT_CREATED = "checkout.created"
CHECKOUT_CANCELLED = "checkout.cancelled"

# A quote is prepared on the buyer's behalf, so it is the buyer's act. This names a role
# and not a verified identity: nothing authenticates a caller yet.
CHECKOUT_ACTOR = ActorType.BUYER


@dataclass(frozen=True, slots=True)
class CheckoutItem:
    """One thing a caller wants on the quote.

    There is no price here on purpose. Accepting a unit price from a caller would let the
    caller decide what a merchant charges, which is the one thing a quote exists to state.
    """

    variant_id: uuid.UUID
    quantity: int

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(f"quantity must be positive, got {self.quantity}")


@dataclass(frozen=True, slots=True)
class NewCheckout:
    """A request to prepare a quote, refused before it reaches the database if wrong.

    `shipping_amount_minor` and `discount_amount_minor` are quote inputs rather than
    computed values. Neither has an authoritative source yet: there is no shipping quote
    provider and no promotion engine. They are here so that the price model is complete
    and so that adding either later is not a migration, and they are deliberately not
    reachable from the API. See docs/shortcomings.md.
    """

    merchant_id: uuid.UUID
    mandate_id: uuid.UUID
    items: tuple[CheckoutItem, ...]
    expires_at: datetime
    shipping_amount_minor: int = 0
    discount_amount_minor: int = 0

    def __post_init__(self) -> None:
        if not self.items:
            raise ValueError("a checkout must contain at least one item")
        if len(self.items) > MAX_CHECKOUT_LINES:
            raise ValueError(f"a checkout may contain at most {MAX_CHECKOUT_LINES} items")
        if len({item.variant_id for item in self.items}) != len(self.items):
            raise ValueError("a checkout must not name the same variant twice")
        validate_amount_minor(self.shipping_amount_minor)
        validate_amount_minor(self.discount_amount_minor)
        if self.expires_at.tzinfo is None:
            raise ValueError("expires_at must be timezone aware")


class CheckoutService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._merchants = MerchantRepository(session)
        self._mandates = MandateRepository(session)
        self._catalog = CatalogRepository(session)
        self._checkouts = CheckoutRepository(session)
        self._constraints = IntentConstraintRepository(session)
        self._audit = AuditRepository(session)

    async def create_checkout(self, request: NewCheckout) -> CheckoutSession:
        """Price a quote from the catalog and record that it was prepared, in one
        transaction.

        Everything is looked up before anything is written, so an unknown merchant,
        mandate or variant is a 404 naming the resource rather than a foreign key
        violation surfacing as a server error.

        Both writes happen in one transaction and one commit. If the audit append fails,
        neither the checkout nor its lines are persisted: a quote with no record of being
        prepared is exactly what the audit trail exists to prevent.
        """
        merchant = await self._merchants.get_by_id(request.merchant_id)
        if merchant is None:
            raise NotFoundError("merchant", str(request.merchant_id))

        mandate = await self._mandates.get(request.mandate_id)
        # A mandate granted to another merchant does not exist as far as this merchant is
        # concerned. Saying so is both the isolation rule and the honest answer: a caller
        # scoped to one merchant must not learn what another merchant has authorized.
        if mandate is None or mandate.merchant_id != request.merchant_id:
            raise NotFoundError("mandate", str(request.mandate_id))

        validate_checkout_expiry(request.expires_at, now=datetime.now(UTC))

        variants = await self._catalog.get_variants(
            merchant_id=request.merchant_id,
            variant_ids=[item.variant_id for item in request.items],
        )
        by_id = {variant.id: variant for variant in variants}
        lines = [_quote_line(item, by_id) for item in request.items]

        checkout = await self._checkouts.create(
            merchant_id=request.merchant_id,
            mandate_id=request.mandate_id,
            currency=_single_currency(by_id[item.variant_id] for item in request.items),
            lines=lines,
            expires_at=request.expires_at,
            shipping_amount_minor=request.shipping_amount_minor,
            discount_amount_minor=request.discount_amount_minor,
        )
        await self._append(checkout, CHECKOUT_CREATED, _created_payload(checkout, lines))
        await self._session.commit()
        return checkout

    async def get_checkout(self, checkout_id: uuid.UUID) -> CheckoutSession:
        """Fetch a quote with its lines, raising rather than returning None.

        A caller naming a checkout has already decided it should exist, and every caller
        turning None into the same error is worse than raising it once.
        """
        checkout = await self._checkouts.get(checkout_id)
        if checkout is None:
            raise NotFoundError("checkout", str(checkout_id))
        return checkout

    async def authorize_checkout(
        self, checkout_id: uuid.UUID, *, at: datetime | None = None
    ) -> CheckoutAuthorizationDecision:
        """Report whether this checkout is financially authorized, at `at` or right now.

        This is the layer allowed to read the clock. The rule underneath it takes the
        instant as an argument and reads nothing, which is what keeps it deterministic.

        Nothing is written, and no catalog row is read. The decision is made against the
        quote as it was recorded, so a price change since then cannot alter what it was
        made against.
        """
        checkout = await self.get_checkout(checkout_id)
        mandate = await self._mandates.get(checkout.mandate_id)
        if mandate is None:
            # Not reachable through the schema: the foreign key onto the mandate is
            # RESTRICT, so the mandate cannot have been removed while this quote exists.
            raise NotFoundError("mandate", str(checkout.mandate_id))
        return authorize_checkout(checkout, mandate, at=at or datetime.now(UTC))

    async def evaluate_intent_constraints(self, checkout_id: uuid.UUID) -> IntentConstraintDecision:
        """Report whether this checkout is what the buyer asked for.

        The second gate, and it is resolved entirely from persisted rows: the checkout, the
        mandate it names, and the constraint set qualifying that mandate. No model, no
        prompt, no conversation history and no audit event is read to reach it.

        A caller does not choose the constraints. They are found through the mandate the
        quote was written against, which is what stops an authorization from being paired
        with terms someone picked afterwards.

        A mandate with no constraint set raises rather than reporting satisfaction. Absence
        of a semantic authorization is not a passed one.

        Nothing is written, and no catalog row is read. The decision is made against the
        semantic snapshot the quote recorded, so a catalog change since then cannot alter
        what it was made against.
        """
        checkout = await self.get_checkout(checkout_id)
        constraint_set = await self._constraints.get_for_mandate(checkout.mandate_id)
        if constraint_set is None:
            raise NotFoundError("intent_constraints", str(checkout.mandate_id))
        return evaluate_intent_constraints(checkout, constraint_set)

    async def cancel_checkout(self, checkout_id: uuid.UUID) -> CheckoutSession:
        """Withdraw a quote and record it, once.

        Idempotent. Cancelling an already cancelled checkout returns it unchanged and
        appends nothing, so a retried request cannot produce a second cancellation event or
        move the original timestamp. Cancellation is terminal; there is no counterpart that
        reopens one, and the database enforces that too.

        Nothing about the price changes. Cancelling a quote withdraws it, it does not
        rewrite what was quoted, and the trigger on the table refuses any attempt to do
        both at once.
        """
        checkout = await self.get_checkout(checkout_id)
        if await self._checkouts.cancel(checkout):
            await self._append(
                checkout, CHECKOUT_CANCELLED, {"status": CheckoutStatus.CANCELLED.value}
            )
        # Committed either way. When nothing changed this just closes the read.
        await self._session.commit()
        return checkout

    async def _append(
        self, checkout: CheckoutSession, event_type: str, payload: dict[str, Any]
    ) -> None:
        await self._audit.append(
            merchant_id=checkout.merchant_id,
            actor_type=CHECKOUT_ACTOR,
            event_type=event_type,
            resource_type=CHECKOUT_RESOURCE,
            resource_id=checkout.id,
            payload=payload,
        )


def _quote_line(item: CheckoutItem, by_id: dict[uuid.UUID, Variant]) -> QuotedLine:
    """Turn one requested item into a priced line, or refuse it.

    Inventory is compared, never decremented and never reserved. This is a quote, and a
    quote does not take stock off the shelf. What that leaves open is recorded in
    docs/shortcomings.md: stock can change between the quote and any later execution.

    The price and the semantic snapshot are read together, from the same variant, at the
    same instant. A later catalog edit changes neither, so what the quote says it costs and
    what the quote says it is stay one consistent description of one offer.
    """
    variant = by_id.get(item.variant_id)
    if variant is None:
        raise NotFoundError("variant", str(item.variant_id))
    if not variant.is_active:
        raise ConflictError(
            "variant_inactive",
            f"variant {variant.id} is not available for purchase",
            resource="variant",
            identifier=str(variant.id),
        )
    if not variant.product.is_active:
        raise ConflictError(
            "product_inactive",
            f"product {variant.product_id} is not available for purchase",
            resource="product",
            identifier=str(variant.product_id),
        )
    if item.quantity > variant.inventory_quantity:
        raise ConflictError(
            "insufficient_inventory",
            f"variant {variant.id} has {variant.inventory_quantity} available,"
            f" {item.quantity} requested",
            resource="variant",
            identifier=str(variant.id),
        )
    return QuotedLine(
        variant_id=variant.id,
        quantity=item.quantity,
        unit_price_amount_minor=variant.price_amount_minor,
        # Read once, here, and never again. This is the only place the live catalog is
        # consulted for what an item actually is, which is what makes a later semantic
        # decision a decision about the offer the buyer saw.
        product_category=variant.product.category,
        variant_attributes=variant.attributes,
    )


def _single_currency(variants: Iterable[Variant]) -> str:
    """The one currency every selected variant is priced in.

    A checkout has exactly one currency and there is no conversion anywhere in this
    system. A mixed selection is refused rather than resolved, because resolving it would
    mean inventing an exchange rate, and an invented rate in a financial quote is worse
    than a refusal.
    """
    currencies = {variant.currency for variant in variants}
    if len(currencies) > 1:
        raise ConflictError(
            "mixed_currencies",
            f"a checkout has one currency, and these variants are priced in"
            f" {', '.join(sorted(currencies))}",
        )
    return currencies.pop()


def _created_payload(checkout: CheckoutSession, lines: Sequence[QuotedLine]) -> dict[str, Any]:
    """What was quoted, in the words of the quote itself.

    The lines are recorded as well as the totals, so the trail answers what a buyer was
    offered and not merely what it added up to. That is what makes a later dispute about a
    price answerable without the catalog.
    """
    return {
        "mandate_id": str(checkout.mandate_id),
        "currency": checkout.currency,
        "subtotal_amount_minor": checkout.subtotal_amount_minor,
        "shipping_amount_minor": checkout.shipping_amount_minor,
        "discount_amount_minor": checkout.discount_amount_minor,
        "total_amount_minor": checkout.total_amount_minor,
        "total_quantity": total_quantity(lines),
        "expires_at": checkout.expires_at.isoformat(),
        "status": checkout.status.value,
        "lines": [
            {
                "variant_id": str(line.variant_id),
                "quantity": line.quantity,
                "unit_price_amount_minor": line.unit_price_amount_minor,
            }
            for line in lines
        ],
    }
