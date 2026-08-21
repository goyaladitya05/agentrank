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

    Released is terminal, so a released reservation is never effective again. An expired
    one stops being effective without any row being touched, which is what makes the
    accounting correct with no sweeper job to fail.
    """
    if at.tzinfo is None:
        raise ValueError("evaluation time must be timezone aware")
    return reservation.status is ReservationStatus.ACTIVE and at < reservation.expires_at
