"""Ending an unresolved payment that no provider will ever settle, deliberately.

Reconciliation is the honest way out of UNKNOWN and it needs the provider's cooperation. A
provider that reports NEVER_EXECUTED ends the attempt with a guarantee behind it. A provider
that only ever reports ABSENT ends nothing, ever, and the attempt holds a merchant's stock and
a buyer's mandate for as long as that stays true.

Not every processor can prove durable absence. Some have no documented visibility guarantee at
all, some are unreachable for longer than anyone can wait, and some simply lose an operation.
For those, somebody has to decide, and this module is where that decision is made, written down
and made atomic.

```text
provider says NEVER_EXECUTED   ->  reconcile      a guarantee, no money moved
nobody can say anything        ->  abandon        a judgement, and it may be wrong
```

The second line is the whole reason this file is separate from `execution.py`. What happens to
the rows is the same shape, and what it means is not. Abandonment is not proof that the payment
failed. It is a decision that waiting has stopped being worth more than the stock, taken with
the residual risk accepted, and the residual risk is real: the provider may yet reveal that the
money moved, and by then the units are back on the shelf and possibly sold to somebody else.

That risk is the reason for every constraint below. Only UNKNOWN is eligible, so the provider
has been asked at least once before anybody gives up on it. A machine readable reason is
required, so the trail says which kind of giving up this was. The event is `payment.abandoned`
rather than `payment.failed`, so no reader ever mistakes it for a provider confirmed outcome,
and `failure_code` on the attempt is `OPERATOR_ABANDONED` for the same reason.

There is no HTTP endpoint here and there must not be one until there is an authenticated
operator to attach to it. Nothing unauthenticated may terminalize a payment: it would be a way
for anybody who can reach the process to release stock that a real charge may be standing
behind. This is an internal service, called by trusted tooling, and the tooling is a later
phase.

A future real provider integration should prefer provider confirmed final absence whenever it
can be obtained. This is what is left when it cannot.
"""

import uuid
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.audit.models import ActorType
from agentrank_api.audit.repository import AuditRepository
from agentrank_api.benchmark.execution import BenchmarkRunCapability
from agentrank_api.benchmark.mutation import BenchmarkMutationGuard
from agentrank_api.errors import ConflictError, NotFoundError
from agentrank_api.inventory.repository import InventoryReservationRepository
from agentrank_api.inventory.service import InventoryReservationService, ReleaseReason
from agentrank_api.payments.admission import PAYMENT_RESOURCE
from agentrank_api.payments.execution import PaymentOutcome
from agentrank_api.payments.models import OutcomeSource, PaymentAttempt, PaymentAttemptStatus
from agentrank_api.payments.repository import PaymentAttemptRepository

PAYMENT_ABANDONED = "payment.abandoned"

# The failure code an abandoned attempt carries. Deliberately says who decided rather than what
# happened, because nobody knows what happened and a code claiming otherwise would be the lie
# this whole module is arranged to avoid.
OPERATOR_ABANDONED = "OPERATOR_ABANDONED"

# Abandonment is this application acting on an operator's instruction. It is not the provider,
# which has confirmed nothing, and it is not the buyer, who did not choose this. SYSTEM is the
# honest one of the three available, and it names a role rather than an identity for the same
# reason every other actor here does: nothing authenticates anybody yet. See
# SECURITY.md.
ABANDONMENT_ACTOR = ActorType.SYSTEM

# The states an abandonment may act on. One, and the narrowness is the point: UNKNOWN is the
# state an attempt reaches by having been queried, so requiring it means the provider has been
# asked at least once before anybody decides to stop asking.
ABANDONABLE_STATUSES: tuple[PaymentAttemptStatus, ...] = (PaymentAttemptStatus.UNKNOWN,)

# How long an operator's free text reference may be. Long enough for a ticket identifier and a
# few words of context, short enough that nobody mistakes the field for a place to write a
# report and short enough that it cannot bury the structured reason beside it.
MAX_OPERATOR_NOTE_LENGTH = 200


class AbandonmentReason(StrEnum):
    """Why an operator decided an unresolved payment would never be resolved.

    An enumeration rather than prose, so the trail stays answerable by a machine and so that
    "we gave up" cannot be recorded as a sentence nobody can aggregate. Three values, and each
    describes a different thing that was true about the provider rather than about the payment.

    PROVIDER_CANNOT_CONFIRM
        The provider answers, has no record, and offers no guarantee that would ever make that
        absence final. Waiting longer changes nothing, because there is nothing left to learn.

    PROVIDER_UNREACHABLE
        The provider cannot be queried at all, for long enough that holding the stock has
        stopped being reasonable. Different from the first: the answer might exist and cannot
        be obtained.

    OPERATOR_DECISION
        Neither of the above, and somebody decided anyway with the risk accepted. The value
        that exists so nobody is tempted to misuse one of the other two.
    """

    PROVIDER_CANNOT_CONFIRM = "provider_cannot_confirm"
    PROVIDER_UNREACHABLE = "provider_unreachable"
    OPERATOR_DECISION = "operator_decision"


