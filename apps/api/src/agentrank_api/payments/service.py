"""One request to pay, which is an admission and then a dispatch.

Two operations exist and neither is the whole of "pay for this checkout". Admission decides,
under locks, that a payment may happen and writes the record proving it. Dispatch sends that
record to a provider and stores the answer. A caller asking to pay wants both, and this is
where they are composed, because composing them in a route would put the order of two
transactions and the handling of an already settled attempt into a handler.

The composition is short and every line of it is a decision:

```text
admit
    refused              -> report the refusal, no provider is involved
    attempt is ADMITTED  -> dispatch it
    anything else        -> return it as it is, whatever state it is in
```

The branch is on the attempt's state and deliberately not on whether this call created it. An
ADMITTED attempt has provably never reached a provider, whoever wrote it, so dispatching one
that a previous request left behind is the recovery path for a process that died between the
admission commit and the dispatch commit. Refusing to dispatch it because somebody else
admitted it would leave the payment stranded in exactly the state it is safe to act on.

Every other state either has an answer already or may have one arriving, and none of them may
be dispatched. A retry that resolves to one of those is answered with what the first request
produced rather than being sent to a provider again.

The race between those two branches is real and is handled rather than raced. Two requests
carrying one key can both see ADMITTED, because the read that decides is not the lock that
dispatches. One of them wins the dispatch lock and the other finds the attempt has moved on,
which is not an error: it is the idempotent answer arriving a moment late. The loser re-reads
the authoritative attempt and returns it. It does not dispatch it, and finding an existing
attempt is never permission to.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.errors import ConflictError, NotFoundError
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
        """Admit a payment for this checkout and, if it may be dispatched, send it.

        An ADMITTED attempt is dispatched whether this call created it or found it. ADMITTED
        means the provider has provably never heard of the identity, because IN_FLIGHT is
        committed before any network call begins, so acting on one is safe regardless of who
        wrote it. That is also the recovery path for a request whose process died after
        admission committed: the retry finds the attempt and completes it.

        Any other state is returned as it stands. It has an answer already or may have one
        arriving, and dispatching any of those would be the second provider operation this
        whole phase exists to prevent.

        A same key retry racing the original is answered with the attempt rather than with a
        conflict. Both requests can read ADMITTED, because the read that decides is not the
        lock that dispatches, and the one that loses the lock finds the attempt already
        IN_FLIGHT. That is the idempotent answer arriving a moment late rather than an error,
        so it re-reads the authoritative row and returns it. It does not dispatch it: having
        an attempt in hand is never permission to send one, and only the ADMITTED branch above
        may do that.

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
        # Read before the dispatch below, because a refusal inside it rolls back and the
        # rollback expires every attribute on this object.
        attempt_id = attempt.id
        if attempt.status is not PaymentAttemptStatus.ADMITTED:
            return PaymentResult(admission=admission, attempt=attempt)

        try:
            outcome = await self._execution.dispatch(attempt_id)
        except ConflictError:
            # Re-read rather than reused. The object admitted above was read before the
            # refusal rolled back, and what a caller needs is the state that actually stands
            # now, which is whatever the request that won the lock left behind.
            overtaken = await self.get_attempt(attempt_id)
            if overtaken.status is PaymentAttemptStatus.ADMITTED:
                # Not a lost race. The dispatch refused for a reason this path cannot answer,
                # and swallowing it would hide it. Unreachable while `dispatch` refuses only a
                # non ADMITTED attempt, and stated rather than assumed.
                raise
            return PaymentResult(admission=admission, attempt=overtaken)
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
