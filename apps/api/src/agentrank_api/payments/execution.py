"""Calling a provider, and recording what it said, without ever holding a lock across the wire.

Three steps, three transaction boundaries, and the shape is the whole design:

```text
TRANSACTION 1   lock the attempt, require ADMITTED, mark IN_FLIGHT, commit
                |
                v
NETWORK         provider.execute(instruction), with no transaction open
                |
                v
TRANSACTION 2   lock everything the outcome touches, record it, commit
```

A transaction held open across the provider call would hold its locks for as long as the
network takes, which for a payment processor is unbounded. So nothing is held. The cost is
that the two commits are separate and a crash can land between them, and every state that
follows from that is designed for rather than hoped away.

The IN_FLIGHT boundary is the honest part and the part worth reading twice. It is committed
before the network call begins, not after. That makes its uncertainty one sided:

```text
found in ADMITTED   the provider was certainly never called. Safe to dispatch
found in IN_FLIGHT  the provider may or may not have been called. Ask it, never re-send
```

The alternative, writing IN_FLIGHT after the dispatch begins, would make ADMITTED the
ambiguous state, and ADMITTED is the state a recovery path most wants to be able to act on
without asking anybody. Choosing which state carries the doubt is the only real choice
available here, because there is no ordering of a database write and a network call that
removes it, and this puts the doubt where a query can resolve it and where blind action is
never tempting.

The outcome transaction takes its locks in the documented order and takes only what each
outcome touches. Success touches everything: the mandate, the checkout, the stock, the hold
and the attempt. A decline touches the hold and the attempt. An ambiguous result touches the
attempt alone, because the correct response to not knowing is to change as little as possible.

Beside dispatch is `reconcile`, which is the way out of UNKNOWN. It queries rather than
charges, records whatever it learns through exactly the same outcome logic, and leaves an
attempt where it is when the provider still does not know. Nothing calls it automatically:
retrying an ambiguous payment on a timer is how one gets charged twice.

It has one answer that ends an unresolved payment without any money having moved. A provider
that guarantees an identity was never executed has said something stronger than "I cannot find
it", and only that stronger statement releases the stock. A provider that merely has no record
right now leaves everything exactly where it is, for as long as that stays its answer. A
provider incapable of the stronger statement leaves an attempt that only
`agentrank_api.payments.recovery` can end, deliberately and with the risk written down.

Two writers can hold definitive answers about one attempt and disagree, when a response that
was thought lost finally arrives beside a query that was made while it was still on its way.
Whichever commits first is authoritative and stays authoritative. The other changes nothing,
appends `payment.outcome_conflict` and returns a `PaymentOutcome` carrying an `OutcomeConflict`.
It used to raise out of a repository, which reached a caller as a 500 for an operation that had
in fact behaved correctly. A settled payment is never rewritten in either direction.

What this module never does: retry an UNKNOWN attempt, treat a transport failure as a decline,
treat a missing record as a failure, release stock under an unresolved payment, decrement
inventory outside the success transaction, rewrite a terminal outcome because a later
observation disagrees, or claim exactly once execution. See docs/security.md.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.audit.models import ActorType
from agentrank_api.audit.repository import AuditRepository
from agentrank_api.benchmark.execution import BenchmarkRunCapability
from agentrank_api.benchmark.mutation import BenchmarkMutationGuard
from agentrank_api.checkout.models import CheckoutSession
from agentrank_api.checkout.repository import CheckoutRepository
from agentrank_api.errors import ConflictError, NotFoundError
from agentrank_api.inventory.models import InventoryReservation
from agentrank_api.inventory.repository import InventoryReservationRepository
from agentrank_api.inventory.service import InventoryReservationService, ReleaseReason
from agentrank_api.mandates.repository import MandateRepository
from agentrank_api.payments.admission import PAYMENT_RESOURCE
from agentrank_api.payments.models import OutcomeSource, PaymentAttempt, PaymentAttemptStatus
from agentrank_api.payments.provider import (
    PaymentInstruction,
    PaymentProvider,
    PaymentQuery,
    ProviderOutcome,
    ProviderQueryResult,
    ProviderRecord,
    ProviderResult,
)
from agentrank_api.payments.references import provider_operation_reference
from agentrank_api.payments.repository import PaymentAttemptRepository

PAYMENT_SUCCEEDED = "payment.succeeded"
PAYMENT_FAILED = "payment.failed"
PAYMENT_UNKNOWN = "payment.unknown"
PAYMENT_RECONCILED = "payment.reconciled"
PAYMENT_OUTCOME_CONFLICT = "payment.outcome_conflict"

# A payment outcome is reported by the provider, not decided by this application. Attributing
# it to the buyer would claim the buyer chose whether their card was declined.
OUTCOME_ACTOR = ActorType.PAYMENT_PROVIDER

# Every reason this application maps to a definitive decline. One value today, because a
# provider reporting no reason is still reporting a decline, and inventing a taxonomy before a
# real provider exists would be inventing a vendor's vocabulary.
UNSPECIFIED_DECLINE = "PROVIDER_DECLINED"

# The failure this application records when a provider guarantees that an operation never
# happened. Deliberately not a decline code: nothing was declined, because nothing was ever
# performed, and a trail that said CARD_DECLINED here would describe an event that did not
# occur. It is the only failure code this application invents rather than receives.
PROVIDER_NEVER_EXECUTED = "PROVIDER_NEVER_EXECUTED"


@dataclass(frozen=True, slots=True)
class OutcomeConflict:
    """A definitive observation that disagrees with the terminal state already recorded.

    Two writers can be resolving one attempt at the same time and see different things: an
    execution whose response finally arrived, and a reconciliation that queried while it was
    still on its way. One of them wins the lock, commits, and is authoritative. The other
    arrives holding an answer that contradicts a settled payment.

    The committed state was always safe, because a terminal attempt cannot be transitioned by
    the database. What was not safe was the response: the second writer used to raise a
    `ValueError` from inside a repository, which reaches a caller as a 500 for an operation
    that in fact did exactly the right thing. This is what it returns instead.

    `authoritative` is the state that stands and will keep standing. `observed` is what the
    losing writer was told. Nothing is decided from this; it is a record of a disagreement,
    for a reader who has to work out afterwards which of two answers a provider gave and when.
    """

    authoritative: PaymentAttemptStatus
    observed: ProviderOutcome


@dataclass(frozen=True, slots=True)
class PaymentOutcome:
    """What one execution or one reconciliation produced.

    `attempt` is the authoritative answer and always reflects committed state. `changed` says
    whether this call is what moved it, which is what makes a repeated reconciliation
    observably idempotent rather than merely harmless.

    `provider_called` is separate from both. A dispatch that found nothing to dispatch and a
    reconciliation that resolved from local state both leave it false, and a test asserting
    "the provider was called exactly once" needs to be able to see it.

    `conflict` is set only when this call observed something definitive that contradicts a
    terminal state somebody else had already recorded. It is None in every ordinary case,
    including the common one where a second writer observed the same outcome and simply had
    nothing to add.

    `provider_record` is what a query was told about the identity existing at all, and it is
    set by `reconcile` alone. A dispatch leaves it None because `execute` has no such concept:
    it performs an operation rather than asking about one. It is carried because "the provider
    has no record right now" and "the provider has the operation and has not decided it" leave
    an attempt in the same state and mean different things, and something eventually has to
    tell an operator which one they are waiting on. Nothing in this module branches on it.
    """

    attempt: PaymentAttempt
    changed: bool = False
    provider_called: bool = False
    conflict: OutcomeConflict | None = None
    provider_record: ProviderRecord | None = None


class PaymentExecutionService:
    """Dispatch an admitted payment and record what came back.

    The provider is injected rather than constructed. It is the one thing in this system that
    is not this system, and a service that built its own would be a service that cannot be
    tested against a decline.
    """

    def __init__(
        self,
        session: AsyncSession,
        provider: PaymentProvider,
        *,
        benchmark_capability: BenchmarkRunCapability | None = None,
    ) -> None:
        self._session = session
        self._provider = provider
        self._benchmark_capability = benchmark_capability
        self._mutation = BenchmarkMutationGuard(session)
        self._attempts = PaymentAttemptRepository(session)
        self._checkouts = CheckoutRepository(session)
        self._mandates = MandateRepository(session)
        self._reservations = InventoryReservationRepository(session)
        self._inventory = InventoryReservationService(session)
        self._audit = AuditRepository(session)

    async def dispatch(self, attempt_id: uuid.UUID) -> PaymentOutcome:
        """Send an admitted payment to the provider and record the answer.

        Only an ADMITTED attempt may be dispatched, and that is the whole safety property.
        Every other state either has an answer already or may have one arriving, and sending
        again would be the second charge this module exists to prevent. Each of those states
        raises a `ConflictError` naming itself rather than quietly doing nothing, because a
        caller that asked to pay and was silently ignored will ask again.

        No authorization is re-evaluated here, and that is deliberate rather than an omission.
        Admission was the authorization instant. A quote that expired, a mandate that lapsed or
        a mandate that was revoked after the attempt committed do not retract it: the purchase
        was authorized while all of them were valid, and a provider operation cannot be
        withdrawn by time passing. Re-checking here would mean refusing to complete payments
        the system had already promised, which is a worse failure than the one it would
        prevent.

        The provider call happens with no transaction open. The dispatch mark is committed
        first, so a crash after this point leaves an attempt that must be reconciled rather
        than one something might re-send.
        """
        await self._require_mutation_authority(attempt_id)
        attempt = await self._dispatchable(attempt_id)
        instruction = _instruction(attempt)

        # Deliberately outside every transaction. The commit above ended it, and nothing below
        # emits SQL until the provider has answered.
        result = await self._provider.execute(instruction)

        recorded = await self._record(attempt_id, result, source=OutcomeSource.EXECUTION)
        return PaymentOutcome(
            attempt=recorded.attempt,
            changed=recorded.changed,
            provider_called=True,
            conflict=recorded.conflict,
        )

    async def reconcile(self, attempt_id: uuid.UUID) -> PaymentOutcome:
        """Ask the provider what happened to an unresolved payment, and record the answer.

        The only way out of UNKNOWN, and the only way out of an IN_FLIGHT attempt whose process
        died before it could record anything. It queries; it never charges. A provider that
        created a payment because somebody asked about one would make this the most dangerous
        method in the system rather than the safest.

        Four answers and three behaviours. A definitive success or failure is recorded through
        exactly the same outcome logic a dispatch uses, so a payment resolved an hour later by a
        query consumes its stock or releases it in the same atomic transaction it would have. An
        indefinite answer leaves the attempt exactly where it was: the provider still does not
        know, and writing a state change to record that we asked would be noise in the one place
        noise is expensive.

        The fourth answer is the one that gives an unresolved payment a way to end. A provider
        that reports NEVER_EXECUTED is not saying it cannot find the operation; it is
        guaranteeing that none exists and that none can appear from the original dispatch. That
        is a definitive statement that no money moved, so it is recorded as a failure, the stock
        goes back and the checkout stays open for another try. A provider reporting merely that
        it has no record right now gets none of that, and the difference between the two is the
        whole reason `ProviderRecord` exists rather than a boolean.

        A definitive answer that contradicts a terminal state somebody else already recorded
        changes nothing and returns a `PaymentOutcome` carrying an `OutcomeConflict`. The
        authoritative state stands, in both directions, and the disagreement is recorded rather
        than resolved.

        Idempotent in two layers. A terminal attempt is returned without asking the provider
        anything, because there is nothing left to learn, and that is the cheap layer. An
        attempt that is still unresolved is queried again, and if two queries somehow both
        return a success, the outcome writers record one outcome, consume one unit and append
        one event, because each reports whether it is what changed the row. The second layer is
        what makes the first one a convenience rather than the guarantee.

        The query happens outside every transaction, exactly as `execute` does, and for the same
        reason.

        A `payment.reconciled` event is appended whenever a query was actually made, whatever it
        found. That is the record that somebody looked, which is worth having separately from
        what they learned: a payment that has been queried five times and is still unknown is a
        different operational fact from one nobody has asked about.
        """
        attempt = await self._attempts.get(attempt_id)
        if attempt is None:
            raise NotFoundError("payment_attempt", str(attempt_id))
        await self._mutation.require_allowed(
            attempt.merchant_id, capability=self._benchmark_capability
        )
        if attempt.is_terminal:
            # Nothing to learn. Deliberately not an error: a reconciliation sweep that meets an
            # attempt somebody else already resolved has done its job.
            await self._session.commit()
            return PaymentOutcome(attempt=attempt)

        if attempt.status is PaymentAttemptStatus.ADMITTED:
            # Certainly never dispatched, so the provider has never heard of this identity and
            # a query would tell us only that. Dispatching is the answer, and it is a different
            # decision from resolving an uncertain result.
            refusal = _not_reconcilable(attempt)
            await self._session.rollback()
            raise refusal

        question = _question(attempt)
        # Nothing is held while the provider answers. The read above opened a transaction and
        # this closes it before the network call.
        await self._session.commit()

        found = await self._provider.query(question)
        result, release_reason = _from_query(found)
        recorded = await self._record(
            attempt_id,
            result,
            source=OutcomeSource.RECONCILIATION,
            release_reason=release_reason,
        )
        await self._append_reconciled(attempt_id, found)
        return PaymentOutcome(
            attempt=recorded.attempt,
            changed=recorded.changed,
            provider_called=True,
            conflict=recorded.conflict,
            provider_record=found.record,
        )

    async def record_external_dispatch(self, attempt_id: uuid.UUID) -> PaymentAttempt:
        """Commit that a provider operation exists for this attempt, before asking what it did.

        The interactive counterpart of the dispatch mark, and it exists because an interactive
        checkout inverts who performs the payment. This application never sends one: a customer
        completes a provider hosted form and the callback is the first this side hears of it. By
        the time a signature verified callback arrives, "a provider request may have been
        dispatched" is not a hedge, it is certainly true, which is exactly what IN_FLIGHT means.

        Committed before the confirmation query, for the same reason `dispatch` commits before
        the network call. A crash after this point leaves an attempt that must be asked about
        rather than one something might settle autonomously, and the doubt stays on the side
        that cannot charge anybody twice.

        Tolerant of every state rather than refusing them, which is the opposite of
        `_dispatchable` and is deliberate. This does not authorize anything; it records a fact
        that has already happened somewhere else. An attempt already IN_FLIGHT is a repeated
        callback, an UNKNOWN one is a callback arriving after somebody asked, and a terminal one
        is a callback arriving after the payment settled. All three are ordinary, and none of
        them is a reason to refuse to look at what the provider now says.
        """
        attempt = await self._attempts.get_for_update(attempt_id)
        if attempt is None:
            raise NotFoundError("payment_attempt", str(attempt_id))
        await self._mutation.require_allowed(
            attempt.merchant_id, capability=self._benchmark_capability
        )
        if attempt.status is PaymentAttemptStatus.ADMITTED:
            await self._attempts.mark_in_flight(attempt)
        await self._session.commit()
        return attempt

    async def apply_provider_observation(
        self, attempt_id: uuid.UUID, result: ProviderResult, *, source: OutcomeSource
    ) -> PaymentOutcome:
        """Record an outcome learned somewhere other than a dispatch or a reconciliation query.

        The seam an interactive provider integration converges on, and it is deliberately a seam
        rather than a second implementation. Everything below it is the machinery a dispatch
        already uses: the same locks in the same documented order, the same atomic transaction
        that marks the attempt, marks the checkout PAID, consumes the hold, decrements the stock
        and appends the trail, the same idempotence when an outcome is already recorded, and the
        same typed conflict when two definitive answers disagree.

        A Razorpay Standard Checkout that succeeds and a fake provider that succeeds therefore
        produce identical business truth, because they produce it through identical code. There
        is no second definition of paid anywhere in this application.

        `source` is the caller's to state, because only the caller knows how it learned. An
        interactive checkout passes INTERACTIVE.
        """
        await self._require_mutation_authority(attempt_id)
        recorded = await self._record(attempt_id, result, source=source)
        return PaymentOutcome(
            attempt=recorded.attempt,
            changed=recorded.changed,
            provider_called=True,
            conflict=recorded.conflict,
        )

    async def _append_reconciled(self, attempt_id: uuid.UUID, found: ProviderQueryResult) -> None:
        """Record that somebody asked, and what the provider said when they did.

        Its own transaction rather than part of the outcome one. The outcome transaction is
        atomic with the state it writes, and an indefinite answer writes no state at all, so
        there is nothing for this to be atomic with. Recording the query separately also keeps
        the meaning clean: `payment.reconciled` says a query was made, and `payment.succeeded`
        beside it says what it resolved to.
        """
        attempt = await self._attempts.get(attempt_id)
        if attempt is None:
            # Not reachable: the outcome transaction just read and wrote this row.
            raise NotFoundError("payment_attempt", str(attempt_id))

        await self._audit.append(
            merchant_id=attempt.merchant_id,
            actor_type=OUTCOME_ACTOR,
            event_type=PAYMENT_RECONCILED,
            resource_type=PAYMENT_RESOURCE,
            resource_id=attempt.id,
            payload={
                "checkout_id": str(attempt.checkout_id),
                "provider_outcome": found.outcome.value,
                # What the provider had on file for this identity, which is a different fact
                # from what it said happened. ABSENT is not a failure and NEVER_EXECUTED is,
                # and a trail that recorded only a boolean could not tell them apart.
                "provider_record": found.record.value,
                "status": attempt.status.value,
            },
        )
        await self._session.commit()

    async def _require_mutation_authority(self, attempt_id: uuid.UUID) -> None:
        """Require the active benchmark's capability before a payment state transition.

        The execution kernel is shared by autonomous payments and the interactive provider
        bridge. Keeping this guard here prevents an external callback from bypassing the active
        benchmark-world claim without changing that bridge's public surface.
        """
        attempt = await self._attempts.get(attempt_id)
        if attempt is None:
            raise NotFoundError("payment_attempt", str(attempt_id))
        await self._mutation.require_allowed(
            attempt.merchant_id, capability=self._benchmark_capability
        )

    async def _dispatchable(self, attempt_id: uuid.UUID) -> PaymentAttempt:
        """Mark one attempt as dispatched and commit, or refuse to dispatch it.

        The attempt is locked before its status is read, so two dispatches cannot both see
        ADMITTED. The lock is released by the commit at the end, which is what lets the
        provider call happen without holding anything.
        """
        attempt = await self._attempts.get_for_update(attempt_id)
        if attempt is None:
            raise NotFoundError("payment_attempt", str(attempt_id))

        if attempt.status is not PaymentAttemptStatus.ADMITTED:
            # Built before the rollback, which expires every attribute on the row it names.
            refusal = _not_dispatchable(attempt)
            await self._session.rollback()
            raise refusal

        await self._attempts.mark_in_flight(attempt)
        await self._session.commit()
        return attempt

    async def _record(
        self,
        attempt_id: uuid.UUID,
        result: ProviderResult,
        *,
        source: OutcomeSource,
        release_reason: ReleaseReason = ReleaseReason.PAYMENT_DECLINED,
    ) -> PaymentOutcome:
        """Persist one provider answer, with every row the answer touches locked.

        A fresh transaction, opened after the provider has replied. The attempt is reloaded
        rather than reused: the object from the dispatch transaction was read before the
        network call, and between then and now anything could have happened to the row.

        `release_reason` is why the stock goes back, and it is a parameter because a definitive
        failure has more than one cause. A dispatch that is told no is a decline and defaults
        to one; a reconciliation that is told the operation never existed is not, and calling
        it a decline in the inventory trail would record a refusal that never happened.

        Locks are taken in the documented order and only as far down as the outcome reaches.
        See `agentrank_api.locking`.
        """
        if result.outcome is ProviderOutcome.SUCCEEDED:
            return await self._record_success(attempt_id, result, source=source)
        if result.outcome is ProviderOutcome.FAILED:
            return await self._record_failure(
                attempt_id, result, source=source, release_reason=release_reason
            )
        return await self._record_unknown(attempt_id, source=source)

    async def _record_success(
        self, attempt_id: uuid.UUID, result: ProviderResult, *, source: OutcomeSource
    ) -> PaymentOutcome:
        """The money moved, so everything that follows from that moves with it, atomically.

        Five things happen or none of them do: the attempt becomes SUCCEEDED with the
        provider's reference, the checkout becomes PAID, the hold becomes CONSUMED, the units
        leave `variant.inventory_quantity`, and the trail records both the payment and the
        consumption. A success recorded without its inventory consequence would be a sale the
        merchant never made, and stock decremented without a recorded success would be a
        shortage nobody can explain.

        Locks first and all of them, in order: the mandate, the checkout, the variant rows,
        the hold, the attempt. The mandate lock is what makes the single purchase rule hold
        under contention rather than only under the partial unique index, and the variant lock
        is what makes the decrement safe against a concurrent preparation.

        Idempotent. An attempt already SUCCEEDED is left exactly as it is and reported
        unchanged, so a reconciliation arriving after the execution recorded the same success
        writes no second outcome, consumes no second unit and appends no second event.

        An attempt already FAILED is a disagreement rather than a repeat, and it is answered
        rather than raised. The authoritative state stands, a `payment.outcome_conflict` event
        records what was observed against what was recorded, and the caller gets a typed
        result. Rewriting a settled payment because a later observation disagrees is the one
        thing that must never happen here.
        """
        attempt, checkout, reservation = await self._lock_outcome(attempt_id, with_stock=True)
        if attempt.status is PaymentAttemptStatus.SUCCEEDED:
            await self._session.commit()
            return PaymentOutcome(attempt=attempt)
        if attempt.is_terminal:
            return await self._record_conflict(attempt, ProviderOutcome.SUCCEEDED)

        reference = result.reference or _fallback_reference(attempt)
        if not await self._attempts.mark_succeeded(
            attempt, provider_reference=reference, source=source
        ):
            # Not reachable: the only status that returns False is SUCCEEDED, handled above.
            await self._session.commit()
            return PaymentOutcome(attempt=attempt)

        await self._checkouts.mark_paid(checkout)
        await self._inventory.consume(reservation)
        await self._append(attempt, PAYMENT_SUCCEEDED, reference=reference)
        await self._session.commit()
        return PaymentOutcome(attempt=attempt, changed=True)

    async def _record_failure(
        self,
        attempt_id: uuid.UUID,
        result: ProviderResult,
        *,
        source: OutcomeSource,
        release_reason: ReleaseReason = ReleaseReason.PAYMENT_DECLINED,
    ) -> PaymentOutcome:
        """A definitive failure, which specifically means no money moved.

        The attempt becomes FAILED with the reason, and the hold goes back on the shelf under
        `release_reason`. The checkout is left OPEN: a failed payment is a fact about an
        attempt, not about the quote, and the price is still good. Marking it failed would
        destroy an offer the merchant never withdrew and would make a later retry impossible.

        Two causes reach here and the trail keeps them apart. A provider that declined said no
        to something it did; a provider that guaranteed it never executed said there was
        nothing to say no to. Both mean the stock is safe to release and only one of them is a
        refusal.

        No variant lock. Releasing only ever frees capacity, so a concurrent preparation that
        has not seen it counts this stock as still held and refuses, which is conservative
        rather than wrong.

        An attempt already SUCCEEDED is a disagreement and is answered rather than raised, in
        exactly the way a success meeting a failure is. This is the expensive direction of the
        two: a failure observation arriving after a recorded success must not release stock
        that was already consumed for money that already moved.
        """
        attempt, _, reservation = await self._lock_outcome(attempt_id, with_stock=False)
        if attempt.status is PaymentAttemptStatus.FAILED:
            await self._session.commit()
            return PaymentOutcome(attempt=attempt)
        if attempt.is_terminal:
            return await self._record_conflict(attempt, ProviderOutcome.FAILED)

        failure_code = result.failure_code or UNSPECIFIED_DECLINE
        if not await self._attempts.mark_failed(
            attempt,
            failure_code=failure_code,
            source=source,
            provider_reference=result.reference,
        ):
            # Not reachable: the only status that returns False is FAILED, handled above.
            await self._session.commit()
            return PaymentOutcome(attempt=attempt)

        await self._inventory.release(reservation, reason=release_reason)
        await self._append(attempt, PAYMENT_FAILED, failure_code=failure_code)
        await self._session.commit()
        return PaymentOutcome(attempt=attempt, changed=True)

    async def _record_unknown(
        self, attempt_id: uuid.UUID, *, source: OutcomeSource
    ) -> PaymentOutcome:
        """Nobody knows, so as little as possible changes.

        The attempt becomes UNKNOWN and nothing else moves. The hold stays COMMITTED, so the
        stock stays off the shelf; the checkout stays OPEN, neither paid nor failed; no
        `resolved_at` is stamped, because nothing was resolved. Releasing the stock here would
        be the expensive mistake: the charge may have gone through, and the unit would be sold
        again while the buyer had already paid for it.

        Nothing retries. An UNKNOWN attempt leaves this state by being queried, and the only
        thing that queries is reconciliation, which a person or a scheduler asks for.

        Only the attempt is locked, because only the attempt changes.
        """
        attempt = await self._attempts.get_for_update(attempt_id)
        if attempt is None:
            raise NotFoundError("payment_attempt", str(attempt_id))
        if attempt.status is not PaymentAttemptStatus.IN_FLIGHT:
            # Already resolved by something else, or already recorded as unknown. Either way
            # this call has nothing to add. Deliberately not a conflict even when the attempt
            # is terminal: an ambiguous observation does not contradict a definitive one, it
            # only fails to add to it.
            await self._session.commit()
            return PaymentOutcome(attempt=attempt)

        await self._attempts.mark_unknown(attempt, source=source)
        await self._append(attempt, PAYMENT_UNKNOWN)
        await self._session.commit()
        return PaymentOutcome(attempt=attempt, changed=True)

    async def _record_conflict(
        self, attempt: PaymentAttempt, observed: ProviderOutcome
    ) -> PaymentOutcome:
        """Record that two definitive answers disagreed, and change nothing else.

        Called with every lock the outcome would have needed already held, and the attempt
        already terminal. Nothing about the payment moves: no status, no reservation, no
        stock, no checkout. The only write is the event, which is why appending it in this
        transaction costs nothing and keeps it atomic with the read that established the
        disagreement.

        The authoritative state is whichever one committed first, and it is never revisited.
        A success is not rewritten into a failure because a stale query said so, and a failure
        is not rewritten into a success because a late response arrived. Which observation is
        true is a question about a provider's records, and answering it by mutating this row
        would destroy the evidence needed to answer it at all.
        """
        conflict = OutcomeConflict(authoritative=attempt.status, observed=observed)
        await self._audit.append(
            merchant_id=attempt.merchant_id,
            actor_type=OUTCOME_ACTOR,
            event_type=PAYMENT_OUTCOME_CONFLICT,
            resource_type=PAYMENT_RESOURCE,
            resource_id=attempt.id,
            payload={
                "checkout_id": str(attempt.checkout_id),
                # What stands, and what was observed against it. Both, because either alone
                # is half the fact.
                "authoritative_status": conflict.authoritative.value,
                "observed_outcome": conflict.observed.value,
                "failure_code": attempt.failure_code,
                "provider_reference": attempt.provider_reference,
            },
        )
        await self._session.commit()
        return PaymentOutcome(attempt=attempt, conflict=conflict)

    async def _lock_outcome(
        self, attempt_id: uuid.UUID, *, with_stock: bool
    ) -> tuple[PaymentAttempt, CheckoutSession, InventoryReservation]:
        """Take every lock this outcome needs, in the order `agentrank_api.locking` names.

        The first read is unlocked and is used for one thing, which is learning which mandate,
        checkout and reservation this attempt names. All three columns are immutable at the
        database, so reading them without a lock cannot be wrong. Everything the decision then
        rests on is read again under the lock.

        `with_stock` is what separates a success from a decline. A success has to decrement the
        variant rows, so it locks them; a decline only frees capacity, and taking the shelf
        lock to give stock back would make two payments that touch the same variant queue for
        no reason.
        """
        found = await self._attempts.get(attempt_id)
        if found is None:
            raise NotFoundError("payment_attempt", str(attempt_id))

        # The merchant comes from the attempt rather than from a caller, and there is nowhere
        # else it could come from: recording an outcome is reached from the operator command
        # line as well as from a buyer's request, and only one of those has an authenticated
        # merchant. It is not a check either. The composite foreign keys already tie the
        # attempt and the mandate to one merchant, so this selects the same row an unscoped
        # read would have.
        await self._mandates.get_for_update(found.mandate_id, merchant_id=found.merchant_id)
        checkout = await self._checkouts.get_for_update(
            found.checkout_id, merchant_id=found.merchant_id
        )
        if checkout is None:
            # Not reachable through the schema: the foreign key onto the checkout is RESTRICT.
            raise NotFoundError("checkout", str(found.checkout_id))

        if with_stock:
            await self._inventory.lock_variants_for(checkout)

        reservation = await self._reservations.get_for_update(found.reservation_id)
        if reservation is None:
            # Not reachable through the schema: the foreign key onto the reservation is
            # RESTRICT.
            raise NotFoundError("inventory_reservation", str(found.reservation_id))

        attempt = await self._attempts.get_for_update(attempt_id)
        if attempt is None:
            raise NotFoundError("payment_attempt", str(attempt_id))
        return attempt, checkout, reservation

    async def _append(
        self,
        attempt: PaymentAttempt,
        event_type: str,
        *,
        reference: str | None = None,
        failure_code: str | None = None,
    ) -> None:
        """Record what the provider said, in this transaction or not at all.

        `merchant_id` comes from the attempt, never from anything a caller sent. The amount and
        the currency are restated because they are what actually moved, and a trail that cannot
        answer "how much was this" without joining to a quote stops answering it once the quote
        is archived.
        """
        payload: dict[str, object] = {
            "checkout_id": str(attempt.checkout_id),
            "mandate_id": str(attempt.mandate_id),
            "amount_minor": attempt.amount_minor,
            "currency": attempt.currency,
            "status": attempt.status.value,
        }
        if reference is not None:
            payload["provider_reference"] = reference
        if failure_code is not None:
            payload["failure_code"] = failure_code

        await self._audit.append(
            merchant_id=attempt.merchant_id,
            actor_type=OUTCOME_ACTOR,
            event_type=event_type,
            resource_type=PAYMENT_RESOURCE,
            resource_id=attempt.id,
            payload=payload,
        )


def _instruction(attempt: PaymentAttempt) -> PaymentInstruction:
    """Everything the provider is told, read off the attempt and off nothing else.

    Not off the checkout, not off the mandate, not through a relationship. The amount and the
    currency on this row are what was authorized, held there by a composite foreign key onto an
    immutable quote, and reading them from anywhere else would be reading a number that could
    have moved since.

    The identity is derived rather than forwarded. `operation_reference` comes from the merchant
    and the attempt, both immutable on this row, so it is unique inside one provider account
    however many merchants share it. The caller's key travels beside it for correlation and is
    never what a provider keys on. See `agentrank_api.payments.references`.
    """
    return PaymentInstruction(
        attempt_id=attempt.id,
        operation_reference=provider_operation_reference(attempt.merchant_id, attempt.id),
        idempotency_key=attempt.idempotency_key,
        amount_minor=attempt.amount_minor,
        currency=attempt.currency,
        merchant_reference=str(attempt.merchant_id),
        checkout_reference=str(attempt.checkout_id),
    )


def _question(attempt: PaymentAttempt) -> PaymentQuery:
    """What the provider is asked about this attempt, read off the attempt itself.

    `dispatched_at` is here because a provider whose visibility guarantee is a duration cannot
    evaluate it without knowing when the clock started, and this application is the only side
    that knows. It carries the fact and never the duration: how long a provider needs before an
    empty answer becomes final is that provider's business.

    The identity is the same derived reference `execute` was given, recomputed rather than
    stored. Both inputs are immutable, so recomputing cannot produce a different answer than
    the dispatch did.
    """
    if attempt.dispatched_at is None:
        # Not reachable. ADMITTED is the only status with no dispatch instant, a check
        # constraint keeps the two in agreement, and reconciliation refuses ADMITTED above.
        raise ValueError(f"payment attempt {attempt.id} has no dispatch instant to query from")
    return PaymentQuery(
        operation_reference=provider_operation_reference(attempt.merchant_id, attempt.id),
        idempotency_key=attempt.idempotency_key,
        dispatched_at=attempt.dispatched_at,
    )


def _from_query(found: ProviderQueryResult) -> tuple[ProviderResult, ReleaseReason]:
    """Turn what a provider knows into an outcome this application can record.

    One translation and it is the important one. A provider that guarantees an operation never
    existed has stated definitively that no money moved, which is a failure even though it
    never reported one, so this application supplies the outcome and the failure code itself.
    That is the only place a failure is invented rather than received, and it is invented from
    a guarantee rather than from an absence.

    Everything else passes through untouched, including ABSENT, which stays UNKNOWN and leaves
    the attempt exactly where it is.
    """
    if found.record is ProviderRecord.NEVER_EXECUTED:
        return (
            ProviderResult(outcome=ProviderOutcome.FAILED, failure_code=PROVIDER_NEVER_EXECUTED),
            ReleaseReason.PAYMENT_NOT_EXECUTED,
        )
    return (
        ProviderResult(
            outcome=found.outcome, reference=found.reference, failure_code=found.failure_code
        ),
        ReleaseReason.PAYMENT_DECLINED,
    )


def _fallback_reference(attempt: PaymentAttempt) -> str:
    """A reference for a provider that reported success without one.

    A success has to be findable afterwards and the database refuses a blank reference, so
    something has to be stored. This is deliberately obviously local: it names the attempt and
    says where it came from, so nobody looking at it later mistakes it for something a
    processor issued.
    """
    return f"unreferenced:{attempt.id}"


def _not_reconcilable(attempt: PaymentAttempt) -> ConflictError:
    """The refusal for an attempt that has nothing to reconcile.

    Only ADMITTED reaches here, and the answer is specific rather than generic: this payment has
    provably never been sent, so what it needs is a dispatch and not a query.
    """
    return ConflictError(
        "payment_not_dispatched",
        f"payment attempt {attempt.id} has never been dispatched and has nothing to reconcile",
        resource="payment_attempt",
        identifier=str(attempt.id),
    )


def _not_dispatchable(attempt: PaymentAttempt) -> ConflictError:
    """The refusal that names why this attempt cannot be sent to a provider.

    Four states and four different next moves. A terminal attempt is finished. An IN_FLIGHT one
    may be at the provider right now. An UNKNOWN one has to be reconciled, and reconciling is
    the one thing a caller can usefully do about it. Collapsing them into one code would leave
    a caller with an error and no idea which.
    """
    if attempt.status is PaymentAttemptStatus.SUCCEEDED:
        return ConflictError(
            "payment_already_succeeded",
            f"payment attempt {attempt.id} has already succeeded",
            resource="payment_attempt",
            identifier=str(attempt.id),
        )
    if attempt.status is PaymentAttemptStatus.FAILED:
        return ConflictError(
            "payment_already_failed",
            f"payment attempt {attempt.id} has already been declined",
            resource="payment_attempt",
            identifier=str(attempt.id),
        )
    if attempt.status is PaymentAttemptStatus.UNKNOWN:
        return ConflictError(
            "payment_unresolved",
            f"payment attempt {attempt.id} has an unresolved result and must be reconciled",
            resource="payment_attempt",
            identifier=str(attempt.id),
        )
    return ConflictError(
        "payment_in_progress",
        f"payment attempt {attempt.id} has already been dispatched",
        resource="payment_attempt",
        identifier=str(attempt.id),
    )
