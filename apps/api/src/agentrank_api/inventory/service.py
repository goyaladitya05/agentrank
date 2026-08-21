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
a reservation, its lines and its audit event be one unit of work.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.checkout.models import CheckoutSession
from agentrank_api.inventory.models import InventoryReservation
from agentrank_api.inventory.repository import InventoryReservationRepository
from agentrank_api.inventory.rules import is_effective


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
        return ReservationOutcome(reservation=reservation, created=True)

    async def release(self, reservation: InventoryReservation) -> bool:
        """Give the stock back, once.

        Idempotent and terminal: releasing an already released reservation is not an error,
        does not move `released_at` and reports that it changed nothing, so a caller can
        record exactly one event for exactly one real transition.

        No variant lock is taken. Releasing only ever frees capacity, so a concurrent
        reservation that has not yet seen it simply counts this stock as still held and
        refuses, which is conservative rather than wrong. The lock exists to stop two
        claims on one unit, and a release makes no claim.
        """
        return await self._reservations.release(reservation)


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