def validate_operator_note(note: str) -> str:
    """Check a short human reference and return it trimmed, or refuse it.

    Beside the reason rather than instead of it. The reason stays a required enumeration and
    nothing here weakens that: this cannot be the only thing recorded, it cannot be aggregated
    over, and no code branches on it. What it is for is the thing an enumeration structurally
    cannot carry, which is which incident this particular decision belonged to. `incident-123`
    or `support-ticket-456` is enough to reconstruct a judgement call a month later.

    Bounded and single line. Long enough for a reference and a few words, short enough that it
    cannot become a report, and free of control characters so that a value written here cannot
    reformat a terminal or a log line that prints it back.

    Nothing scans it, and it must never carry a secret. A key, a card number or a password put
    here lands in an append only table that refuses UPDATE and DELETE, which is the worst
    possible place for one. See SECURITY.md.
    """
    trimmed = note.strip()
    if not trimmed:
        raise ValueError("an operator note cannot be blank")
    if len(trimmed) > MAX_OPERATOR_NOTE_LENGTH:
        raise ValueError(
            f"an operator note is at most {MAX_OPERATOR_NOTE_LENGTH} characters, got {len(trimmed)}"
        )
    if not trimmed.isprintable():
        raise ValueError("an operator note must be a single line of printable characters")
    return trimmed


class PaymentRecoveryService:
    """Terminalize an unresolved payment by decision rather than by evidence.

    No provider. This service cannot call one and does not hold one, which is not an oversight:
    an operation that ends a payment without asking anybody should be structurally incapable of
    asking, so that nothing in it can quietly become a second dispatch.
    """

    def __init__(
        self, session: AsyncSession, *, benchmark_capability: BenchmarkRunCapability | None = None
    ) -> None:
        self._session = session
        self._benchmark_capability = benchmark_capability
        self._mutation = BenchmarkMutationGuard(session)
        self._attempts = PaymentAttemptRepository(session)
        self._reservations = InventoryReservationRepository(session)
        self._inventory = InventoryReservationService(session)
        self._audit = AuditRepository(session)

    async def abandon_payment_attempt(
        self, attempt_id: uuid.UUID, *, reason: AbandonmentReason, note: str | None = None
    ) -> PaymentOutcome:
        """Give up on one unresolved payment, atomically, and say so in the trail.

        One transaction. The attempt becomes FAILED with `OPERATOR_ABANDONED`, the hold is
        released as `payment_abandoned`, `payment.abandoned` is appended with the reason, and
        the checkout is left OPEN with `variant.inventory_quantity` untouched. All of it or
        none of it.

        The checkout stays open on purpose. Abandoning a payment says nothing about the quote,
        and a buyer who still wants the goods may pay for them again through the ordinary path:
        a fresh reservation and a new identity. What abandonment frees is the mandate, which
        was being held by a non terminal attempt.

        Only an UNKNOWN attempt is eligible. An ADMITTED one has provably never been sent and
        should be dispatched or left alone; an IN_FLIGHT one has never been queried and should
        be reconciled first, because giving up before asking is not a recovery, it is a guess.
        Both are refused by name so the caller is told which.

        Idempotent for a repeat of itself and refused for anything else. An attempt already
        abandoned is returned unchanged, with `changed` false and no second release, because a
        tool that lost its answer and asked again has not made a second decision. An attempt
        that reached a terminal state some other way is a different fact and is refused rather
        than quietly reported as abandoned.

        `provider_called` is always false on the result, and that is the honest headline: no
        provider was involved in this and none confirmed anything.

        `note` is an optional short human reference, recorded beside the reason and never
        instead of it. It exists because a three value enumeration cannot say which incident a
        judgement belonged to, and it is deliberately something nothing aggregates over and
        nothing branches on. It must never carry a secret: this lands in an append only table.
        """
        recorded_note = None if note is None else validate_operator_note(note)
        attempt = await self._attempts.get(attempt_id)
        if attempt is None:
            raise NotFoundError(PAYMENT_RESOURCE, str(attempt_id))
        await self._mutation.require_allowed(
            attempt.merchant_id, capability=self._benchmark_capability
        )

        # The locks the release will need, in the documented order and no further down than it
        # reaches. No mandate and no checkout: neither changes here. No variant rows: releasing
        # only ever frees capacity, so a concurrent preparation that has not seen it counts
        # this stock as still held and refuses, which is conservative rather than wrong.
        # See agentrank_api.locking.
        reservation = await self._reservations.get_for_update(attempt.reservation_id)
        if reservation is None:
            # Not reachable through the schema: the foreign key onto the reservation is
            # RESTRICT.
            raise NotFoundError("inventory_reservation", str(attempt.reservation_id))

        locked = await self._attempts.get_for_update(attempt_id)
        if locked is None:
            raise NotFoundError(PAYMENT_RESOURCE, str(attempt_id))

        if _already_abandoned(locked):
            # The same decision, asked again. Nothing was written and the commit closes the
            # read.
            await self._session.commit()
            return PaymentOutcome(attempt=locked)

        if locked.status not in ABANDONABLE_STATUSES:
            # Built before the rollback, which expires every attribute on the row it names.
            refusal = _not_abandonable(locked)
            await self._session.rollback()
            raise refusal

        await self._attempts.mark_failed(
            locked, failure_code=OPERATOR_ABANDONED, source=OutcomeSource.OPERATOR
        )
        await self._inventory.release(reservation, reason=ReleaseReason.PAYMENT_ABANDONED)
        await self._audit.append(
            merchant_id=locked.merchant_id,
            actor_type=ABANDONMENT_ACTOR,
            event_type=PAYMENT_ABANDONED,
            resource_type=PAYMENT_RESOURCE,
            resource_id=locked.id,
            payload=_abandoned_payload(locked, reason, recorded_note),
        )
        await self._session.commit()
        return PaymentOutcome(attempt=locked, changed=True)


