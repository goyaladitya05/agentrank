"""Persistence access for payment attempts.

The repository owns SQLAlchemy and does not commit: the caller sets the transaction
boundary, which is what lets an attempt, a reservation transition and an audit event be one
unit of work.

There is deliberately no generic update method. An attempt is written once and then moves
along a whitelist of transitions, one method per transition, each reporting whether it is
what changed the row. A caller cannot set a status, cannot set an amount, and cannot reopen a
terminal attempt: the methods do not exist and the database trigger refuses it anyway.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.payments.models import (
    OPEN_STATUSES,
    OutcomeSource,
    PaymentAttempt,
    PaymentAttemptStatus,
)


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
        return await self._session.get(PaymentAttempt, attempt_id)

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


def _require_dispatched(attempt: PaymentAttempt, verb: str) -> None:
    """Refuse an outcome for an attempt that no provider can have answered.

    ADMITTED means certainly not dispatched, and the two terminal states mean already
    answered. Only IN_FLIGHT and UNKNOWN can receive an outcome, which is the same whitelist
    the database trigger enforces, stated here so a caller gets a sentence rather than a
    driver error.
    """
    if attempt.status not in (PaymentAttemptStatus.IN_FLIGHT, PaymentAttemptStatus.UNKNOWN):
        raise ValueError(f"a {attempt.status.value} attempt cannot {verb}")
