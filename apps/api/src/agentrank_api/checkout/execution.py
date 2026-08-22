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
lock the mandate, then the checkout, then the variant rows
        |
        v
evaluate both authorization gates against the accounting instant
        |
        v
not authorized -> return, having written nothing
        |
        v
read the clock again, now that nothing can block, and require the
financial gate again against that instant
        |
        v
count what is held, reserve or refuse
        |
        v
execution ready
```

Establishing a precondition is not the same as holding it. The first version of this method
read the mandate and the checkout without locking either, decided both gates allowed, and
then blocked on inventory. While it blocked, another transaction could revoke that mandate
or cancel that checkout and commit, and this one would wake up and write a reservation on
the strength of a snapshot that had stopped being true. Nothing in the reservation's own
foreign keys stopped it: writing one takes `FOR KEY SHARE` on the checkout, cancelling one
takes `FOR NO KEY UPDATE`, and those two modes do not conflict.

So every row this decision rests on is locked before the decision is made, in the order
written down in `agentrank_api.locking`, and stays locked until commit. Revoking the mandate
and cancelling the checkout take the same locks, so they serialize against this rather than
racing it. Either they win and this observes the result and refuses, or this wins and they
wait for it.

Locks make state stand still. They do not stop the clock, which is the third way this used
to be wrong: a preparation that pinned one instant before it blocked could resume after the
quote or the mandate had lapsed and hold stock nobody could use. So the clock is read again
once every lock is held, and the time sensitive half of the authorization is decided again
against that reading. Only then is anything written.

Live catalog state is read for exactly one thing, which is stock. What the checkout costs
and what it is were snapshotted when it was quoted and are read from the quote, so a
merchant editing a price or a colour afterwards cannot change what was authorized. That
split is deliberate: quote semantics come from snapshots, availability comes from now.

Beside preparation is its inverse, `release_reservation`, which gives a hold back on purpose
and records why. Preparation is the only thing that takes a hold, so without a deliberate way
to end one a reservation that should not be held could only be waited out. It is an
application operation with no route: an endpoint that released an arbitrary reservation would
let an unauthenticated caller free stock a merchant was holding for somebody else.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.benchmark.mutation import BenchmarkMutationGuard, BenchmarkRunCapability
from agentrank_api.checkout.authorization import authorize_checkout
from agentrank_api.checkout.execution_authorization import (
    CheckoutExecutionAuthorization,
    authorize_checkout_execution,
)
from agentrank_api.checkout.models import CheckoutSession
from agentrank_api.checkout.repository import CheckoutRepository
from agentrank_api.conflicts import translated_conflicts
from agentrank_api.constraints.repository import IntentConstraintRepository
from agentrank_api.errors import NotFoundError
from agentrank_api.inventory.models import InventoryReservation
from agentrank_api.inventory.rules import reservation_expires_at
from agentrank_api.inventory.service import (
    InventoryReservationService,
    InventoryViolation,
    InventoryViolationCode,
    ReleaseReason,
)
from agentrank_api.mandates.models import SpendingMandate
from agentrank_api.mandates.repository import MandateRepository


@dataclass(frozen=True, slots=True)
class CheckoutExecutionReadiness:
    """Whether this checkout may be attempted, and if not, everything that stopped it.

    `ready` is derived rather than stored, so a result carrying a denial, a shortfall or a
    reservation that has already lapsed cannot also claim to be ready. It requires a
    reservation to exist, which is what stops an authorized checkout with no stock from
    reading as ready, and it requires that reservation to still be holding stock at
    `admitted_at`, which is what stops a ready answer from carrying a claim that had already
    expired by the time it was returned.

    Two instants, because there are honestly two. `evaluated_at` is what both gates and the
    inventory accounting were decided against, and it is injectable so that those decisions
    are reproducible. `admitted_at` is a reading of the real clock taken once every lock was
    held, which is the instant this answer is actually about; it is None when preparation
    refused before reaching that point, because no such reading was taken.

    Never a bare boolean. A caller that cannot explain a refusal is a caller that will retry
    the same request, and the reasons a preparation fails call for completely different next
    moves: an authorization denial is about what the buyer may do, and an inventory
    shortfall is about what the merchant has.
    """

    checkout_id: uuid.UUID
    evaluated_at: datetime
    authorization: CheckoutExecutionAuthorization
    admitted_at: datetime | None = None
    reservation: InventoryReservation | None = None
    inventory_violations: tuple[InventoryViolation, ...] = ()

    @property
    def ready(self) -> bool:
        return (
            self.authorization.authorized
            and not self.inventory_violations
            and self.admitted_at is not None
            and self.reservation is not None
            # The invariant this whole class exists to make unrepresentable: a ready result
            # never carries a reservation that had already stopped holding stock at the
            # instant it was admitted.
            and self.reservation.expires_at > self.admitted_at
        )


@dataclass(frozen=True, slots=True)
class LockedAuthorization:
    """Both gates decided while every relevant row is held, and nothing committed yet.

    The shared half of two operations. Execution preparation reserves stock on the strength
    of it; payment admission writes a `PaymentAttempt` on the strength of it. Both need the
    same locks taken in the same order, the same two gates, and the same second reading of
    the clock, and both need to keep holding all of that while they write.

    That last point is why this exists at all. An operation that called a method which
    committed and then wrote its own row afterwards would be running two transactions with a
    gap between them, and a revocation or a cancellation committing in that gap would make
    the first decision stale before the second write landed. So nothing here commits and
    nothing here rolls back: the caller owns the transaction, and the locks stay held from
    the decision through to the write.

    The two ORM objects are live and locked, and a caller that rolls back must stop reading
    them. `evaluated_at`, `admitted_at` and `authorization` are plain data and stay readable
    afterwards, which is what a refusal is assembled from.

    `admitted` requires both that the composed authorization allows and that an admission
    instant was reached, so a result that refused before the variant locks were taken cannot
    read as permitting anything.
    """

    checkout: CheckoutSession
    mandate: SpendingMandate
    evaluated_at: datetime
    authorization: CheckoutExecutionAuthorization
    admitted_at: datetime | None = None

    @property
    def admitted(self) -> bool:
        return self.authorization.authorized and self.admitted_at is not None

    def admission_instant(self) -> datetime:
        """The instant this was admitted at, for a caller that has checked `admitted`.

        A method rather than an attribute read, because `admitted_at` is legitimately absent
        on a refusal and every caller that has already established otherwise would have to
        say so again to a type checker. Raising here is the honest alternative to each of
        them asserting it separately.
        """
        if self.admitted_at is None:
            raise RuntimeError("this authorization was refused before it reached an instant")
        return self.admitted_at


class CheckoutExecutionService:
    """Prepare a checkout for an execution that does not exist yet.

    It locks authoritative state, requires both gates, revalidates against a fresh clock
    once nothing can block it and reserves inventory transactionally. It does not call a
    payment provider, does not create a payment attempt, does not move a checkout into any
    new status and executes nothing external. There is nothing here for a payment to reuse
    except the readiness answer itself, which is the point.
    """

    def __init__(
        self, session: AsyncSession, *, benchmark_capability: BenchmarkRunCapability | None = None
    ) -> None:
        self._session = session
        self._benchmark_capability = benchmark_capability
        self._mutation = BenchmarkMutationGuard(session)
        self._checkouts = CheckoutRepository(session)
        self._mandates = MandateRepository(session)
        self._constraints = IntentConstraintRepository(session)
        self._inventory = InventoryReservationService(session)

    async def authorize_under_locks(
        self, checkout_id: uuid.UUID, *, merchant_id: uuid.UUID, at: datetime | None = None
    ) -> LockedAuthorization:
        """Decide both gates with every relevant row held, and hand the locks to the caller.

        The order is the one `agentrank_api.locking` writes down: the mandate, then the
        checkout, then the variant rows. The mandate and the checkout are held against the
        two operations that could withdraw them, and the variant rows against every other
        preparation and every other payment admission.

        Two instants, and the difference is the point. `at` is the accounting instant, used
        for both gates, and it is injectable so those decisions are reproducible. The
        admission instant is read from the real clock after the last lock is taken, which is
        the first moment this operation knows it will not wait again, and the financial gate
        is required a second time against it. Locks hold rows still; they do not hold the
        clock still, and an operation that queued behind a lock for longer than the quote had
        left has to find that out before it writes anything.

        A denial before the variant locks returns with no catalog row locked and no admission
        instant, so a request that may not proceed never touches a merchant's shelf. A denial
        at the admission instant returns with the locks held, and the caller rolls back.

        Nothing is committed and nothing is rolled back. Both are the caller's, because the
        whole value of this method is that its locks are still held when the caller writes.

        The variant locks are taken even though nothing here reads stock. They are what makes
        this operation serialize against every other one that decides something about the
        same shelf, and taking them before the clock is read again is what makes the reading
        one this operation cannot be delayed past.

        `merchant_id` is the authenticated merchant, and it is required rather than derived from
        the checkout. Deriving it would make this method authorize whoever the quote belongs to,
        which is the whole of the vulnerability rather than a check on it. A quote belonging to
        anybody else is not found, and the caller is told the same thing it would be told about
        an identifier nobody has ever used.
        """
        checkout, mandate = await self._load_locked(checkout_id, merchant_id=merchant_id)
        constraint_set = await self._constraints.get_for_mandate(
            checkout.mandate_id, merchant_id=checkout.merchant_id
        )

        evaluated_at = at or datetime.now(UTC)
        authorization = authorize_checkout_execution(
            checkout, mandate, constraint_set, at=evaluated_at
        )
        if not authorization.authorized:
            return LockedAuthorization(
                checkout=checkout,
                mandate=mandate,
                evaluated_at=evaluated_at,
                authorization=authorization,
            )

        # The last class of lock, and the one that actually waits.
        await self._inventory.lock_variants_for(checkout)

        admitted_at = datetime.now(UTC)
        return LockedAuthorization(
            checkout=checkout,
            mandate=mandate,
            evaluated_at=evaluated_at,
            admitted_at=admitted_at,
            authorization=authorization.with_financial(
                # The existing rule, against the new instant, rather than a second set of
                # timestamp comparisons that could disagree with it. The semantic half is
                # carried forward because a snapshot and an immutable constraint set cannot
                # have moved.
                authorize_checkout(checkout, mandate, at=admitted_at)
            ),
        )

    async def prepare_execution(
        self, checkout_id: uuid.UUID, *, merchant_id: uuid.UUID, at: datetime | None = None
    ) -> CheckoutExecutionReadiness:
        """Make this checkout execution ready, or report exactly why it is not.

        Every row the answer depends on is locked first, in the order `agentrank_api.locking`
        writes down, so that nothing this decision rests on can move underneath it. The
        mandate and the checkout are held against the two operations that could withdraw
        them, and the variant rows against every other preparation.

        A refusal writes nothing. An authorization denial returns before the catalog is
        touched at all, and every later refusal rolls back without leaving a row, so a
        checkout that may not proceed never holds a merchant's stock.

        Two instants, and the difference is the point. `at` is the accounting instant, used
        for both gates and for counting what is already held, and it is injectable so those
        decisions are reproducible. The admission instant is read from the real clock after
        the last lock is taken, which is the first moment this operation knows it will not
        wait again, and the financial gate is required a second time against it. That is
        what stops a preparation that queued behind a lock for longer than the quote had
        left from waking up and holding stock for a quote nobody may act on.

        A success commits the reservation, its lines and the `inventory.reserved` event
        together. Preparing again while that reservation is still effective returns the same
        one and writes nothing further.
        """
        await self._mutation.require_allowed(merchant_id, capability=self._benchmark_capability)
        admission = await self.authorize_under_locks(checkout_id, merchant_id=merchant_id, at=at)
        if not admission.admitted:
            # Nothing has been written. An authorization denial returns before any catalog
            # row is locked at all, so a request that may not proceed cannot take stock off a
            # merchant's shelf even briefly, and a denial at the admission instant rolls back
            # what it held. Only the plain data on the admission is read after this point:
            # the rollback expires the two ORM objects it carries.
            evaluated_at, admitted_at = admission.evaluated_at, admission.admitted_at
            authorization = admission.authorization
            await self._session.rollback()
            return CheckoutExecutionReadiness(
                checkout_id=checkout_id,
                evaluated_at=evaluated_at,
                admitted_at=admitted_at,
                authorization=authorization,
            )

        checkout = admission.checkout
        mandate = admission.mandate
        evaluated_at = admission.evaluated_at
        admitted_at = admission.admission_instant()

        async with translated_conflicts(self._session, identifier=str(checkout_id)):
            # A backstop rather than the mechanism. The locks and the admission check above
            # are what stop a second active reservation and an already expired one, and both
            # are also constraints on the table. If either is ever violated anyway, a caller
            # gets the refusal it names rather than a psycopg error as a 500.
            outcome = await self._inventory.reserve(
                checkout,
                # Server derived, never a caller's number, and never longer than either of
                # the two things that make this checkout usable at all.
                expires_at=reservation_expires_at(checkout.expires_at, mandate.valid_until),
                at=evaluated_at,
            )
        if not outcome.reserved:
            await self._session.rollback()
            return CheckoutExecutionReadiness(
                checkout_id=checkout_id,
                evaluated_at=evaluated_at,
                admitted_at=admitted_at,
                authorization=admission.authorization,
                inventory_violations=outcome.violations,
            )

        if outcome.reservation is None or outcome.reservation.expires_at <= admitted_at:
            # Neither half is reachable. `reserved` is true only when a reservation exists,
            # and a reservation expires at the earlier of the checkout expiry and the mandate
            # validity, both of which the admission gate just required `admitted_at` to be
            # before. Stated as a refusal rather than assumed, because "a ready answer
            # carries a reservation that is still holding stock" is the invariant this whole
            # operation exists to provide, and an invariant nothing checks is a comment.
            await self._session.rollback()
            return CheckoutExecutionReadiness(
                checkout_id=checkout_id,
                evaluated_at=evaluated_at,
                admitted_at=admitted_at,
                authorization=admission.authorization,
                inventory_violations=(
                    InventoryViolation(code=InventoryViolationCode.RESERVATION_EXPIRED),
                ),
            )

        await self._session.commit()
        return CheckoutExecutionReadiness(
            checkout_id=checkout_id,
            evaluated_at=evaluated_at,
            admitted_at=admitted_at,
            authorization=admission.authorization,
            reservation=outcome.reservation,
        )

    async def release_reservation(
        self, checkout_id: uuid.UUID, *, merchant_id: uuid.UUID, reason: ReleaseReason
    ) -> bool:
        """Give back the stock this checkout is holding, and say why in the trail.

        The inverse of preparation, and the reason it exists is that preparation is the only
        thing that takes a hold. Until now the only way to give one back was to cancel the
        quote, which is a decision about the quote rather than about the stock, so a
        reservation that should not be held could only be waited out. A claim on a merchant's
        inventory that nothing can deliberately end is a claim the system has lost track of.

        `reason` is required and is a code rather than prose, because the whole point of
        releasing on purpose is that the trail says which purpose. There is no default: a
        caller that has not decided why it is giving stock back has not decided to give it
        back.

        Internal on purpose. No route reaches this. An endpoint that released an arbitrary
        reservation would let an unauthenticated caller free stock a merchant was holding for
        somebody else, and no caller outside this application needs one: a buyer who does not
        want the item cancels the quote, and a lapsed reservation frees itself.

        The checkout is locked first, so this serializes against a preparation that may be
        writing a reservation for it at this moment, and against a cancellation. Second in
        the lock order and nothing above it, which reverses nothing. See
        agentrank_api.locking.

        Idempotent, and it reports whether this call is what changed anything. Releasing a
        checkout that holds nothing is not an error and records nothing. The release and its
        `inventory.released` event commit together or not at all.

        Nothing here decides whether a purchase happened. This is a claim on stock being
        withdrawn, and no payment exists in this application to have withdrawn it.
        """
        await self._mutation.require_allowed(merchant_id, capability=self._benchmark_capability)
        checkout = await self._checkouts.get_for_update(checkout_id, merchant_id=merchant_id)
        if checkout is None:
            raise NotFoundError("checkout", str(checkout_id))

        released = await self._inventory.release_for_checkout(checkout.id, reason=reason)
        await self._session.commit()
        return released

    async def execution_authorization(
        self, checkout_id: uuid.UUID, *, merchant_id: uuid.UUID, at: datetime | None = None
    ) -> CheckoutExecutionAuthorization:
        """Report what both gates say about this checkout, without holding anything.

        Informational. It writes nothing, locks nothing and reserves nothing, so its answer
        is what was true when it was asked and grants nothing at all. A caller that reads
        `authorized: true` here still has to prepare, and preparation evaluates all of this
        again under locks, because a mandate can be revoked and a checkout can expire in
        between.
        """
        checkout = await self._checkouts.get(checkout_id, merchant_id=merchant_id)
        if checkout is None:
            raise NotFoundError("checkout", str(checkout_id))

        mandate = await self._mandates.get(checkout.mandate_id, merchant_id=checkout.merchant_id)
        if mandate is None:
            # Not reachable through the schema: the foreign key onto the mandate is
            # RESTRICT, so the mandate cannot have been removed while this quote exists.
            raise NotFoundError("mandate", str(checkout.mandate_id))

        constraint_set = await self._constraints.get_for_mandate(
            checkout.mandate_id, merchant_id=checkout.merchant_id
        )
        return authorize_checkout_execution(
            checkout, mandate, constraint_set, at=at or datetime.now(UTC)
        )

    async def _load_locked(
        self, checkout_id: uuid.UUID, *, merchant_id: uuid.UUID
    ) -> tuple[CheckoutSession, SpendingMandate]:
        """One merchant's quote and the authorization it was written against, held until
        commit.

        The mandate is found through the checkout rather than supplied, which is what stops
        a checkout from being paired with an authorization someone chose afterwards. The
        constraint set is found through that mandate for the same reason.

        Merchant scoped from the first statement onwards. A quote belonging to somebody else
        is not found, so a foreign caller never reaches a lock at all and cannot make a
        merchant's own preparation or payment wait behind one.

        The first read is unlocked and is used for one thing, which is learning which
        mandate this quote names. That column is immutable at the database, so reading it
        without a lock cannot be wrong. Everything the decision actually rests on is read
        again below, under the lock, in the documented order: the mandate first, then the
        checkout. Doing it the other way round would put this operation and any operation
        that walks from a mandate downwards into a cycle.
        """
        found = await self._checkouts.get(checkout_id, merchant_id=merchant_id)
        if found is None:
            raise NotFoundError("checkout", str(checkout_id))

        # The merchant comes from the checkout that names this mandate, which the composite
        # foreign key already ties them together through, so this selects the same row an
        # unscoped read would have. It is required because the repository offers no unscoped
        # read, which is what makes one impossible to write by accident.
        mandate = await self._mandates.get_for_update(
            found.mandate_id, merchant_id=found.merchant_id
        )
        if mandate is None:
            # Not reachable through the schema: the foreign key onto the mandate is
            # RESTRICT, so the mandate cannot have been removed while this quote exists.
            raise NotFoundError("mandate", str(found.mandate_id))

        checkout = await self._checkouts.get_for_update(checkout_id, merchant_id=merchant_id)
        if checkout is None:
            raise NotFoundError("checkout", str(checkout_id))
        return checkout, mandate