def _already_abandoned(attempt: PaymentAttempt) -> bool:
    """Whether this exact decision has already been recorded on this attempt."""
    return (
        attempt.status is PaymentAttemptStatus.FAILED and attempt.failure_code == OPERATOR_ABANDONED
    )


def _abandoned_payload(
    attempt: PaymentAttempt, reason: AbandonmentReason, note: str | None
) -> dict[str, object]:
    """What was given up on, and the fact that nobody confirmed anything.

    `provider_confirmed` is stated rather than implied. Every other terminal payment event in
    this system is a provider's report, and a reader skimming the trail has to be able to see
    at a glance that this one is not. `residual_risk` says what may still be true, in the
    record itself rather than only in a document: the money may have moved, and the stock has
    gone back anyway.

    `operator_note` is always present and is null when none was given, so every abandonment
    event has one shape and a reader never has to tell a missing key from a missing note. It
    sits after the structured reason rather than beside it, which is the order it should be
    read in: the reason is the fact, and the note is the context somebody added to it.
    """
    return {
        "checkout_id": str(attempt.checkout_id),
        "mandate_id": str(attempt.mandate_id),
        "reservation_id": str(attempt.reservation_id),
        "amount_minor": attempt.amount_minor,
        "currency": attempt.currency,
        "status": attempt.status.value,
        "failure_code": attempt.failure_code,
        "reason": reason.value,
        "operator_note": note,
        "provider_confirmed": False,
        "residual_risk": "the provider may later reveal that this payment succeeded",
    }


def _not_abandonable(attempt: PaymentAttempt) -> ConflictError:
    """The refusal that names why this attempt may not be given up on.

    Four states and four different next moves, exactly as the dispatch refusal has. An
    ADMITTED attempt was never sent and needs a dispatch. An IN_FLIGHT one has never been
    asked about and needs a reconciliation first, because abandoning something nobody has
    queried is a guess rather than a recovery. A terminal one already has an answer, and
    overwriting it would be rewriting a settled payment.
    """
    if attempt.status is PaymentAttemptStatus.ADMITTED:
        return ConflictError(
            "payment_not_dispatched",
            f"payment attempt {attempt.id} has never been dispatched and has nothing to abandon",
            resource=PAYMENT_RESOURCE,
            identifier=str(attempt.id),
        )
    if attempt.status is PaymentAttemptStatus.IN_FLIGHT:
        return ConflictError(
            "payment_not_reconciled",
            f"payment attempt {attempt.id} has never been queried and must be reconciled"
            " before it can be abandoned",
            resource=PAYMENT_RESOURCE,
            identifier=str(attempt.id),
        )
    if attempt.status is PaymentAttemptStatus.SUCCEEDED:
        return ConflictError(
            "payment_already_succeeded",
            f"payment attempt {attempt.id} has already succeeded",
            resource=PAYMENT_RESOURCE,
            identifier=str(attempt.id),
        )
    return ConflictError(
        "payment_already_failed",
        f"payment attempt {attempt.id} has already been resolved as failed",
        resource=PAYMENT_RESOURCE,
        identifier=str(attempt.id),
    )
