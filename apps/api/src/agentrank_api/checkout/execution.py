"""The one path a checkout takes to become safe to attempt.

Everything Phase 1E exists for meets here. A checkout is execution ready only when the
mandate authorizes the money, an authoritative constraint set authorizes the purchase, the
quote is still open and unexpired, and the stock it names has actually been held. Any future
payment execution has to come through this method, so that no caller can satisfy three of
those and forget the fourth.

Execution ready means safe to attempt payment. It does not mean paid, and nothing here can
pay: no provider exists, no payment attempt is written, and no checkout status changes.

The order matters and it is deliberate:

```text
load the checkout, the mandate and the constraint set
        |
        v
evaluate both authorization gates against the current time
        |
        v
not authorized -> return, having written nothing and locked nothing
        |
        v
derive the reservation expiry from the checkout and the mandate
        |
        v
lock the variant rows, count what is held, reserve or refuse
        |
        v
execution ready
```

Authorization comes first so that an obviously unauthorized request never takes a lock on a
merchant's catalog rows. Inventory comes last because it is the only step that writes.

Preparation always re-evaluates. It never trusts an earlier read of either authorization
endpoint, because a mandate can be revoked, a checkout can be cancelled and either can
expire between the two calls. This is load bearing: an authorization that was true a minute
ago is not an authorization now, and the whole point of one path is that the path is where
the check happens.

Live catalog state is read for exactly one thing, which is stock. What the checkout costs
and what it is were snapshotted when it was quoted and are read from the quote, so a
merchant editing a price or a colour afterwards cannot change what was authorized. That
split is deliberate: quote semantics come from snapshots, availability comes from now.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.checkout.execution_authorization import (
    CheckoutExecutionAuthorization,
    authorize_checkout_execution,
)
from agentrank_api.checkout.models import CheckoutSession
from agentrank_api.checkout.repository import CheckoutRepository
from agentrank_api.constraints.repository import IntentConstraintRepository
from agentrank_api.errors import NotFoundError
from agentrank_api.inventory.models import InventoryReservation
from agentrank_api.inventory.rules import reservation_expires_at
from agentrank_api.inventory.service import (
    InventoryReservationService,
    InventoryViolation,
)
from agentrank_api.mandates.models import SpendingMandate
from agentrank_api.mandates.repository import MandateRepository


@dataclass(frozen=True, slots=True)
class CheckoutExecutionReadiness:
    """Whether this checkout may be attempted, and if not, everything that stopped it.

    `ready` is derived rather than stored, so a result carrying a denial or a shortfall
    cannot also claim to be ready. It requires a reservation to exist, which is what stops
    an authorized checkout with no stock from reading as ready.

    Never a bare boolean. A caller that cannot explain a refusal is a caller that will retry
    the same request, and the two reasons a preparation fails call for completely different
    next moves: an authorization denial is about what the buyer may do, and an inventory
    shortfall is about what the merchant has.
    """

    checkout_id: uuid.UUID
    evaluated_at: datetime
    authorization: CheckoutExecutionAuthorization
    reservation: InventoryReservation | None = None
    inventory_violations: tuple[InventoryViolation, ...] = ()

    @property
    def ready(self) -> bool:
        return (
            self.authorization.authorized
            and self.reservation is not None
            and not self.inventory_violations
        )


class CheckoutExecutionService:
    """Prepare a checkout for an execution that does not exist yet.

    It loads authoritative state, requires both gates, revalidates against the current
    time and reserves inventory transactionally. It does not call a payment provider, does
    not create a payment attempt, does not move a checkout into any new status and executes
    nothing external. There is nothing here for a payment to reuse except the readiness
    answer itself, which is the point.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._checkouts = CheckoutRepository(session)
        self._mandates = MandateRepository(session)
        self._constraints = IntentConstraintRepository(session)
        self._inventory = InventoryReservationService(session)

    async def prepare_execution(
        self, checkout_id: uuid.UUID, *, at: datetime | None = None
    ) -> CheckoutExecutionReadiness:
        """Make this checkout execution ready, or report exactly why it is not.

        One instant is chosen at the start and used for both gates and for the inventory
        accounting, so the whole preparation describes one moment rather than several
        readings of a moving clock.

        A refusal writes nothing. An authorization denial returns before any lock is taken,
        and an inventory shortfall rolls back the locks it took without writing a row, so a
        checkout that may not proceed never holds a merchant's stock.

        A success commits the reservation, its lines and the `inventory.reserved` event
        together. Preparing again while that reservation is still effective returns the same
        one and writes nothing further.
        """
        moment = at or datetime.now(UTC)
        checkout, mandate = await self._load(checkout_id)
        constraint_set = await self._constraints.get_for_mandate(checkout.mandate_id)

        authorization = authorize_checkout_execution(checkout, mandate, constraint_set, at=moment)
        if not authorization.authorized:
            # Nothing has been written and no catalog row has been locked. A request that
            # may not proceed must not be able to take stock off a merchant's shelf, even
            # briefly.
            await self._session.rollback()
            return CheckoutExecutionReadiness(
                checkout_id=checkout_id, evaluated_at=moment, authorization=authorization
            )

        outcome = await self._inventory.reserve(
            checkout,
            # Server derived, never a caller's number, and never longer than either of the
            # two things that make this checkout usable at all.
            expires_at=reservation_expires_at(checkout.expires_at, mandate.valid_until),
            at=moment,
        )
        if not outcome.reserved:
            await self._session.rollback()
            return CheckoutExecutionReadiness(
                checkout_id=checkout_id,
                evaluated_at=moment,
                authorization=authorization,
                inventory_violations=outcome.violations,
            )

        await self._session.commit()
        return CheckoutExecutionReadiness(
            checkout_id=checkout_id,
            evaluated_at=moment,
            authorization=authorization,
            reservation=outcome.reservation,
        )

    async def execution_authorization(
        self, checkout_id: uuid.UUID, *, at: datetime | None = None
    ) -> CheckoutExecutionAuthorization:
        """Report what both gates say about this checkout, without holding anything.

        Informational. It writes nothing, locks nothing and reserves nothing, so its answer
        is what was true when it was asked and grants nothing at all. A caller that reads
        `authorized: true` here still has to prepare, and preparation evaluates all of this
        again, because a mandate can be revoked and a checkout can expire in between.
        """
        checkout, mandate = await self._load(checkout_id)
        constraint_set = await self._constraints.get_for_mandate(checkout.mandate_id)
        return authorize_checkout_execution(
            checkout, mandate, constraint_set, at=at or datetime.now(UTC)
        )

    async def _load(self, checkout_id: uuid.UUID) -> tuple[CheckoutSession, SpendingMandate]:
        """The quote and the authorization it was written against.

        The mandate is found through the checkout rather than supplied, which is what stops
        a checkout from being paired with an authorization someone chose afterwards. The
        constraint set is found through that mandate for the same reason.
        """
        checkout = await self._checkouts.get(checkout_id)
        if checkout is None:
            raise NotFoundError("checkout", str(checkout_id))

        mandate = await self._mandates.get(checkout.mandate_id)
        if mandate is None:
            # Not reachable through the schema: the foreign key onto the mandate is
            # RESTRICT, so the mandate cannot have been removed while this quote exists.
            raise NotFoundError("mandate", str(checkout.mandate_id))
        return checkout, mandate
