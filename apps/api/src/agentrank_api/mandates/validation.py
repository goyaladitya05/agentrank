"""Rules about a mandate.

Two questions live here, and they are not the same question:

- may this mandate be created at all, which is about the request
- may this mandate be used right now, which is about the clock and the status

Neither of them is "does this checkout satisfy this mandate". That is Phase 1C, and
keeping it out of here is what stops the two from blurring.
"""

from datetime import datetime


def validate_validity_window(valid_from: datetime, valid_until: datetime) -> None:
    """Reject a window that ends before it starts.

    Also refused by a database check constraint. It is stated here as well so that the
    API can answer with a message naming the fields rather than surfacing an integrity
    error, and so that a service caller gets the same refusal as an HTTP caller.
    """
    if valid_from.tzinfo is None or valid_until.tzinfo is None:
        raise ValueError("validity window timestamps must be timezone aware")
    if valid_until <= valid_from:
        raise ValueError("valid_until must be after valid_from")
