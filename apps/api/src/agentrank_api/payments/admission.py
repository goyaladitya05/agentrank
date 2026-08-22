"""The one transaction that decides a payment may happen, and records that it decided.

Admission is the load bearing moment of this phase. Everything a provider call rests on is
established here, while every relevant row is locked, and the `PaymentAttempt` that proves it
is written before those locks are released. A provider may be invoked only if a committed
attempt exists, and a committed attempt exists only if all of this was true at once:

```text
checkout OPEN and unexpired
mandate ACTIVE and inside its validity window
an authoritative IntentConstraintSet exists
the financial gate allows
the semantic gate allows
a reservation is effective and belongs to this checkout
merchant ownership matches across every resource
no successful payment has already consumed the mandate
no other non terminal attempt exists under the mandate
the amount and the currency are frozen
```

The shape of this module is a reaction to the shape it must not have. The obvious version
calls `prepare_execution`, gets a readiness answer, and then creates an attempt. That is two
transactions: preparation commits, its locks are released, and a mandate revocation or a
checkout cancellation can commit in the gap before the attempt is written. The attempt would
then prove something that had stopped being true. So this reuses the locked loading and
revalidation directly, through `CheckoutExecutionService.authorize_under_locks`, which
commits nothing and hands its locks over, and the attempt is written inside the same
transaction that decided admission.

What admission is not is a reservation. It requires an effective one and refuses without it,
because holding stock is execution preparation's job and it is a decision about inventory
rather than about money. Doing both here would mean a payment could take stock off a shelf as
a side effect, which is the kind of thing that should need its own call.

After this commits, the attempt stands on its own. A later revocation, cancellation or expiry
does not retroactively invalidate it: admission was the authorization instant, and an
operation authorized while everything was valid stays authorized while it completes. What
those later events do is prevent the next admission.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.audit.models import ActorType
from agentrank_api.audit.repository import AuditRepository
from agentrank_api.benchmark.execution import BenchmarkRunCapability
from agentrank_api.benchmark.mutation import BenchmarkMutationGuard
from agentrank_api.checkout.execution import CheckoutExecutionService, LockedAuthorization
from agentrank_api.checkout.execution_authorization import CheckoutExecutionAuthorization
from agentrank_api.checkout.models import CheckoutSession, CheckoutStatus
from agentrank_api.conflicts import translated_conflicts
from agentrank_api.inventory.models import InventoryReservation
from agentrank_api.inventory.repository import InventoryReservationRepository
from agentrank_api.inventory.rules import is_effective
from agentrank_api.inventory.service import InventoryReservationService
from agentrank_api.payments.models import PaymentAttempt
from agentrank_api.payments.repository import PaymentAttemptRepository
from agentrank_api.payments.rules import validate_idempotency_key

PAYMENT_RESOURCE = "payment_attempt"
PAYMENT_ADMITTED = "payment.admitted"

# A payment is admitted because a buyer asked to pay. The provider has not been involved yet
# and has said nothing, so attributing this to it would be attributing a decision to somebody
# who has not made one. This names a role and not a person. The credential that authorized the
# request is recorded beside it, which says which merchant integration asked and deliberately
# does not claim to say who was holding the key.
ADMISSION_ACTOR = ActorType.BUYER


class AdmissionRefusal(StrEnum):
    """Why a payment was not admitted.

    Machine readable identifiers, not prose, for the same reason every other code here is
    one. A buyer agent has to tell "you may not buy this" from "somebody is already paying
    for it" from "that mandate has been spent" without reading English, because the three
    call for completely different next moves: fix the request, wait, and stop.

    `PAYMENT_IN_PROGRESS` and `MANDATE_PAYMENT_IN_PROGRESS` are kept apart deliberately. The
    first says this checkout already has a payment going and a retry with the same key would
    have joined it. The second says a different checkout under the same mandate does, which
    is not something a caller can fix by changing anything about this request.
    """

    NOT_AUTHORIZED = "payment_not_authorized"
    RESERVATION_MISSING = "reservation_missing"
    RESERVATION_EXPIRED = "reservation_expired"
    MANDATE_ALREADY_CONSUMED = "mandate_already_consumed"
    CHECKOUT_ALREADY_PAID = "checkout_already_paid"
    PAYMENT_IN_PROGRESS = "payment_in_progress"
    MANDATE_PAYMENT_IN_PROGRESS = "mandate_payment_in_progress"


@dataclass(frozen=True, slots=True)
class PaymentAdmission:
    """Whether a payment may now be attempted, and everything that decided it.

    `admitted` is derived rather than stored, so a result carrying a refusal cannot also carry
    an attempt anybody may dispatch.

    `created` distinguishes an attempt this call wrote from one that already existed under the
    same identity. That distinction is the whole of idempotency being observable rather than
    merely harmless: a duplicate request returns `created=False` and the original attempt, and
    a test can tell the two apart.

    `authorization` is what the two gates said during this call. When `created` is False it
    decided nothing, and it may well be a denial: an attempt admitted an hour ago against a
    quote that has since expired is still a valid attempt, and reporting today's gate result
    beside it is more useful than hiding it. What authorized the attempt is the state at its
    own `created_at`, not this.

    Two instants, as everywhere else here. `evaluated_at` is what the gates were decided
    against and is injectable so those decisions are reproducible. `admitted_at` is a reading
    of the real clock taken once every lock was held, and it is None when admission refused
    before reaching that point.
    """

    checkout_id: uuid.UUID
    evaluated_at: datetime
    authorization: CheckoutExecutionAuthorization
    admitted_at: datetime | None = None
    attempt: PaymentAttempt | None = None
    created: bool = False
    refusal: AdmissionRefusal | None = None

    @property
    def admitted(self) -> bool:
        return self.attempt is not None and self.refusal is None


class PaymentAdmissionService:
    """Admit one payment, in one transaction, or refuse it having written nothing."""

    def __init__(
        self, session: AsyncSession, *, benchmark_capability: BenchmarkRunCapability | None = None
    ) -> None:
        self._session = session
        self._benchmark_capability = benchmark_capability
        self._mutation = BenchmarkMutationGuard(session)
        self._execution = CheckoutExecutionService(
            session, benchmark_capability=benchmark_capability
        )
        self._attempts = PaymentAttemptRepository(session)
        self._reservations = InventoryReservationRepository(session)
        self._inventory = InventoryReservationService(session)
        self._audit = AuditRepository(session)

    async def admit_payment(
        self,
        checkout_id: uuid.UUID,
        *,
        merchant_id: uuid.UUID,
        idempotency_key: str,
        credential_id: uuid.UUID | None = None,
        at: datetime | None = None,
    ) -> PaymentAdmission:
        """Decide whether this payment may happen, and record the decision before returning.

        One transaction from the first lock to the commit. The order is fixed:

        1. lock the mandate, the checkout and the variant rows, and decide both gates twice
           against two instants, the second read after the last lock is held
        2. look for an attempt under this identity, and return it if there is one
        3. require a reservation that is still effective and belongs to this checkout
        4. require that no successful payment has consumed the mandate or paid the checkout
        5. require that no other non terminal attempt exists under the mandate
        6. write the attempt with the amount and the currency frozen from the quote
        7. bind the reservation to it
        8. append `payment.admitted`
        9. commit

        Step 2 before step 1's result is examined, and that ordering is deliberate. A repeat
        of a request that already produced an attempt returns that attempt whatever the gates
        say now, because admission already happened and a quote expiring afterwards does not
        un-admit it. Checking the gates first and refusing would tell a retrying caller that
        its payment does not exist, which is the single most dangerous wrong answer available
        here.

        A refusal writes nothing and rolls back, so a payment that may not proceed never binds
        a merchant's stock and never leaves a row behind.

        Nothing is dispatched. This returns an ADMITTED attempt, which means a provider may
        now be called and has not been.

        `merchant_id` is the authenticated merchant and it is required. Step 1 fails to find a
        quote that belongs to anybody else, so a cross merchant payment raises before step 2 is
        reached, which means before an idempotency key can be matched and long before a provider
        could be involved. That ordering is what makes the refusal cost a provider nothing: the
        denial happens in the first statement of the first transaction.

        The idempotency key is scoped by the checkout, and the checkout is scoped by the
        merchant, so one merchant's key cannot resolve to another merchant's payment. Two
        merchants may use the same key string against their own checkouts and they are two
        payments, exactly as they were before authentication existed.
        """
        validate_idempotency_key(idempotency_key)

        await self._mutation.require_allowed(merchant_id, capability=self._benchmark_capability)
        authorization = await self._execution.authorize_under_locks(
            checkout_id, merchant_id=merchant_id, at=at
        )
        existing = await self._attempts.get_by_identity(
            checkout_id=checkout_id, idempotency_key=idempotency_key
        )
        if existing is not None:
            # The same logical operation, asked again. Nothing was written, and the commit
            # only closes the read.
            await self._session.commit()
            return PaymentAdmission(
                checkout_id=checkout_id,
                evaluated_at=authorization.evaluated_at,
                admitted_at=authorization.admitted_at,
                authorization=authorization.authorization,
                attempt=existing,
                created=False,
            )

        if not authorization.admitted:
            # Read before the rollback: it expires the ORM objects this carries.
            paid = authorization.checkout.status is CheckoutStatus.PAID
            return await self._refuse(
                authorization,
                AdmissionRefusal.CHECKOUT_ALREADY_PAID if paid else AdmissionRefusal.NOT_AUTHORIZED,
            )

        checkout = authorization.checkout
        admitted_at = authorization.admission_instant()

        reservation = await self._reservations.get_holding_for_checkout(checkout_id)
        if reservation is None:
            # Deliberately not "reserve one now". Holding stock is execution preparation's
            # decision and it belongs to the call that asks for it, not to a side effect of
            # asking to pay.
            return await self._refuse(authorization, AdmissionRefusal.RESERVATION_MISSING)
        if not is_effective(reservation, at=authorization.evaluated_at) or not is_effective(
            reservation, at=admitted_at
        ):
            # Both instants, which mirrors what the financial gate already does. The
            # accounting instant is what the rest of the decision was made against, and the
            # admission instant is the reading taken once nothing could block again; a hold
            # has to be standing at both, and requiring the stricter of the two is the answer
            # that cannot be wrong in either direction.
            #
            # An ACTIVE hold that has already lapsed is refused rather than renewed or
            # committed. Committing it would take a claim that stopped holding stock and make
            # it permanent, which is how stock gets promised to two buyers.
            return await self._refuse(authorization, AdmissionRefusal.RESERVATION_EXPIRED)

        refusal = await self._already_settled(checkout)
        if refusal is not None:
            return await self._refuse(authorization, refusal)

        open_attempt = await self._attempts.get_open_for_mandate(checkout.mandate_id)
        if open_attempt is not None:
            # A different key against a checkout whose payment is already going, or a second
            # candidate checkout under a mandate that is already paying for the first. Either
            # way a second provider operation must not start.
            return await self._refuse(
                authorization,
                AdmissionRefusal.PAYMENT_IN_PROGRESS
                if open_attempt.checkout_id == checkout_id
                else AdmissionRefusal.MANDATE_PAYMENT_IN_PROGRESS,
            )

        async with translated_conflicts(self._session, identifier=str(checkout_id)):
            # A backstop rather than the mechanism. The locks and the checks above are what
            # stop a duplicate identity, a second non terminal attempt and a second success,
            # and all three are also indexes. If any is ever violated anyway, a caller gets
            # the refusal it names rather than a psycopg error as a 500.
            attempt = await self._attempts.create(
                merchant_id=checkout.merchant_id,
                checkout_id=checkout.id,
                mandate_id=checkout.mandate_id,
                reservation_id=reservation.id,
                idempotency_key=idempotency_key,
                # Frozen from the quote, and refused by the composite foreign key if they are
                # anything else. Nothing later reads a live checkout to decide what to charge.
                amount_minor=checkout.total_amount_minor,
                currency=checkout.currency,
            )

        await self._inventory.commit_to_payment(reservation)
        await self._audit.append(
            merchant_id=attempt.merchant_id,
            actor_type=ADMISSION_ACTOR,
            credential_id=credential_id,
            event_type=PAYMENT_ADMITTED,
            resource_type=PAYMENT_RESOURCE,
            resource_id=attempt.id,
            payload=_admitted_payload(attempt, reservation),
        )
        await self._session.commit()
        return PaymentAdmission(
            checkout_id=checkout_id,
            evaluated_at=authorization.evaluated_at,
            admitted_at=admitted_at,
            authorization=authorization.authorization,
            attempt=attempt,
            created=True,
        )

    async def _already_settled(self, checkout: CheckoutSession) -> AdmissionRefusal | None:
        """Whether this mandate or this checkout has already been paid for.

        The mandate check is the single purchase rule, read under the mandate lock this
        transaction is already holding. The checkout check is mostly unreachable, since a paid
        checkout is not OPEN and the financial gate refuses it before this runs, and it is
        stated anyway: a rule this important should not depend on another rule staying true.
        """
        if await self._attempts.get_succeeded_for_mandate(checkout.mandate_id) is not None:
            return AdmissionRefusal.MANDATE_ALREADY_CONSUMED
        if await self._attempts.get_succeeded_for_checkout(checkout.id) is not None:
            return AdmissionRefusal.CHECKOUT_ALREADY_PAID
        return None

    async def _refuse(
        self, authorization: LockedAuthorization, refusal: AdmissionRefusal
    ) -> PaymentAdmission:
        """Roll back and report why, reading only what survives the rollback.

        The two ORM objects on the authorization are expired by the rollback and are not
        touched after it. The three plain values are read first.
        """
        evaluated_at = authorization.evaluated_at
        admitted_at = authorization.admitted_at
        decision = authorization.authorization
        checkout_id = authorization.checkout.id
        await self._session.rollback()
        return PaymentAdmission(
            checkout_id=checkout_id,
            evaluated_at=evaluated_at,
            admitted_at=admitted_at,
            authorization=decision,
            refusal=refusal,
        )


def _admitted_payload(attempt: PaymentAttempt, reservation: InventoryReservation) -> dict[str, Any]:
    """What was admitted, in the words of the attempt itself.

    The amount and the currency are recorded because they are what a provider will be asked
    for, and a trail that cannot answer "how much was this authorized for" without joining to
    a quote is a trail that stops answering it once the quote is archived. The reservation is
    recorded because this event is also the record that the hold became committed, which is
    why no separate inventory event is appended for it.

    No idempotency key. It is an identity a caller chose and it travels to a provider, and an
    audit payload is read in more places than either.
    """
    return {
        "checkout_id": str(attempt.checkout_id),
        "mandate_id": str(attempt.mandate_id),
        "reservation_id": str(reservation.id),
        "amount_minor": attempt.amount_minor,
        "currency": attempt.currency,
        "status": attempt.status.value,
        "total_quantity": reservation.total_quantity,
    }
