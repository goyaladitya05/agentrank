"""API request and response models for payments.

Separate from the persistence models on purpose: these are the contract and the tables are an
implementation detail. Mapping is written out rather than inferred from attributes, so adding a
column never silently changes the API.

What a request may contain is one field, and the list of what it may not is longer and more
important. There is no amount, because what a payment costs was decided when the quote was
made and frozen when the payment was admitted. There is no currency, no merchant, no mandate
and no reservation quantity, all for the same reason: every one of them comes from
authoritative state, and a caller that could state one could state a different one.

There is also nothing that configures a provider. No `simulate=timeout`, no outcome override,
no test mode flag. The fake provider is chosen by the application at construction and
configured by whoever constructed it, so a production shaped caller has no way to ask for a
particular result. See docs/decisions.md.

Nothing here exposes a provider's own vocabulary either. `provider_reference` is a string this
application stored and `failure_code` is one it recorded, and neither is a passthrough of a
vendor's response body.
"""

import uuid
from datetime import datetime
from typing import Self

from pydantic import BaseModel, Field, field_validator

from agentrank_api.checkout.execution_authorization import ExecutionAuthorizationViolation
from agentrank_api.checkout.schemas import ExecutionAuthorizationView
from agentrank_api.payments.admission import AdmissionRefusal, PaymentAdmission
from agentrank_api.payments.execution import PaymentOutcome
from agentrank_api.payments.models import (
    MAX_IDEMPOTENCY_KEY_LENGTH,
    MIN_IDEMPOTENCY_KEY_LENGTH,
    OutcomeSource,
    PaymentAttempt,
    PaymentAttemptStatus,
)
from agentrank_api.payments.rules import generate_idempotency_key, validate_idempotency_key


class CreatePaymentRequest(BaseModel):
    """What a caller may state to pay for a checkout, which is one optional thing.

    `idempotency_key` names one logical payment operation. Supplying one is what makes a retry
    safe: two requests carrying the same key against the same checkout are the same request and
    resolve to the same attempt, whatever happened in between. Omitting it is allowed and is
    not the same thing. A generated key is a new identity every time, so a repeat is a new
    operation, and it will be refused while the first is still going rather than answered with
    the first one's result. That is safe and it is not idempotent, and the difference belongs
    to the caller.
    """

    idempotency_key: str | None = Field(
        default=None,
        min_length=MIN_IDEMPOTENCY_KEY_LENGTH,
        max_length=MAX_IDEMPOTENCY_KEY_LENGTH,
    )

    @field_validator("idempotency_key")
    @classmethod
    def is_a_usable_identity(cls, value: str | None) -> str | None:
        # The domain rule rather than a second copy of it, so a refusal is a 422 naming the
        # field instead of an error raised from inside a route.
        if value is not None:
            validate_idempotency_key(value)
        return value

    def resolve_key(self) -> str:
        """The identity this request will be admitted under."""
        return self.idempotency_key or generate_idempotency_key()


class PaymentAttemptView(BaseModel):
    """One payment operation as callers see it.

    The amount and the currency are the frozen ones, which is the whole point of showing them:
    a caller reading this is reading what was authorized and what a provider was asked for,
    which are the same number.

    Three timestamps and each answers a different question. `dispatched_at` is null exactly
    while the status is ADMITTED, which means no provider request has begun. `resolved_at` is
    null for UNKNOWN, because an ambiguous result is not a resolution.

    There is no idempotency key here. It is an identity the caller already has if they chose
    one, it travels to a provider, and echoing it back on every read would put it in more
    places than it needs to be.
    """

    id: uuid.UUID
    merchant_id: uuid.UUID
    checkout_id: uuid.UUID
    mandate_id: uuid.UUID
    reservation_id: uuid.UUID
    amount_minor: int
    currency: str
    status: PaymentAttemptStatus
    provider_reference: str | None
    failure_code: str | None
    outcome_source: OutcomeSource | None
    created_at: datetime
    dispatched_at: datetime | None
    resolved_at: datetime | None

    @classmethod
    def from_model(cls, attempt: PaymentAttempt) -> Self:
        return cls(
            id=attempt.id,
            merchant_id=attempt.merchant_id,
            checkout_id=attempt.checkout_id,
            mandate_id=attempt.mandate_id,
            reservation_id=attempt.reservation_id,
            amount_minor=attempt.amount_minor,
            currency=attempt.currency,
            status=attempt.status,
            provider_reference=attempt.provider_reference,
            failure_code=attempt.failure_code,
            outcome_source=attempt.outcome_source,
            created_at=attempt.created_at,
            dispatched_at=attempt.dispatched_at,
            resolved_at=attempt.resolved_at,
        )


