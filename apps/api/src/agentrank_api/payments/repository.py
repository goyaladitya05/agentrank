"""Persistence access for payment attempts.

The repository owns SQLAlchemy and does not commit: the caller sets the transaction
boundary, which is what lets an attempt, a reservation transition and an audit event be one
unit of work.

There is deliberately no generic update method. An attempt is written once and then moves
along a whitelist of transitions, one method per transition, each reporting whether it is
what changed the row. A caller cannot set a status, cannot set an amount, and cannot reopen a
terminal attempt: the methods do not exist and the database trigger refuses it anyway.

Beside the lifecycle there is one operational read shape, `PaymentOperationRow`. It answers
the questions somebody recovering payments has to ask, and it answers them in one statement
across three tables rather than by handing a caller an attempt and letting it fetch the
checkout and the hold per row. It is a frozen record rather than an ORM object on purpose:
an operator tool must not be able to write through a read.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Self

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.checkout.models import CheckoutSession, CheckoutStatus
from agentrank_api.inventory.models import InventoryReservation, ReservationStatus
from agentrank_api.payments.models import (
    OPEN_STATUSES,
    OutcomeSource,
    PaymentAttempt,
    PaymentAttemptStatus,
)

# How many unresolved attempts one operator read returns unless told otherwise, and the most
# it will return however large a number is asked for. Both exist because this table only
# grows and an operator command that could scan all of it is a command that will one day be
# run against a table nobody wants printed. Fifty is a screen; five hundred is a deliberate
# sweep and still bounded.
DEFAULT_UNRESOLVED_LIMIT = 50
MAX_UNRESOLVED_LIMIT = 500


def payment_attempt_lock_statement(attempt_id: uuid.UUID) -> Select[tuple[PaymentAttempt]]:
    """The statement that locks one payment attempt, written where a test can read it.

    `FOR UPDATE` rather than a weaker mode, for the same reason as every other lock here: it
    is the one mode that conflicts with all the others. An attempt whose status is about to
    be read and then written must not move underneath the operation deciding it, or two
    dispatches would both read ADMITTED and both call a provider.

    `populate_existing` is load bearing. Without it an attempt already in the session's
    identity map is returned with the attributes it was loaded with, so the row would be
    locked and then read stale, which is the exact failure the lock exists to prevent. It
    matters more here than anywhere else: the stale attribute would be the status that
    decides whether a payment is dispatched.

    Last in the lock order. See agentrank_api.locking.
    """
    return (
        select(PaymentAttempt)
        .where(PaymentAttempt.id == attempt_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


@dataclass(frozen=True, slots=True)
class PaymentOperationRow:
    """One payment as somebody recovering it needs to see it.

    Everything an operator asks about a stuck payment, in one record: what state it is in,
    which checkout and mandate it belongs to, how much money is involved, what the hold and
    the quote currently say, and every instant that has been stamped on it.

    Frozen, and deliberately not a `PaymentAttempt`. A read that handed back the mapped
    object would hand back something writable and something lazily joined, and an operator
    tool listing fifty payments would then issue a hundred more statements to say what
    checkout each one belonged to. This is assembled by one statement across three tables.

    The checkout status and the reservation status are the current ones rather than the ones
    that were true when the payment was admitted. That is what an operator needs: the
    question is what stands now.
    """

    attempt_id: uuid.UUID
    status: PaymentAttemptStatus
    merchant_id: uuid.UUID
    checkout_id: uuid.UUID
    checkout_status: CheckoutStatus
    mandate_id: uuid.UUID
    reservation_id: uuid.UUID
    reservation_status: ReservationStatus
    idempotency_key: str
    amount_minor: int
    currency: str
    provider_reference: str | None
    failure_code: str | None
    outcome_source: OutcomeSource | None
    created_at: datetime
    dispatched_at: datetime | None
    resolved_at: datetime | None

    @classmethod
    def of(
        cls,
        attempt: PaymentAttempt,
        checkout_status: CheckoutStatus,
        reservation_status: ReservationStatus,
    ) -> Self:
        """Copy the fields out, so the mapped object stays inside the repository."""
        return cls(
            attempt_id=attempt.id,
            status=attempt.status,
            merchant_id=attempt.merchant_id,
            checkout_id=attempt.checkout_id,
            checkout_status=checkout_status,
            mandate_id=attempt.mandate_id,
            reservation_id=attempt.reservation_id,
            reservation_status=reservation_status,
            idempotency_key=attempt.idempotency_key,
            amount_minor=attempt.amount_minor,
            currency=attempt.currency,
            provider_reference=attempt.provider_reference,
            failure_code=attempt.failure_code,
            outcome_source=attempt.outcome_source,
            created_at=attempt.created_at,
            dispatched_at=attempt.dispatched_at,
            resolved_at=attempt.resolved_at,
        )

    @property
    def is_unresolved(self) -> bool:
        """Whether this payment is one of the ones an operator has to do something about."""
        return self.status in OPEN_STATUSES

    def age(self, observed_at: datetime) -> timedelta:
        """How long this payment has existed, measured from the instant it was admitted.

        Admission is the instant the authorization was decided and the instant the hold and
        the mandate started being held by this attempt, so it is what an operator asking how
        long something has been stuck is really asking about. Deliberately not the dispatch
        instant, which would restart the clock for a payment that had already been waiting,
        and deliberately not anything read out of the audit trail.

        `observed_at` is passed in rather than read here, so every row in one listing is aged
        against one instant and that instant is the database's clock rather than the clock of
        whatever machine an operator happens to be on.
        """
        return observed_at - self.created_at


class PaymentAttemptRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        merchant_id: uuid.UUID,
        checkout_id: uuid.UUID,
        mandate_id: uuid.UUID,
        reservation_id: uuid.UUID,
        idempotency_key: str,
        amount_minor: int,
        currency: str,
    ) -> PaymentAttempt:
        """Write an admitted attempt and flush so that generated columns are set.

        An attempt is always created ADMITTED. There is no parameter for status, so an
        attempt that claims a provider was already called cannot be brought into existence,
        only arrived at.

        The amount and the currency are passed in rather than read from a checkout here,
        because deciding which checkout and at which instant is the admission service's
        business. What makes them trustworthy is not this signature: it is the composite
        foreign key, which refuses any pair that is not the checkout's own total and
        currency.
        """
        attempt = PaymentAttempt(
            merchant_id=merchant_id,
            checkout_id=checkout_id,
            mandate_id=mandate_id,
            reservation_id=reservation_id,
            idempotency_key=idempotency_key,
            amount_minor=amount_minor,
            currency=currency,
            status=PaymentAttemptStatus.ADMITTED,
        )
        self._session.add(attempt)
        await self._session.flush()
        return attempt

    async def get(self, attempt_id: uuid.UUID) -> PaymentAttempt | None:
        """One attempt, whatever merchant it belongs to.

        Unscoped, and it stays unscoped, which is the opposite of the decision the checkout and
        mandate repositories made. The difference is who the callers are. Every read of a quote
        or an authorization comes from a request acting for one merchant. Payments are also read
        by the recovery kernel and by the operator command line, which walk from an attempt to
        the checkout and the mandate it names and have no merchant in hand to scope by.

        No HTTP path reaches this. The two that read a payment for a caller use
        `get_for_merchant` below, and the ones that resolve an outcome are reached only after
        `get_for_merchant` or the command line has already established who is asking.
        """
        return await self._session.get(PaymentAttempt, attempt_id)

    async def get_for_merchant(
        self, attempt_id: uuid.UUID, *, merchant_id: uuid.UUID
    ) -> PaymentAttempt | None:
        """One merchant's payment attempt, or nothing.

        The read every authenticated payment request goes through. The merchant is a condition
        in the SQL rather than a comparison afterwards, so another merchant's attempt is absent
        rather than refused and knowing its identifier reveals nothing.

        `merchant_id` on an attempt is immutable: no method here writes it, the guard trigger
        refuses every update that would, and the composite foreign key ties it to the checkout's
        merchant anyway. So an ownership answer from this read cannot go stale between here and
        whatever the caller does next.
        """
        statement = select(PaymentAttempt).where(
            PaymentAttempt.id == attempt_id, PaymentAttempt.merchant_id == merchant_id
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get_for_update(self, attempt_id: uuid.UUID) -> PaymentAttempt | None:
        """Fetch one attempt and hold it against every other transaction until commit."""
        statement = payment_attempt_lock_statement(attempt_id)
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get_by_identity(
        self, *, checkout_id: uuid.UUID, idempotency_key: str
    ) -> PaymentAttempt | None:
        """The attempt this logical payment operation already produced, if any.

        The identity of a payment operation is the checkout it pays for and the key the
        caller chose for it. Two requests carrying both are the same request, whatever else
        differs about them, and this is what makes the second one return the first one's
        answer rather than start a second payment.
        """
        statement = select(PaymentAttempt).where(
            PaymentAttempt.checkout_id == checkout_id,
            PaymentAttempt.idempotency_key == idempotency_key,
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get_open_for_mandate(self, mandate_id: uuid.UUID) -> PaymentAttempt | None:
        """The one non terminal attempt under this mandate, if there is one.

        At most one exists: a partial unique index says so. That index is what makes a second
        candidate checkout under the same mandate refusable at admission rather than
        something that races the first one to a provider, and this read is what turns the
        refusal into a sentence a caller can act on instead of an integrity error.
        """
        statement = select(PaymentAttempt).where(
            PaymentAttempt.mandate_id == mandate_id,
            PaymentAttempt.status.in_(OPEN_STATUSES),
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get_open_for_checkout(self, checkout_id: uuid.UUID) -> PaymentAttempt | None:
        """The one non terminal attempt for this checkout, if there is one.

        At most one exists, implied rather than indexed: the mandate scoped index allows one
        non terminal attempt per mandate, and a checkout names exactly one mandate. This asks
        the narrower question, which is the one a cancellation needs: a checkout with a
        payment that may still reach a provider must not be withdrawn underneath it.
        """
        statement = select(PaymentAttempt).where(
            PaymentAttempt.checkout_id == checkout_id,
            PaymentAttempt.status.in_(OPEN_STATUSES),
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get_succeeded_for_mandate(self, mandate_id: uuid.UUID) -> PaymentAttempt | None:
        """The one successful payment that consumed this mandate, if there is one.

        A mandate authorizes one purchase. At most one attempt under it is ever SUCCEEDED,
        and a partial unique index rather than this query is what guarantees it. This read
        exists so that a second admission is refused with a reason rather than a violation.
        """
        statement = select(PaymentAttempt).where(
            PaymentAttempt.mandate_id == mandate_id,
            PaymentAttempt.status == PaymentAttemptStatus.SUCCEEDED,
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get_succeeded_for_checkout(self, checkout_id: uuid.UUID) -> PaymentAttempt | None:
        """The one successful payment for this checkout, if there is one."""
        statement = select(PaymentAttempt).where(
            PaymentAttempt.checkout_id == checkout_id,
            PaymentAttempt.status == PaymentAttemptStatus.SUCCEEDED,
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def list_for_checkout(self, checkout_id: uuid.UUID) -> Sequence[PaymentAttempt]:
        """Every payment ever attempted for a checkout, oldest first.

        Version 7 identifiers are time ordered, so this is the order they were admitted in.
        """
        statement = (
            select(PaymentAttempt)
            .where(PaymentAttempt.checkout_id == checkout_id)
            .order_by(PaymentAttempt.id)
        )
        return (await self._session.execute(statement)).scalars().all()

    async def clock(self) -> datetime:
        """The database's own clock, read once so a whole listing is aged from one instant.

        `now()` in PostgreSQL is `transaction_timestamp()`, so this and the read that follows
        it in the same transaction return the same instant. That is the point: two rows in one
        listing must not be aged against two different readings, and an operator machine whose
        clock is minutes off the database's must not be able to make a payment look older or
        younger than it is.
        """
        observed: datetime = (await self._session.execute(select(func.now()))).scalar_one()
        return observed

    async def list_unresolved(
        self, *, limit: int = DEFAULT_UNRESOLVED_LIMIT
    ) -> Sequence[PaymentOperationRow]:
        """Every payment that still needs somebody, oldest first, bounded.

        Unresolved is `OPEN_STATUSES` and is not a second definition of it. ADMITTED,
        IN_FLIGHT and UNKNOWN are exactly the states in which an identity may still reach a
        provider or is waiting on one, which is the same set the mandate scoped uniqueness is
        built on. SUCCEEDED and FAILED are absent because there is nothing to do about them.

        Oldest means admitted longest ago: the order is `created_at` and then the identifier,
        which is a total order and therefore a stable prefix rather than an arbitrary one.
        `created_at` is the admission instant, so this is the order the payments started
        holding stock in. It is deliberately not audit order, which is transaction start order
        and is not commit order, and nothing here infers anything from the trail.

        One statement across three tables. An operator listing fifty payments must not cost
        a hundred and fifty more statements to say which checkout and which hold each one
        names, and returning frozen records rather than mapped objects is what guarantees
        that rather than hoping a lazy load never fires.
        """
        statement = (
            select(PaymentAttempt, CheckoutSession.status, InventoryReservation.status)
            .join(CheckoutSession, CheckoutSession.id == PaymentAttempt.checkout_id)
            .join(InventoryReservation, InventoryReservation.id == PaymentAttempt.reservation_id)
            .where(PaymentAttempt.status.in_(OPEN_STATUSES))
            .order_by(PaymentAttempt.created_at, PaymentAttempt.id)
            .limit(bounded_unresolved_limit(limit))
        )
        rows = (await self._session.execute(statement)).all()
        return [
            PaymentOperationRow.of(attempt, checkout_status, reservation_status)
            for attempt, checkout_status, reservation_status in rows
        ]

    async def get_operational(self, attempt_id: uuid.UUID) -> PaymentOperationRow | None:
        """One payment in the same shape the listing uses, whatever state it is in.

        Not restricted to unresolved ones. An operator who has just resolved something has to
        be able to read it back, and a detail view that refused to show a settled payment
        would send them to psql to find out what happened.
        """
        statement = (
            select(PaymentAttempt, CheckoutSession.status, InventoryReservation.status)
            .join(CheckoutSession, CheckoutSession.id == PaymentAttempt.checkout_id)
            .join(InventoryReservation, InventoryReservation.id == PaymentAttempt.reservation_id)
            .where(PaymentAttempt.id == attempt_id)
        )
        found = (await self._session.execute(statement)).one_or_none()
        if found is None:
            return None
        attempt, checkout_status, reservation_status = found
        return PaymentOperationRow.of(attempt, checkout_status, reservation_status)

    async def count_by_status(self) -> dict[PaymentAttemptStatus, int]:
        """How many attempts are in each state, aggregated in the database.

        The one operator read with no row limit, because it does not return rows: PostgreSQL
        groups and this receives at most one line per status. Counting is not sampling and it
        is not a window either, so the terminal counts are lifetime totals rather than recent
        ones. A window would need a policy about how long recent is, and nobody has set one.

        A status with no attempts is absent from the result rather than present as zero.
        Filling it in is the caller's business, and doing it here would mean this method
        deciding which statuses an operator cares about.
        """
        statement = select(PaymentAttempt.status, func.count()).group_by(PaymentAttempt.status)
        rows = (await self._session.execute(statement)).all()
        return {row[0]: row[1] for row in rows}

    async def mark_in_flight(self, attempt: PaymentAttempt) -> bool:
        """Record that this attempt may now have been dispatched, before it is.

        The direction of that sentence is the design. This commits before the network call
        begins, so an attempt found in ADMITTED after a crash was certainly never sent and is
        safe to dispatch, while one found in IN_FLIGHT may or may not have been and has to be
        reconciled. The uncertainty is one sided, and it is on the side that cannot charge
        anybody twice.

        Only an ADMITTED attempt can be dispatched. Anything else is refused here rather than
        silently ignored, because a second dispatch is exactly the thing this whole module
        exists to prevent.

        Idempotent only if the row was read under a lock.
        """
        if attempt.status is not PaymentAttemptStatus.ADMITTED:
            raise ValueError(f"a {attempt.status.value} attempt cannot be dispatched")

        attempt.status = PaymentAttemptStatus.IN_FLIGHT
        attempt.dispatched_at = func.now()
        await self._session.flush()
        await self._session.refresh(attempt, ["dispatched_at"])
        return True

    async def mark_succeeded(
        self, attempt: PaymentAttempt, *, provider_reference: str, source: OutcomeSource
    ) -> bool:
        """Record a definitive provider success, and report whether this call changed it.

        Idempotent: an attempt already SUCCEEDED is left alone and reported unchanged, so a
        reconciliation that arrives after the execution already recorded the same success
        writes no second outcome and appends no second event.

        Only a dispatched attempt can succeed. An ADMITTED one has provably never reached a
        provider, so a success reported for it would be a success nobody asked for.
        """
        if attempt.status is PaymentAttemptStatus.SUCCEEDED:
            return False
        _require_dispatched(attempt, "succeed")

        attempt.status = PaymentAttemptStatus.SUCCEEDED
        attempt.provider_reference = provider_reference
        attempt.outcome_source = source
        attempt.resolved_at = func.now()
        await self._session.flush()
        await self._session.refresh(attempt, ["resolved_at"])
        return True

    async def mark_failed(
        self,
        attempt: PaymentAttempt,
        *,
        failure_code: str,
        source: OutcomeSource,
        provider_reference: str | None = None,
    ) -> bool:
        """Record a definitive provider failure, and report whether this call changed it.

        Definitive means no money moved. A timeout is not this, and collapsing one into the
        other is the single most expensive mistake available here: it would release the stock
        and invite a second payment for a charge that had already gone through.
        """
        if attempt.status is PaymentAttemptStatus.FAILED:
            return False
        _require_dispatched(attempt, "fail")

        attempt.status = PaymentAttemptStatus.FAILED
        attempt.failure_code = failure_code
        attempt.provider_reference = provider_reference
        attempt.outcome_source = source
        attempt.resolved_at = func.now()
        await self._session.flush()
        await self._session.refresh(attempt, ["resolved_at"])
        return True

    async def mark_unknown(self, attempt: PaymentAttempt, *, source: OutcomeSource) -> bool:
        """Record that the result is ambiguous, and report whether this call changed it.

        No `resolved_at` is stamped, because nothing was resolved. The attempt keeps its
        commitment on the stock, nothing is released, no checkout is paid, and nothing may
        retry it. It leaves this state by being queried, not by being tried again.
        """
        if attempt.status is PaymentAttemptStatus.UNKNOWN:
            return False
        _require_dispatched(attempt, "become unknown")

        attempt.status = PaymentAttemptStatus.UNKNOWN
        attempt.outcome_source = source
        await self._session.flush()
        return True


def bounded_unresolved_limit(limit: int) -> int:
    """Clamp an operator supplied limit into the range this repository will actually serve.

    Clamped rather than refused, because the caller that gets this wrong is a person typing a
    number at a terminal and the useful answer to `--limit 100000` is five hundred payments
    rather than an error. The floor is one for the same reason: zero and negative are not
    requests for nothing, they are typing mistakes.

    Public rather than private because the command that asks for a listing has to be able to
    report the bound it was actually given, and a caller working that out for itself would be
    a second copy of this rule.
    """
    return min(max(limit, 1), MAX_UNRESOLVED_LIMIT)


def _require_dispatched(attempt: PaymentAttempt, verb: str) -> None:
    """Refuse an outcome for an attempt that no provider can have answered.

    ADMITTED means certainly not dispatched, and the two terminal states mean already
    answered. Only IN_FLIGHT and UNKNOWN can receive an outcome, which is the same whitelist
    the database trigger enforces, stated here so a caller gets a sentence rather than a
    driver error.
    """
    if attempt.status not in (PaymentAttemptStatus.IN_FLIGHT, PaymentAttemptStatus.UNKNOWN):
        raise ValueError(f"a {attempt.status.value} attempt cannot {verb}")
