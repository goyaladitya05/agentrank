"""API request and response models for mandates.

Separate from the persistence models on purpose: these are the contract and the table is
an implementation detail. Mapping is written out rather than inferred from attributes, so
adding a column never silently changes the API.

Every rule the domain enforces is restated here as a field constraint or a validator.
That is what turns a refusal into a 422 naming the field instead of a 500 carrying a
database error.
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentrank_api.constraints.rules import (
    MAX_ATTRIBUTE_KEY_LENGTH,
    ConstraintOperator,
)
from agentrank_api.mandates.intent import (
    MAX_DESCRIPTION_LENGTH,
    MAX_HARD_CONSTRAINTS,
    MAX_PREFERENCES,
    MAX_STATEMENT_LENGTH,
    AllowedCategory,
    BuyerIntent,
    ConstraintKind,
    HardConstraint,
    MaxQuantity,
    MaxTotalAmount,
    Preference,
    RequiredAttribute,
)
from agentrank_api.mandates.models import MandateStatus, SpendingMandate
from agentrank_api.mandates.service import NewMandate
from agentrank_api.mandates.validation import (
    MandateValidationResult,
    MandateViolation,
    validate_validity_window,
)
from agentrank_api.money import CURRENCY_PATTERN

# Stands in for the authenticated merchant while a request is being validated, and never
# leaves the validator that uses it. The route builds the real command from the credential.
_UNVALIDATED_MERCHANT = uuid.UUID(int=0)


class MaxTotalAmountInput(BaseModel):
    # A field this schema does not define is a field a caller believed would take effect, and
    # ignoring one is how a request comes to mean something the caller did not intend: a body
    # spelling `maxQuantity` would create an unlimited mandate and be told 201.
    model_config = ConfigDict(extra="forbid")

    kind: Literal[ConstraintKind.MAX_TOTAL_AMOUNT]
    amount_minor: int = Field(ge=0)
    currency: str = Field(pattern=CURRENCY_PATTERN)

    def to_domain(self) -> MaxTotalAmount:
        return MaxTotalAmount(amount_minor=self.amount_minor, currency=self.currency)


class MaxQuantityInput(BaseModel):
    # A field this schema does not define is a field a caller believed would take effect, and
    # ignoring one is how a request comes to mean something the caller did not intend: a body
    # spelling `maxQuantity` would create an unlimited mandate and be told 201.
    model_config = ConfigDict(extra="forbid")

    kind: Literal[ConstraintKind.MAX_QUANTITY]
    quantity: int = Field(gt=0)

    def to_domain(self) -> MaxQuantity:
        return MaxQuantity(quantity=self.quantity)


# A constraint value keeps its type on the wire. `100` is a number, `"100"` is text, and
# the evaluator will never compare one against the other, so collapsing them here would
# make an authorization mean something different from what was sent.
ConstraintValueInput = bool | int | float | str
ConstraintValuesInput = ConstraintValueInput | list[ConstraintValueInput]


class RequiredAttributeInput(BaseModel):
    """A structured attribute the purchase must carry.

    `operator` defaults to `EQ`, which is what an unqualified "colour black" means. A list
    value is accepted only beside `IN`, and an ordering comparison only beside a number;
    both rules live in the domain and are applied when this is converted, so a wrong shape
    is a 422 naming the constraint rather than an error from inside a route.
    """

    # A field this schema does not define is a field a caller believed would take effect, and
    # ignoring one is how a request comes to mean something the caller did not intend: a body
    # spelling `maxQuantity` would create an unlimited mandate and be told 201.
    model_config = ConfigDict(extra="forbid")

    kind: Literal[ConstraintKind.REQUIRED_ATTRIBUTE]
    name: str = Field(min_length=1, max_length=MAX_ATTRIBUTE_KEY_LENGTH)
    operator: ConstraintOperator = ConstraintOperator.EQ
    value: ConstraintValuesInput

    def to_domain(self) -> RequiredAttribute:
        value = tuple(self.value) if isinstance(self.value, list) else self.value
        return RequiredAttribute(name=self.name, operator=self.operator, value=value)


class AllowedCategoryInput(BaseModel):
    # A field this schema does not define is a field a caller believed would take effect, and
    # ignoring one is how a request comes to mean something the caller did not intend: a body
    # spelling `maxQuantity` would create an unlimited mandate and be told 201.
    model_config = ConfigDict(extra="forbid")

    kind: Literal[ConstraintKind.ALLOWED_CATEGORY]
    category: str = Field(min_length=1, max_length=MAX_STATEMENT_LENGTH)

    def to_domain(self) -> AllowedCategory:
        return AllowedCategory(category=self.category)


# Discriminated on `kind`, so an unknown constraint is refused with a message naming the
# field rather than being silently dropped.
HardConstraintInput = Annotated[
    MaxTotalAmountInput | MaxQuantityInput | RequiredAttributeInput | AllowedCategoryInput,
    Field(discriminator="kind"),
]


class BuyerIntentInput(BaseModel):
    """Optional context for why a mandate is being asked for.

    There is no merchant here. The intent takes the mandate's merchant, so a request
    cannot describe a desire aimed at one merchant while authorizing spending at another.
    """

    # A field this schema does not define is a field a caller believed would take effect, and
    # ignoring one is how a request comes to mean something the caller did not intend: a body
    # spelling `maxQuantity` would create an unlimited mandate and be told 201.
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1, max_length=MAX_DESCRIPTION_LENGTH)
    hard_constraints: list[HardConstraintInput] = Field(
        default_factory=list, max_length=MAX_HARD_CONSTRAINTS
    )
    preferences: list[str] = Field(default_factory=list, max_length=MAX_PREFERENCES)

    def to_domain(self, merchant_id: uuid.UUID) -> BuyerIntent:
        constraints: tuple[HardConstraint, ...] = tuple(
            constraint.to_domain() for constraint in self.hard_constraints
        )
        return BuyerIntent(
            merchant_id=merchant_id,
            description=self.description,
            hard_constraints=constraints,
            preferences=tuple(Preference(statement=text) for text in self.preferences),
        )


class CreateMandateRequest(BaseModel):
    """What a caller must state to authorize spending.

    There is no `merchant_id`. It was a field until Phase 1H, and removing it is the point
    rather than a simplification: the merchant is the authenticated one, and a body that could
    name a different one would be a body that could authorize spending at somebody else's shop.

    `valid_until` is required. There is no perpetual authorization: an authorization that
    never lapses can only be ended by remembering to revoke it.

    `valid_from` defaults to the moment the request was parsed. The window is caller
    supplied policy rather than a record timestamp, which is why it does not come from
    the database clock the way `created_at` does.
    """

    # A field this schema does not define is a field a caller believed would take effect, and
    # ignoring one is how a request comes to mean something the caller did not intend: a body
    # spelling `maxQuantity` would create an unlimited mandate and be told 201.
    model_config = ConfigDict(extra="forbid")

    max_total_amount_minor: int = Field(ge=0)
    currency: str = Field(pattern=CURRENCY_PATTERN)
    max_quantity: int | None = Field(default=None, gt=0)
    valid_from: datetime = Field(default_factory=lambda: datetime.now(UTC))
    valid_until: datetime
    intent: BuyerIntentInput | None = None

    @model_validator(mode="after")
    def is_a_valid_authorization(self) -> Self:
        # Building the command applies every domain rule, including the ones the field
        # constraints above cannot express. Doing it during validation is what makes a
        # refusal a 422 rather than an error raised from inside a route.
        # The merchant is not known here, so a placeholder stands in for it. Every rule
        # `NewMandate` enforces is about the money, the window and the intent, and none of them
        # reads the merchant except to check that the intent names the same one, which it does
        # by construction below.
        validate_validity_window(self.valid_from, self.valid_until)
        self.to_command(_UNVALIDATED_MERCHANT)
        return self

    def to_command(self, merchant_id: uuid.UUID) -> NewMandate:
        return NewMandate(
            merchant_id=merchant_id,
            max_total_amount_minor=self.max_total_amount_minor,
            currency=self.currency,
            max_quantity=self.max_quantity,
            valid_from=self.valid_from,
            valid_until=self.valid_until,
            intent=self.intent.to_domain(merchant_id) if self.intent else None,
        )


class MandateView(BaseModel):
    """A mandate as callers see it.

    Everything needed to reason about the authorization, and nothing that requires
    parsing: an amount is an integer of minor units with its currency beside it, and the
    window is two timestamps.
    """

    id: uuid.UUID
    merchant_id: uuid.UUID
    max_total_amount_minor: int
    currency: str
    max_quantity: int | None
    valid_from: datetime
    valid_until: datetime
    status: MandateStatus
    created_at: datetime
    revoked_at: datetime | None

    @classmethod
    def from_model(cls, mandate: SpendingMandate) -> Self:
        return cls(
            id=mandate.id,
            merchant_id=mandate.merchant_id,
            max_total_amount_minor=mandate.max_total_amount_minor,
            currency=mandate.currency,
            max_quantity=mandate.max_quantity,
            valid_from=mandate.valid_from,
            valid_until=mandate.valid_until,
            status=mandate.status,
            created_at=mandate.created_at,
            revoked_at=mandate.revoked_at,
        )


class MandateValidationView(BaseModel):
    """Whether a mandate is usable, and if not, every reason at once."""

    valid: bool
    violations: list[MandateViolation]

    @classmethod
    def from_result(cls, result: MandateValidationResult) -> Self:
        return cls(valid=result.valid, violations=list(result.violations))