class PaymentView(BaseModel):
    """The answer to a request to pay, whether or not one happened.

    `admitted` is the composed answer and it is never the only thing in the body. A caller that
    cannot tell an authorization denial from a payment somebody else is already making is a
    caller that will retry the same request forever, and the two call for opposite next moves.

    `refusal` is null when a payment was admitted, and it is a stable code otherwise.
    `authorization` is what both gates said during this request; when `created` is false it
    decided nothing, and it may well be a denial beside a perfectly valid attempt that was
    admitted earlier, which is honest rather than confusing once read that way.

    `created` says whether this request is what produced the attempt. It is what makes
    idempotency visible from the outside: a retry carrying the same key answers with the same
    attempt and `created: false`.

    Admitted does not mean paid. The attempt's own status says what happened.
    """

    admitted: bool
    created: bool
    checkout_id: uuid.UUID
    evaluated_at: datetime
    admitted_at: datetime | None
    refusal: AdmissionRefusal | None
    authorization: ExecutionAuthorizationView
    attempt: PaymentAttemptView | None

    @classmethod
    def from_admission(
        cls, admission: PaymentAdmission, attempt: PaymentAttempt | None = None
    ) -> Self:
        """Build the answer, preferring an attempt a later step has moved on.

        A payment request admits and then dispatches, so the attempt on the admission is one
        step out of date by the time the response is written. Passing the dispatched one in is
        what stops a caller being told ADMITTED about a payment that has already succeeded.
        """
        settled = attempt or admission.attempt
        return cls(
            admitted=admission.admitted,
            created=admission.created,
            checkout_id=admission.checkout_id,
            evaluated_at=admission.evaluated_at,
            admitted_at=admission.admitted_at,
            refusal=admission.refusal,
            authorization=ExecutionAuthorizationView.from_decision(admission.authorization),
            attempt=None if settled is None else PaymentAttemptView.from_model(settled),
        )


class ReconciliationView(BaseModel):
    """What asking the provider about an unresolved payment produced.

    `resolved` is whether this call is what moved the attempt, and `provider_queried` is
    whether a provider was asked at all. They are separate because a reconciliation that found
    a settled attempt asks nothing and changes nothing, and one that asked and learned nothing
    changes nothing either, and those are different operational facts.

    The provider's answer is not on the wire. What is on the wire is the attempt, which is the
    authoritative record of what this application believes and the only thing a caller should
    act on.
    """

    resolved: bool
    provider_queried: bool
    attempt: PaymentAttemptView

    @classmethod
    def from_outcome(cls, outcome: PaymentOutcome) -> Self:
        return cls(
            resolved=outcome.changed,
            provider_queried=outcome.provider_called,
            attempt=PaymentAttemptView.from_model(outcome.attempt),
        )


# Re-exported so that a caller reading the schema module sees the full refusal vocabulary in
# one place rather than having to find two enums in two packages.
__all__ = [
    "AdmissionRefusal",
    "CreatePaymentRequest",
    "ExecutionAuthorizationViolation",
    "PaymentAttemptView",
    "PaymentView",
    "ReconciliationView",
]
