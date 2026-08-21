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

from agentrank_api.errors import AgentRankError, ConflictError, NotFoundError
from agentrank_api.payments.admission import PaymentAdmission, PaymentAdmissionService
from agentrank_api.payments.execution import PaymentExecutionService, PaymentOutcome
from agentrank_api.payments.models import PaymentAttempt, PaymentAttemptStatus
from agentrank_api.payments.operations import (
    PaymentOperationResult,
    PaymentOperationsService,
    PaymentSweep,
    SweepItem,
    classify,
)
from agentrank_api.payments.provider import PaymentProvider
from agentrank_api.payments.repository import (
    DEFAULT_UNRESOLVED_LIMIT,
    PaymentAttemptRepository,
    PaymentOperationRow,
)


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
        self._operations = PaymentOperationsService(session)

    async def pay(
        self,
        checkout_id: uuid.UUID,
        *,
        merchant_id: uuid.UUID,
        idempotency_key: str,
        credential_id: uuid.UUID | None = None,
    ) -> PaymentResult:
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

        A cross merchant request reaches no provider either, and it costs less than a refusal
        does: admission raises on the first statement, having found no quote by that identifier
        belonging to this merchant. Nothing is written, nothing is locked, no idempotency key is
        looked up and no provider is asked a question. `merchant_id` is required here for that
        reason, rather than being read off the checkout, which would authorize whoever happened
        to own it.
        """
        admission = await self._admission.admit_payment(
            checkout_id,
            merchant_id=merchant_id,
            idempotency_key=idempotency_key,
            credential_id=credential_id,
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

        Unscoped, and not reachable from HTTP. The operator command line reads payments this
        way, and `pay` re-reads its own attempt this way after losing a dispatch race, having
        already established the merchant to admit it in the first place. Everything a request
        reads goes through `get_attempt_for_merchant` below.
        """
        attempt = await self._attempts.get(attempt_id)
        if attempt is None:
            raise NotFoundError("payment_attempt", str(attempt_id))
        return attempt

    async def get_attempt_for_merchant(
        self, attempt_id: uuid.UUID, *, merchant_id: uuid.UUID
    ) -> PaymentAttempt:
        """Fetch one merchant's payment, raising rather than returning None.

        Another merchant's payment raises the same error as an identifier nobody has ever used,
        because the merchant is a condition in the query and the query found nothing. A payment
        attempt identifier appears in this application's own responses and will appear in a
        provider dashboard, so it is exactly the kind of value that ends up somewhere it should
        not, and holding one must be worth nothing to anybody who is not its merchant.
        """
        attempt = await self._attempts.get_for_merchant(attempt_id, merchant_id=merchant_id)
        if attempt is None:
            raise NotFoundError("payment_attempt", str(attempt_id))
        return attempt

    async def reconcile_for_merchant(
        self, attempt_id: uuid.UUID, *, merchant_id: uuid.UUID
    ) -> PaymentOutcome:
        """Reconcile one of this merchant's payments, and refuse to reach a provider for
        anybody else's.

        Ownership is established first, by a scoped read, and the reconciliation below is only
        reached if that read found something. So a cross merchant reconciliation raises before
        the provider is asked anything, which matters more here than on any other route: this is
        the one read shaped operation that talks to an external system, and an unauthorized
        caller must not be able to make this application ask a processor about a payment that is
        not theirs.

        Two statements rather than one because reconciliation is also an operator operation and
        it has to stay callable without a merchant. What keeps the two statements safe is that
        `payment_attempt.merchant_id` is immutable: no repository method writes it, the guard
        trigger refuses every update, and the composite foreign key ties it to the checkout.
        There is no schedule in which the answer changes between the two reads.
        """
        await self.get_attempt_for_merchant(attempt_id, merchant_id=merchant_id)
        return await self.reconcile(attempt_id)

    async def reconcile(self, attempt_id: uuid.UUID) -> PaymentOutcome:
        """Ask the provider what happened to an unresolved payment.

        Delegated unchanged, including whether a provider was asked and whether anything moved.
        It exists here so that a route has one service to talk to rather than two, not because
        there is anything to add.

        It queries and never charges, which is what makes it safe to offer to an operator and
        what makes it the wrong operation for an ADMITTED attempt. That one is refused by name
        and is `resume`'s business.
        """
        return await self._execution.reconcile(attempt_id)

    async def reconcile_unresolved(self, *, limit: int = DEFAULT_UNRESOLVED_LIMIT) -> PaymentSweep:
        """Reconcile a bounded batch of unresolved payments, one at a time, and report each.

        Hand triggered and one shot. There is no scheduler behind this, no retry timer, no
        daemon and no polling loop, and that is the same decision the whole payment kernel
        rests on rather than an unimplemented feature: an ambiguous payment queried on a
        timer eventually becomes a payment charged twice by something nobody is watching. A
        person decides to ask, and this is what they get when they do.

        Bounded twice over. The listing is bounded, so this cannot walk a table, and the batch
        is exactly the listing, so it cannot grow while it runs. Payments admitted after the
        listing was taken belong to the next sweep.

        ADMITTED is skipped rather than acted on, and that is the most important line here.
        Such an attempt has provably never reached a provider, so a query would learn nothing,
        and the operation that would finish it is a payment. Performing one from inside
        something an operator ran across a whole work list is exactly the surprise this
        separation exists to prevent, so the sweep reports it and leaves it for `resume`.

        One at a time and never in parallel. Each reconciliation takes locks in the documented
        order and commits, and running several at once from one session would serialize on the
        session anyway while making the failure modes harder to reason about. Two operators
        sweeping at the same time is a different question and is safe: each attempt is re-read
        under its own lock, and whichever writer commits first is authoritative.

        A refusal is recorded and the sweep carries on. That is what stops one payment costing
        an operator the report on all the others, and it is safe because each item's outcome
        was already committed atomically by the reconciliation that produced it: there is no
        batch transaction to corrupt. Anything that is not a deliberate application error
        propagates, because an unexpected exception in a trusted recovery tool should be loud
        rather than summarized into a column.
        """
        listing = await self._operations.list_unresolved(limit=limit)
        items = [await self._sweep_one(payment) for payment in listing.payments]
        return PaymentSweep(
            observed_at=listing.observed_at, limit=listing.limit, items=tuple(items)
        )

    async def _sweep_one(self, payment: PaymentOperationRow) -> SweepItem:
        """Reconcile one payment out of a batch, or record why this one could not be.

        The snapshot decides only whether to skip. Everything the reconciliation acts on it
        re-reads under a lock, so an attempt that moved between the listing and its turn is
        handled by the kernel rather than by a stale status in this loop.
        """
        if payment.status is PaymentAttemptStatus.ADMITTED:
            return SweepItem(
                attempt_id=payment.attempt_id,
                status_before=payment.status,
                status_after=payment.status,
                result=PaymentOperationResult.SKIPPED_NOT_DISPATCHED,
            )

        try:
            outcome = await self.reconcile(payment.attempt_id)
        except AgentRankError as refused:
            # The refusing service already rolled back if it had a transaction open. This is
            # for the ones that raise from a read, so the next item starts clean either way.
            await self._session.rollback()
            return SweepItem(
                attempt_id=payment.attempt_id,
                status_before=payment.status,
                # Deliberately unknown. Nothing was read back, and reporting the snapshot here
                # would be claiming knowledge this sweep does not have.
                status_after=None,
                result=PaymentOperationResult.REFUSED,
                # The refusal's stable code, or the one name a missing payment has. Prose
                # belongs on the error rather than in a column a script may read.
                detail=refused.reason if isinstance(refused, ConflictError) else "not_found",
            )

        return SweepItem(
            attempt_id=payment.attempt_id,
            status_before=payment.status,
            status_after=outcome.attempt.status,
            result=classify(outcome),
        )

    async def resume(self, attempt_id: uuid.UUID) -> PaymentOutcome:
        """Send a payment that was admitted and never dispatched, and record the answer.

        The operator half of the crash after admission recovery. `pay` already does this for a
        buyer retrying the same identity, and there is no buyer here: the request that admitted
        this attempt died, nobody is going to retry it, and the payment sits holding a
        merchant's stock and a buyer's mandate until somebody completes or ends it.

        This is the only operator command that can move money, and the name says so. It is
        deliberately not part of `reconcile`, which everywhere else in this system means asking
        rather than doing. An operator running something called reconcile against a list of
        stuck payments must not discover afterwards that some of them were charged.

        Delegated to the same dispatch every payment goes through, so every property of that
        path holds unchanged: only ADMITTED may be dispatched and every other state is refused
        by name, IN_FLIGHT is committed before the network call so the doubt stays one sided,
        the provider is called with no transaction open, and the outcome is recorded through
        the same locked transaction a buyer's payment uses. It cannot reach a provider except
        through that path and it holds no provider of its own.

        Admission is deliberately not re-run. The authorization instant was when the attempt
        was written, and re-deciding it now would refuse a payment because a quote expired
        while the process was down, which would strand exactly the attempt this exists to
        finish. It is the same reasoning `dispatch` already documents, and it is why this
        delegates there rather than to `pay`.
        """
        return await self._execution.dispatch(attempt_id)
