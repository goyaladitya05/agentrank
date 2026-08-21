"""Holding stock for a checkout, correctly, while other transactions try to do the same.

One operation matters here and everything else supports it: reserve the quantities a
checkout quoted, or reserve nothing at all. It is the answer to the race Phase 1C left
open, where two quotes could each observe the last unit and both look satisfiable.

The concurrency answer is PostgreSQL and nothing else. The variant rows are locked with
`SELECT FOR UPDATE` in a deterministic order, availability is calculated while those locks
are held, and the reservation is written before they are released. There is no Python lock,
no in memory registry and no reliance on the interpreter: a process local lock would not
survive a second worker, and a check made outside the transaction is a read that something
else can invalidate before the write lands.

Three rules shape this module:

- quantities come from the checkout lines and from nowhere else. A caller who could say
  "reserve one" for a quote of two would be choosing how much stock an authorization
  actually protects
- all or nothing. A quote for a charger and a cable where only the charger is available
  reserves neither, because half a basket held is a claim on stock that cannot become a
  purchase
- `variant.inventory_quantity` is never written. It stays the authoritative total, and what
  is available is that total minus the reservations effective right now

This service does not commit. The caller sets the transaction boundary, which is what lets
a reservation, its lines and its audit event be one unit of work. Holding a merchant's stock
and releasing it are both recorded, because both are things that happened to a merchant's
inventory on a buyer's behalf.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.audit.models import ActorType
from agentrank_api.audit.repository import AuditRepository
from agentrank_api.checkout.models import CheckoutSession
from agentrank_api.inventory.models import InventoryReservation, ReservationStatus
from agentrank_api.inventory.repository import InventoryReservationRepository
from agentrank_api.inventory.rules import is_effective

RESERVATION_RESOURCE = "inventory_reservation"
INVENTORY_RESERVED = "inventory.reserved"
INVENTORY_RELEASED = "inventory.released"

# Stock is held while preparing a purchase the buyer asked for, so it is the buyer's act,
# exactly as quoting is. This names a role and not a verified identity: nothing
# authenticates a caller yet.
RESERVATION_ACTOR = ActorType.BUYER


class ReleaseReason(StrEnum):
    """Why stock was given back.

    One member, because there is one way to release a reservation today. A payment phase
    that gives stock back after a failed attempt adds a member here rather than writing
    prose into a payload, so the trail stays answerable by a machine.
    """

    CHECKOUT_CANCELLED = "checkout_cancelled"


class InventoryViolationCode(StrEnum):
    """Why stock could not be held.

    Machine readable identifiers, not prose, for the same reason every other code in this
    project is one. A buyer agent has to tell "there is not enough of this" from "the claim
    you already had has lapsed" without reading English, because the two call for different
    next moves.
    """

    INSUFFICIENT_INVENTORY = "INSUFFICIENT_INVENTORY"
    RESERVATION_EXPIRED = "RESERVATION_EXPIRED"


@dataclass(frozen=True, slots=True)
class InventoryViolation:
    """One variant, what was wanted, and what was actually free.

    A bare code is not enough for a refusal a caller may want to act on. "Out of stock" is
    not actionable across a multi line basket; "variant X had 1 free and 2 were wanted" is.
    The numbers are what was true at the instant the decision was made, under the locks that
    made it stable.
    """

    code: InventoryViolationCode
    variant_id: uuid.UUID | None = None
    requested_quantity: int | None = None
    available_quantity: int | None = None


@dataclass(frozen=True, slots=True)
class ReservationOutcome:
    """What holding stock for one checkout produced.

    `reserved` is derived rather than stored, so an outcome carrying a violation cannot also
    claim to hold anything. `created` distinguishes a reservation written by this call from
    one that already existed, which is what makes a repeated preparation observably
    idempotent rather than merely harmless.
    """

    reservation: InventoryReservation | None = None
    created: bool = False
    violations: tuple[InventoryViolation, ...] = ()

    @property
    def reserved(self) -> bool:
        return self.reservation is not None and not self.violations


class InventoryReservationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._reservations = InventoryReservationRepository(session)
        self._audit = AuditRepository(session)

    async def lock_variants_for(self, checkout: CheckoutSession) -> None:
        """Take the variant locks this checkout will need, without deciding anything yet.

        `reserve` takes exactly these locks itself and is complete on its own. This exists
        for a caller that has to make a decision between acquiring them and writing, and
        the only such decision is the current time: a caller that blocked here for a minute
        has to find out before it commits, and it can only find out once it is no longer
        going to block.

        Taking them twice in one transaction is free. A transaction never waits on a lock
        it already holds, so the second acquisition inside `reserve` is a row lookup and
        nothing else. That redundancy is the price of leaving the reservation algorithm
        self contained, which is worth more than saving a statement: a lock separated from
        the accounting it protects is a lock that eventually gets forgotten.

        Last in the lock order, after the mandate and the checkout. See
        agentrank_api.locking.
        """
        quantities = checkout_quantities(checkout)
        if not quantities:
            # Not reachable through `CheckoutRepository.create`, which refuses an empty
            # quote. Stated anyway, because locking nothing must never look like locking.
            raise ValueError("a checkout with no lines has no variant rows to lock")

        await self._reservations.lock_variants(
            merchant_id=checkout.merchant_id, variant_ids=sorted(quantities, key=str)
        )

    async def reserve(
        self, checkout: CheckoutSession, *, expires_at: datetime, at: datetime
    ) -> ReservationOutcome:
        """Hold every quantity this checkout quoted, or hold none of them.

        The order is the whole point:

        1. read what the checkout quoted
        2. lock those variant rows, in a deterministic order
        3. only then look for an existing reservation, and only then count what is already
           held and compare it against the stock the locked rows report
        4. write the reservation before the locks are released

        Step 2 before step 3 is what makes a repeated preparation safe as well as a
        concurrent one. Two calls for the same checkout necessarily want the same variants,
        so they queue on the same locks, and the second one sees the first one's reservation
        rather than racing it into a second insert.

        `expires_at` is supplied by the caller because deriving it needs the mandate, which
        this service has no business loading. It is never a caller's own number: the
        preparation service derives it from the checkout and the mandate. `at` is the
        instant the accounting is done against, and it is an argument for the same reason
        every other rule here takes one.

        A success writes the reservation, its lines and one `inventory.reserved` event. They
        are one unit of work: if the audit append fails, no stock is held. A repeat writes
        no second event, because nothing happened the second time.

        Nothing is committed. A refusal leaves the transaction usable, and a success leaves
        the caller to decide what else belongs in it.
        """
        if at.tzinfo is None:
            raise ValueError("evaluation time must be timezone aware")

        quantities = checkout_quantities(checkout)
        if not quantities:
            # Not reachable through `CheckoutRepository.create`, which refuses an empty
            # quote. Stated anyway, because reserving nothing must never look like success.
            raise ValueError("a checkout with no lines cannot reserve inventory")

        stock = await self._reservations.lock_variants(
            merchant_id=checkout.merchant_id, variant_ids=sorted(quantities, key=str)
        )

        existing = await self._reservations.get_active_for_checkout(checkout.id)
        if existing is not None:
            if is_effective(existing, at=at):
                # Idempotent. The same claim on the same stock, not a second one.
                return ReservationOutcome(reservation=existing, created=False)
            # Unreachable from the preparation path: a reservation expires no later than
            # its checkout and its mandate, so by the time one has lapsed that checkout is
            # already refused by the authorization gates. Refused rather than renewed here,
            # because renewing a lapsed claim is a decision, not a detail.
            return ReservationOutcome(
                violations=(
                    InventoryViolation(
                        code=InventoryViolationCode.RESERVATION_EXPIRED,
                    ),
                )
            )

        reserved = await self._reservations.effective_reserved_quantities(
            variant_ids=list(quantities), at=at
        )
        violations = _shortfalls(checkout, stock, reserved)
        if violations:
            return ReservationOutcome(violations=violations)

        reservation = await self._reservations.create(
            merchant_id=checkout.merchant_id,
            checkout_id=checkout.id,
            expires_at=expires_at,
            quantities=quantities,
        )
        await self._append(reservation, INVENTORY_RESERVED, _reserved_payload(reservation))
        return ReservationOutcome(reservation=reservation, created=True)

    async def release(self, reservation: InventoryReservation, *, reason: ReleaseReason) -> bool:
        """Give the stock back, once, and record that it happened.

        Idempotent and terminal: releasing an already released reservation is not an error,
        does not move `released_at` and appends no second event. The return value says
        whether this call is what changed anything.

        No variant lock is taken. Releasing only ever frees capacity, so a concurrent
        reservation that has not yet seen it simply counts this stock as still held and
        refuses, which is conservative rather than wrong. The lock exists to stop two
        claims on one unit, and a release makes no claim.
        """
        if not await self._reservations.release(reservation):
            return False

        await self._append(reservation, INVENTORY_RELEASED, _released_payload(reservation, reason))
        return True

    async def release_for_checkout(self, checkout_id: uuid.UUID, *, reason: ReleaseReason) -> bool:
        """Release whatever this checkout is holding, if it is holding anything.

        The lookup and the release are one step so that a caller withdrawing a checkout
        cannot release the stock without recording it, or record it without releasing.
        """
        reservation = await self._reservations.get_active_for_checkout(checkout_id)
        if reservation is None:
            return False
        return await self.release(reservation, reason=reason)

    async def _append(
        self, reservation: InventoryReservation, event_type: str, payload: dict[str, Any]
    ) -> None:
        await self._audit.append(
            merchant_id=reservation.merchant_id,
            actor_type=RESERVATION_ACTOR,
            event_type=event_type,
            resource_type=RESERVATION_RESOURCE,
            resource_id=reservation.id,
            payload=payload,
        )


def checkout_quantities(checkout: CheckoutSession) -> dict[uuid.UUID, int]:
    """What this checkout quoted, as variants and unit counts.

    Read from the checkout lines, which are immutable at the database, so what gets reserved
    is exactly what was quoted and authorized. Reading `lines` raises unless they were
    loaded, which is deliberate: a reservation built from a collection that was never
    fetched would hold nothing and look like it held everything.
    """
    return {line.variant_id: line.quantity for line in checkout.lines}


def _shortfalls(
    checkout: CheckoutSession,
    stock: Mapping[uuid.UUID, int],
    reserved: Mapping[uuid.UUID, int],
) -> tuple[InventoryViolation, ...]:
    """Every line that cannot be held, in the order the lines were quoted.

    Every one is reported rather than only the first, because a caller adjusting a basket
    wants the whole picture at once. The order is fixed, so two runs of the same comparison
    produce the same refusal.

    A variant that is missing from `stock` counts as having none. The composite foreign key
    on the quote line means that cannot happen, and treating it as zero is the fail closed
    answer if it somehow does.
    """
    violations: list[InventoryViolation] = []
    for line in checkout.lines:
        available = stock.get(line.variant_id, 0) - reserved.get(line.variant_id, 0)
        if line.quantity > available:
            violations.append(
                InventoryViolation(
                    code=InventoryViolationCode.INSUFFICIENT_INVENTORY,
                    variant_id=line.variant_id,
                    requested_quantity=line.quantity,
                    # Never negative on the wire. Stock can fall below what is already held
                    # if a merchant lowers it, and reporting minus two units available
                    # would be arithmetic leaking out of an answer to "how many can I have".
                    available_quantity=max(available, 0),
                )
            )
    return tuple(violations)


def _reserved_payload(reservation: InventoryReservation) -> dict[str, Any]:
    """What was held, in the words of the reservation itself.

    The lines are recorded as well as the totals, so the trail answers which stock was held
    rather than merely how much. Nothing about price is here: a reservation is a claim on
    stock and the money is already recorded on the quote.
    """
    return {
        "checkout_id": str(reservation.checkout_id),
        "expires_at": reservation.expires_at.isoformat(),
        "line_count": len(reservation.lines),
        "total_quantity": reservation.total_quantity,
        "lines": [
            {"variant_id": str(line.variant_id), "quantity": line.quantity}
            for line in reservation.lines
        ],
    }


def _released_payload(reservation: InventoryReservation, reason: ReleaseReason) -> dict[str, Any]:
    """What stopped being held, and why.

    The reason is a stable code rather than prose, for the same reason an event type is one.
    The quantities are restated because they are what the merchant got back, and reading
    them should not need a join to a table.
    """
    return {
        "checkout_id": str(reservation.checkout_id),
        "reason": reason.value,
        "status": ReservationStatus.RELEASED.value,
        "line_count": len(reservation.lines),
        "total_quantity": reservation.total_quantity,
    }


def total_reserved(reservations: Sequence[InventoryReservation], *, at: datetime) -> int:
    """How many units these reservations hold at `at`, ignoring the ones that do not.

    Here rather than in a test helper because "what is actually held right now" is the
    question this whole module exists to answer, and it should have one implementation.
    """
    return sum(
        reservation.total_quantity
        for reservation in reservations
        if is_effective(reservation, at=at)
    )
