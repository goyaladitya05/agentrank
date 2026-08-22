"""Reads and writes for the Razorpay checkout binding. No decisions, no commits.

Same shape as every other repository here: it emits SQL and flushes, and the service above it
decides what a result means and when a transaction ends. The two writes below are transitions
rather than setters, because the guard trigger enforces a whitelist and a repository that
offered a general purpose update would be offering to fail.

Every read that a request can reach is merchant scoped, and the scope is a condition in the
query rather than a check afterwards. That is the difference between a merchant being unable to
see another merchant's binding and a merchant being able to see it and then being told off.
"""

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.razorpay.models import RazorpayCheckout, RazorpayCheckoutStatus


class RazorpayCheckoutRepository:
    """Persistence for the AgentRank to Razorpay binding."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        merchant_id: uuid.UUID,
        payment_attempt_id: uuid.UUID,
        provider_receipt: str,
        amount_minor: int,
        currency: str,
    ) -> RazorpayCheckout:
        """Reserve the receipt for one attempt, before Razorpay is asked anything.

        PREPARING with no order identifier, which is the honest description of the instant
        before the create call: a receipt is spoken for and nothing exists at the gateway yet.

        Committing this first is what makes a lost create response recoverable rather than
        merely regrettable. The alternative, calling Razorpay and then writing the row, leaves a
        window in which an order exists and nothing in this database knows its receipt was ever
        used.
        """
        binding = RazorpayCheckout(
            merchant_id=merchant_id,
            payment_attempt_id=payment_attempt_id,
            provider_receipt=provider_receipt,
            amount_minor=amount_minor,
            currency=currency,
            status=RazorpayCheckoutStatus.PREPARING,
        )
        self._session.add(binding)
        await self._session.flush()
        return binding

    async def get_for_attempt(
        self, payment_attempt_id: uuid.UUID, *, merchant_id: uuid.UUID
    ) -> RazorpayCheckout | None:
        """One merchant's binding for one attempt, or nothing.

        `merchant_id` is required rather than optional. Every caller has an authenticated
        merchant, and a signature defaulting it to None would make the unscoped read the easy
        one to write.
        """
        statement = select(RazorpayCheckout).where(
            RazorpayCheckout.payment_attempt_id == payment_attempt_id,
            RazorpayCheckout.merchant_id == merchant_id,
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get_for_update(
        self, payment_attempt_id: uuid.UUID, *, merchant_id: uuid.UUID
    ) -> RazorpayCheckout | None:
        """The same read, with the row locked for the rest of this transaction.

        Two requests preparing one checkout at the same instant both want to create an order,
        and only one may. The unique constraint on `payment_attempt_id` is what makes that
        impossible rather than unlikely, and this is what makes the loser wait rather than
        raise.
        """
        statement = (
            select(RazorpayCheckout)
            .where(
                RazorpayCheckout.payment_attempt_id == payment_attempt_id,
                RazorpayCheckout.merchant_id == merchant_id,
            )
            .with_for_update()
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get_by_receipt(self, provider_receipt: str) -> RazorpayCheckout | None:
        """One binding by the identity Razorpay knows it as.

        Unscoped, and not reachable from a request. A receipt is derived from a merchant and an
        attempt, so finding one by receipt is finding the merchant, and there is nothing for a
        caller to scope it by that it does not already contain.
        """
        statement = select(RazorpayCheckout).where(
            RazorpayCheckout.provider_receipt == provider_receipt
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def bind_order(self, binding: RazorpayCheckout, *, provider_order_id: str) -> bool:
        """Record which Razorpay order this attempt is settled through.

        Returns whether this call is what wrote it, so a second preparation that finds the order
        already bound can say so rather than pretending it created one.

        An attempt to bind a different order raises. The guard trigger refuses it too, and both
        exist because rebinding a payment to another order is the single most expensive update
        available in this table: it would point verification at an order the customer never paid.

        Idempotent only if the row was read under a lock.
        """
        if binding.provider_order_id is not None:
            if binding.provider_order_id != provider_order_id:
                raise ValueError(
                    f"razorpay checkout {binding.id} is already bound to order"
                    f" {binding.provider_order_id} and cannot be rebound"
                )
            return False

        binding.provider_order_id = provider_order_id
        binding.order_created_at = func.now()
        binding.status = RazorpayCheckoutStatus.AWAITING_PAYMENT
        await self._session.flush()
        await self._session.refresh(binding, ["order_created_at"])
        return True

    async def mark_confirmed(self, binding: RazorpayCheckout, *, provider_payment_id: str) -> bool:
        """Record that a provider payment confirmed this checkout, and report whether it moved.

        Idempotent in the direction that matters: a binding already CONFIRMED is left exactly as
        it is and reported unchanged, so a repeated callback writes nothing. The provider
        payment identifier of the first confirmation stands, because that is the payment the
        outcome was applied for.

        Not the authoritative record of anything. The payment succeeded because
        `payment_attempt.status` says SUCCEEDED, in the same transaction as this. This column
        says which Razorpay payment it was.
        """
        if binding.status is RazorpayCheckoutStatus.CONFIRMED:
            return False

        binding.provider_payment_id = provider_payment_id
        binding.confirmed_at = func.now()
        binding.status = RazorpayCheckoutStatus.CONFIRMED
        await self._session.flush()
        await self._session.refresh(binding, ["confirmed_at"])
        return True

    async def clock(self) -> datetime:
        """The database clock, so that one clock stamps everything a request reasons about."""
        stamped = await self._session.scalar(select(func.now()))
        if stamped is None:
            # Not reachable: now() is never null.
            raise RuntimeError("the database did not return a time")
        return stamped
