"""One request to pay, which is an admission and then a dispatch.

Two operations exist and neither is the whole of "pay for this checkout". Admission decides,
under locks, that a payment may happen and writes the record proving it. Dispatch sends that
record to a provider and stores the answer. A caller asking to pay wants both, and this is
where they are composed, because composing them in a route would put the order of two
transactions and the handling of an already settled attempt into a handler.

The composition is short and every line of it is a decision:

```text
admit
    refused          -> report the refusal, no provider is involved
    already existed  -> return it as it is, whatever state it is in
    newly admitted   -> dispatch it
```

The middle branch is the one that matters. A repeated request resolves to an attempt that may
already have succeeded, failed or become unresolved, and none of those may be dispatched. So
the composition dispatches only what it just admitted, and a retry is answered with the answer
the first request produced rather than being sent to a provider again.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.errors import NotFoundError
from agentrank_api.payments.admission import PaymentAdmission, PaymentAdmissionService
from agentrank_api.payments.execution import PaymentExecutionService, PaymentOutcome
from agentrank_api.payments.models import PaymentAttempt, PaymentAttemptStatus
from agentrank_api.payments.provider import PaymentProvider
from agentrank_api.payments.repository import PaymentAttemptRepository


@dataclass(frozen=True, slots=True)
class PaymentResult:
    """The admission decision and the attempt as it stands after this request.

    Two fields rather than one, because they answer different questions and a caller usually
    wants both. `admission` says whether this request was allowed to start a payment and why
    not if it was not. `attempt` is the authoritative state of the payment afterwards, which
    for a newly dispatched one is its outcome and for a repeat is whatever the first request
    left behind.
    """

    admission: PaymentAdmission
    attempt: PaymentAttempt | None = None


class PaymentService:
    """The one entry point a caller asking to pay comes through."""

    def __init__(self, session: AsyncSession, provider: PaymentProvider) -> None:
        self._session = session
        self._admission = PaymentAdmissionService(session)
        self._execution = PaymentExecutionService(session, provider)
        self._attempts = PaymentAttemptRepository(session)

    async def pay(self, checkout_id: uuid.UUID, *, idempotency_key: str) -> PaymentResult:
        """Admit a payment for this checkout and, if it is new, send it to the provider.

        Only a freshly admitted attempt is dispatched. A repeat under the same identity is
        returned as it stands, because it may already have succeeded, failed or become
        unresolved, and dispatching any of those would be the second provider operation this
        whole phase exists to prevent.

        A refusal reaches no provider at all. That is worth saying explicitly: the ordinary
        reasons a payment is not allowed, from a lapsed mandate to a payment somebody else is
        already making, are all decided before anything external is involved.
        """
        admission = await self._admission.admit_payment(
            checkout_id, idempotency_key=idempotency_key
        )
        attempt = admission.attempt
        if attempt is None:
            return PaymentResult(admission=admission)
        if attempt.status is not PaymentAttemptStatus.ADMITTED:
            return PaymentResult(admission=admission, attempt=attempt)

        outcome = await self._execution.dispatch(attempt.id)
        return PaymentResult(admission=admission, attempt=outcome.attempt)

    async def get_attempt(self, attempt_id: uuid.UUID) -> PaymentAttempt:
        """Fetch one payment, raising rather than returning None.

        A caller naming an attempt has already decided it should exist, and every caller
        turning None into the same error is worse than raising it once.
        """
        attempt = await self._attempts.get(attempt_id)
        if attempt is None:
            raise NotFoundError("payment_attempt", str(attempt_id))
        return attempt

    async def reconcile(self, attempt_id: uuid.UUID) -> PaymentOutcome:
        """Ask the provider what happened to an unresolved payment.

        Delegated unchanged, including whether a provider was asked and whether anything moved.
        It exists here so that a route has one service to talk to rather than two, not because
        there is anything to add.
        """
        return await self._execution.reconcile(attempt_id)
