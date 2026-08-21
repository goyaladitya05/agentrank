"""API request and response models for checkouts.

Separate from the persistence models on purpose: these are the contract and the tables are
an implementation detail. Mapping is written out rather than inferred from attributes, so
adding a column never silently changes the API.

Every rule the domain enforces is restated here as a field constraint or a validator. That
is what turns a refusal into a 422 naming the field instead of a 500 carrying a database
error.

Two things are deliberately absent from the request. There is no unit price, because what a
merchant charges is not a caller's to state. There is no shipping or discount amount,
because neither has an authoritative source yet and an unauthenticated caller must not be
able to move a quote total. See docs/decisions.md.
"""

import uuid
from datetime import UTC, datetime
from typing import Any, Self

from pydantic import BaseModel, Field, model_validator

from agentrank_api.checkout.authorization import (
    CheckoutAuthorizationDecision,
    CheckoutAuthorizationViolation,
)
from agentrank_api.checkout.models import CheckoutLine, CheckoutSession, CheckoutStatus
from agentrank_api.checkout.quote import (
    DEFAULT_CHECKOUT_TTL,
    MAX_CHECKOUT_LINES,
    validate_checkout_expiry,
)
from agentrank_api.checkout.service import CheckoutItem, NewCheckout


class CheckoutItemInput(BaseModel):
    variant_id: uuid.UUID
    quantity: int = Field(gt=0)

    def to_domain(self) -> CheckoutItem:
        return CheckoutItem(variant_id=self.variant_id, quantity=self.quantity)


class CreateCheckoutRequest(BaseModel):
    """What a caller must state to be quoted.

    `expires_at` is optional and defaults to a short window from the moment the request was
    parsed. It is bounded on both sides: a quote cannot be created already expired, and it
    cannot be pushed arbitrarily far out, because a quote that lasts a year is a promise to
    honour a price nobody rechecked.
    """

    merchant_id: uuid.UUID
    mandate_id: uuid.UUID
    items: list[CheckoutItemInput] = Field(min_length=1, max_length=MAX_CHECKOUT_LINES)
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def is_a_quotable_request(self) -> Self:
        # Building the command applies every domain rule, including the ones the field
        # constraints above cannot express. Doing it during validation is what makes a
        # refusal a 422 rather than an error raised from inside a route.
        self.to_command()
        return self

    def to_command(self) -> NewCheckout:
        now = datetime.now(UTC)
        expires_at = self.expires_at if self.expires_at is not None else now + DEFAULT_CHECKOUT_TTL
        validate_checkout_expiry(expires_at, now=now)
        return NewCheckout(
            merchant_id=self.merchant_id,
            mandate_id=self.mandate_id,
            items=tuple(item.to_domain() for item in self.items),
            expires_at=expires_at,
        )


class CheckoutLineView(BaseModel):
    """One quoted line: priced, and described as the catalog described it at the time.

    Every field here is the snapshot that was taken when the quote was made, so a caller
    never has to reread a variant to understand what was offered, and rereading one could
    not tell them anyway once the catalog has moved on.

    `product_category` and `variant_attributes` are what semantic authorization is decided
    against, and showing them is what makes a denial explainable without a second request.
    """

    id: uuid.UUID
    variant_id: uuid.UUID
    quantity: int
    unit_price_amount_minor: int
    line_amount_minor: int
    currency: str
    product_category: str | None
    variant_attributes: dict[str, Any]

    @classmethod
    def from_model(cls, line: CheckoutLine) -> Self:
        return cls(
            id=line.id,
            variant_id=line.variant_id,
            quantity=line.quantity,
            unit_price_amount_minor=line.unit_price_amount_minor,
            line_amount_minor=line.line_amount_minor,
            currency=line.currency,
            product_category=line.product_category,
            variant_attributes=line.variant_attributes,
        )


class CheckoutView(BaseModel):
    """A quote as callers see it.

    Everything needed to reason about the offer, and nothing that requires parsing: every
    amount is an integer of minor units with the currency beside it, and there is no
    formatted money anywhere. `total_quantity` is stated because it is the number a mandate
    ceiling is compared against, and deriving it from the lines is exactly the place a
    caller would count lines by mistake.
    """

    id: uuid.UUID
    merchant_id: uuid.UUID
    mandate_id: uuid.UUID
    currency: str
    lines: list[CheckoutLineView]
    total_quantity: int
    subtotal_amount_minor: int
    shipping_amount_minor: int
    discount_amount_minor: int
    total_amount_minor: int
    status: CheckoutStatus
    created_at: datetime
    expires_at: datetime
    cancelled_at: datetime | None

    @classmethod
    def from_model(cls, checkout: CheckoutSession) -> Self:
        return cls(
            id=checkout.id,
            merchant_id=checkout.merchant_id,
            mandate_id=checkout.mandate_id,
            currency=checkout.currency,
            lines=[CheckoutLineView.from_model(line) for line in checkout.lines],
            total_quantity=sum(line.quantity for line in checkout.lines),
            subtotal_amount_minor=checkout.subtotal_amount_minor,
            shipping_amount_minor=checkout.shipping_amount_minor,
            discount_amount_minor=checkout.discount_amount_minor,
            total_amount_minor=checkout.total_amount_minor,
            status=checkout.status,
            created_at=checkout.created_at,
            expires_at=checkout.expires_at,
            cancelled_at=checkout.cancelled_at,
        )


class CheckoutAuthorizationView(BaseModel):
    """Whether this checkout is financially authorized, and if not, every reason at once.

    Financially. This says nothing about whether the checkout satisfies the buyer's intent,
    which nothing enforces yet. See docs/security.md.
    """

    allowed: bool
    violations: list[CheckoutAuthorizationViolation]

    @classmethod
    def from_decision(cls, decision: CheckoutAuthorizationDecision) -> Self:
        return cls(allowed=decision.allowed, violations=list(decision.violations))
