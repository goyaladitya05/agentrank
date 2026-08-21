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
from agentrank_api.checkout.execution import CheckoutExecutionReadiness
from agentrank_api.checkout.execution_authorization import (
    CheckoutExecutionAuthorization,
    ExecutionAuthorizationViolation,
)
from agentrank_api.checkout.models import CheckoutLine, CheckoutSession, CheckoutStatus
from agentrank_api.checkout.quote import (
    DEFAULT_CHECKOUT_TTL,
    MAX_CHECKOUT_LINES,
    validate_checkout_expiry,
)
from agentrank_api.checkout.service import CheckoutItem, NewCheckout
from agentrank_api.constraints.schemas import IntentAuthorizationView
from agentrank_api.inventory.models import (
    InventoryReservation,
    InventoryReservationLine,
    ReservationStatus,
)
from agentrank_api.inventory.service import InventoryViolation, InventoryViolationCode


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


class ExecutionAuthorizationView(BaseModel):
    """Both gates over one checkout, with neither answer folded into the other.

    `authorized` is the composed answer and the two decisions are stated in full beside it,
    so a caller that has to explain a refusal never needs a second request. A denial can come
    from the money, from the purchase, or from there being no semantic authorization at all,
    and those are three different problems.

    `intent_authorization` is null only when the mandate has no constraint set, and that case
    always carries `INTENT_CONSTRAINTS_MISSING` in `violations`. It never means "nothing was
    required".

    This authorizes a future execution. It is not permission to pay, and nothing in this
    application can pay.
    """

    authorized: bool
    violations: list[ExecutionAuthorizationViolation]
    financial_authorization: CheckoutAuthorizationView
    intent_authorization: IntentAuthorizationView | None

    @classmethod
    def from_decision(cls, decision: CheckoutExecutionAuthorization) -> Self:
        return cls(
            authorized=decision.authorized,
            violations=list(decision.violations),
            financial_authorization=CheckoutAuthorizationView.from_decision(decision.financial),
            intent_authorization=(
                None
                if decision.intent is None
                else IntentAuthorizationView.from_decision(decision.intent)
            ),
        )


class ReservationLineView(BaseModel):
    """One variant and how many units of it are held.

    No price and no currency. A reservation is a claim on stock, and what the buyer pays is
    already on the quote.
    """

    variant_id: uuid.UUID
    quantity: int

    @classmethod
    def from_model(cls, line: InventoryReservationLine) -> Self:
        return cls(variant_id=line.variant_id, quantity=line.quantity)


class ReservationView(BaseModel):
    """Stock held for this checkout, and until when.

    `expires_at` is stated because it is the useful half of the answer: it is the deadline by
    which an execution has to be attempted, and it was derived by the server from the quote
    and the mandate rather than chosen by anyone.

    Held is not sold. Nothing has been paid for and no stock has been consumed.
    """

    id: uuid.UUID
    checkout_id: uuid.UUID
    status: ReservationStatus
    total_quantity: int
    lines: list[ReservationLineView]
    created_at: datetime
    expires_at: datetime

    @classmethod
    def from_model(cls, reservation: InventoryReservation) -> Self:
        return cls(
            id=reservation.id,
            checkout_id=reservation.checkout_id,
            status=reservation.status,
            total_quantity=reservation.total_quantity,
            lines=[ReservationLineView.from_model(line) for line in reservation.lines],
            created_at=reservation.created_at,
            expires_at=reservation.expires_at,
        )


class InventoryViolationView(BaseModel):
    """One variant that could not be held, with the numbers that decided it.

    The code is the stable part. The quantities are what was true at the instant the decision
    was made, under the locks that made it stable, and they are what lets a caller adjust a
    basket rather than retry the same request.
    """

    code: InventoryViolationCode
    variant_id: uuid.UUID | None
    requested_quantity: int | None
    available_quantity: int | None

    @classmethod
    def from_violation(cls, violation: InventoryViolation) -> Self:
        return cls(
            code=violation.code,
            variant_id=violation.variant_id,
            requested_quantity=violation.requested_quantity,
            available_quantity=violation.available_quantity,
        )


class ExecutionPreparationView(BaseModel):
    """Whether this checkout may now be attempted, and everything that decided it.

    `ready` is the composed answer, and it is never the only thing in the body. A caller that
    cannot tell an authorization denial from an empty shelf is a caller that retries the same
    request forever.

    Ready means safe to attempt payment. It does not mean paid, and no payment exists in this
    application to attempt. The reservation is a claim on stock held until
    `reservation.expires_at`, which is the earlier of the quote expiry and the mandate
    validity.
    """

    ready: bool
    checkout_id: uuid.UUID
    evaluated_at: datetime
    authorization: ExecutionAuthorizationView
    reservation: ReservationView | None
    inventory_violations: list[InventoryViolationView]

    @classmethod
    def from_readiness(cls, readiness: CheckoutExecutionReadiness) -> Self:
        return cls(
            ready=readiness.ready,
            checkout_id=readiness.checkout_id,
            evaluated_at=readiness.evaluated_at,
            authorization=ExecutionAuthorizationView.from_decision(readiness.authorization),
            reservation=(
                None
                if readiness.reservation is None
                else ReservationView.from_model(readiness.reservation)
            ),
            inventory_violations=[
                InventoryViolationView.from_violation(violation)
                for violation in readiness.inventory_violations
            ],
        )
