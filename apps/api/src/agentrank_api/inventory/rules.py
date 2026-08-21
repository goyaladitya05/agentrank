"""When a reservation holds stock, and for how long.

Pure domain code. No SQLAlchemy, no FastAPI, no clock reading, so the same inputs always
produce the same answer. The evaluation instant is an argument for the same reason it is
one in every other rule here: a function that reads the clock cannot be tested without
controlling the clock.

Two rules, and both exist so that inventory is never held longer than the thing holding it
is good for:

- a reservation expires no later than the checkout it was made for, and no later than the
  mandate that authorized it. Whichever comes first is the expiry
- a reservation is effective while it is ACTIVE and its expiry has not been reached. At
  exactly `expires_at` it is no longer effective, which is the same half open convention a
  mandate window and a checkout expiry already use
- a COMMITTED reservation is effective regardless of the clock. Expiry stops governing the
  moment a payment attempt is admitted against it, because admission was the instant the
  purchase was authorized and a provider operation cannot be withdrawn by time passing
"""

from datetime import datetime

from agentrank_api.inventory.models import InventoryReservation, ReservationStatus


def reservation_expires_at(
    checkout_expires_at: datetime, mandate_valid_until: datetime
) -> datetime:
    """The instant a reservation for this checkout stops holding stock.

    The earlier of the two, never a fixed window of its own. A reservation outliving the
    quote would hold stock for a price nobody may still accept, and one outliving the
    mandate would hold it for an authorization that has lapsed. A caller does not get to
    choose this, which is why it takes the two authoritative timestamps rather than a
    duration.
    """
    if checkout_expires_at.tzinfo is None or mandate_valid_until.tzinfo is None:
        raise ValueError("reservation expiry inputs must be timezone aware")
    return min(checkout_expires_at, mandate_valid_until)


def is_effective(reservation: InventoryReservation, *, at: datetime) -> bool:
    """Whether this reservation is holding stock at `at`.

    Three answers in one function, and the middle one is the Phase 1F addition.

    ACTIVE is expiry governed: it stops being effective without any row being touched, which
    is what makes the accounting correct with no sweeper job to fail.

    COMMITTED is not. Once a payment attempt has been admitted against a reservation, the
    hold is what that payment rests on, and the clock cannot take it back. A provider
    operation admitted while everything was valid stays valid while it completes, so the
    stock behind it stays held until that operation has a definitive answer.

    RELEASED and CONSUMED are terminal and hold nothing. CONSUMED specifically holds nothing
    because the units have already been taken out of `variant.inventory_quantity`, and
    counting them again as a hold would subtract the same purchase twice.
    """
    if at.tzinfo is None:
        raise ValueError("evaluation time must be timezone aware")
    if reservation.status is ReservationStatus.COMMITTED:
        return True
    return reservation.status is ReservationStatus.ACTIVE and at < reservation.expires_at
