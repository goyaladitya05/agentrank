"""API request and response models for intent constraints.

Separate from the persistence models on purpose: these are the contract and the tables are
an implementation detail. Mapping is written out rather than inferred from attributes, so
adding a column never silently changes the API.

The request reuses `HardConstraintInput`, the same discriminated union a `BuyerIntent`
carries on a mandate creation. That is deliberate: a buyer states a requirement in one
vocabulary, and the difference between an intent and an authorization is what happens to it
afterwards, not how it is written down.

The response calls a required attribute's name `attribute` rather than `name`, matching the
stored column and every violation that reports it. The request keeps `name`, because that is
the existing buyer intent contract and churning it would give the same field a third name.
"""

import uuid
from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, Field, model_validator

from agentrank_api.checkout.intent_authorization import (
    IntentConstraintDecision,
    IntentConstraintViolation,
    IntentViolationCode,
)
from agentrank_api.constraints.models import IntentConstraint, IntentConstraintSet
from agentrank_api.constraints.rules import ConstraintOperator, PersistedConstraintKind
from agentrank_api.constraints.service import NewIntentConstraints
from agentrank_api.mandates.intent import MAX_HARD_CONSTRAINTS, HardConstraint
from agentrank_api.mandates.schemas import HardConstraintInput

# Stands in for the authenticated merchant while a request is being validated, and never
# leaves the validator that uses it. The route builds the real command from the credential.
_UNVALIDATED_MERCHANT = uuid.UUID(int=0)


class CreateIntentConstraintsRequest(BaseModel):
    """The hard constraints a purchase under one mandate must satisfy.

    The mandate is in the path and the merchant is the authenticated one. It used to be a body
    field, for a reason that was real at the time: without it, knowing a mandate identifier was
    the whole authorization needed to decide what that mandate may buy. A credential is a
    better answer to the same problem than a second identifier a caller supplies, and it is the
    only answer that also covers the reads. So the field is gone, and the mandate is resolved
    scoped to the credential's merchant.

    A financial constraint may appear here and is validated against the mandate rather than
    stored. At least one semantic constraint is required, because a constraint set with
    nothing in it is the absence of an authorization rather than a permissive one.
    """

    constraints: list[HardConstraintInput] = Field(min_length=1, max_length=MAX_HARD_CONSTRAINTS)

    @model_validator(mode="after")
    def is_an_authorizable_request(self) -> Self:
        # Building the command applies every domain rule, including the ones the field
        # constraints above cannot express. Doing it during validation is what makes a
        # refusal a 422 rather than an error raised from inside a route. Neither identifier is
        # known here and neither is read by any of those rules, so placeholders stand in for
        # both. The route builds the command that carries the real ones.
        self.to_command(uuid.uuid7(), _UNVALIDATED_MERCHANT)
        return self

    def to_command(self, mandate_id: uuid.UUID, merchant_id: uuid.UUID) -> NewIntentConstraints:
        constraints: tuple[HardConstraint, ...] = tuple(
            constraint.to_domain() for constraint in self.constraints
        )
        return NewIntentConstraints(
            merchant_id=merchant_id,
            mandate_id=mandate_id,
            hard_constraints=constraints,
        )


class IntentConstraintView(BaseModel):
    """One authorized rule, flattened.

    `attribute` is null for a category rule and `value` is a list beside `IN`, which is what
    the operator already says. Nothing here is formatted or rendered: a buyer agent reads
    the same structure the evaluator compares with.
    """

    id: uuid.UUID
    kind: PersistedConstraintKind
    attribute: str | None
    operator: ConstraintOperator
    value: Any

    @classmethod
    def from_model(cls, constraint: IntentConstraint) -> Self:
        return cls(
            id=constraint.id,
            kind=constraint.kind,
            attribute=constraint.attribute_key,
            operator=constraint.operator,
            value=constraint.value,
        )


class IntentConstraintSetView(BaseModel):
    """The semantic half of one authorization, as callers see it.

    There is no status and no `updated_at`, because a constraint set has no lifecycle. It is
    written once with its mandate and then only read.
    """

    id: uuid.UUID
    merchant_id: uuid.UUID
    mandate_id: uuid.UUID
    constraints: list[IntentConstraintView]
    created_at: datetime

    @classmethod
    def from_model(cls, constraint_set: IntentConstraintSet) -> Self:
        return cls(
            id=constraint_set.id,
            merchant_id=constraint_set.merchant_id,
            mandate_id=constraint_set.mandate_id,
            constraints=[
                IntentConstraintView.from_model(constraint)
                for constraint in constraint_set.constraints
            ],
            created_at=constraint_set.created_at,
        )


class IntentViolationView(BaseModel):
    """Why one line failed one rule, in enough detail to explain without a second request."""

    code: IntentViolationCode
    constraint_id: uuid.UUID | None
    line_id: uuid.UUID | None
    variant_id: uuid.UUID | None
    attribute: str | None
    operator: ConstraintOperator | None
    expected: Any
    actual: Any

    @classmethod
    def from_violation(cls, violation: IntentConstraintViolation) -> Self:
        return cls(
            code=violation.code,
            constraint_id=violation.constraint_id,
            line_id=violation.line_id,
            variant_id=violation.variant_id,
            attribute=violation.attribute,
            operator=violation.operator,
            expected=violation.expected,
            actual=violation.actual,
        )


class IntentAuthorizationView(BaseModel):
    """Whether this checkout is what the buyer asked for, and if not, every reason at once.

    Semantically. This says nothing about whether the money is within what was authorized,
    which is a separate decision at a separate endpoint. Both have to allow before any
    payment could be considered safe, and nothing in this application makes one.

    `constraint_set_id` is stated because the caller did not choose it. It is resolved
    through the mandate the checkout was quoted against.
    """

    satisfied: bool
    constraint_set_id: uuid.UUID
    violations: list[IntentViolationView]

    @classmethod
    def from_decision(cls, decision: IntentConstraintDecision) -> Self:
        return cls(
            satisfied=decision.satisfied,
            constraint_set_id=decision.constraint_set_id,
            violations=[
                IntentViolationView.from_violation(violation) for violation in decision.violations
            ],
        )
