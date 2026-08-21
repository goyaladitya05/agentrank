"""What makes an idempotency key one, and where one comes from when a caller has none.

Pure domain code. No SQLAlchemy, no FastAPI, no clock reading.

An idempotency key names one logical payment operation. Two requests carrying the same key
against the same checkout are the same request, whatever else differs about them, and the
second one gets the first one's answer rather than a second payment. The same string is what
a provider is given, so there is one identity end to end rather than an application identity
and a provider identity that have to be correlated afterwards.
"""

import re
import uuid

from agentrank_api.payments.models import (
    IDEMPOTENCY_KEY_PATTERN,
    MAX_IDEMPOTENCY_KEY_LENGTH,
    MIN_IDEMPOTENCY_KEY_LENGTH,
)

# Server generated keys carry a prefix so that a key this application invented is
# distinguishable from one a caller chose, in a log, in a provider dashboard, and in a
# support conversation.
GENERATED_PREFIX = "ar"

_KEY = re.compile(IDEMPOTENCY_KEY_PATTERN)


def validate_idempotency_key(key: str) -> None:
    """Reject anything that cannot serve as an identity.

    Bounded on both sides. Too short and two unrelated requests collide by accident, which
    would make one payment answer for another; too long and it stops fitting in the places it
    has to travel. The character set is what survives a URL, a header and a provider API
    without being escaped differently in each, because a key that is encoded two ways is two
    keys.

    A format check and not a registry. This application does not decide which keys a caller
    may invent, only that what arrives can be compared for equality everywhere it is used.
    """
    if _KEY.fullmatch(key) is None:
        raise ValueError(
            f"an idempotency key is {MIN_IDEMPOTENCY_KEY_LENGTH} to"
            f" {MAX_IDEMPOTENCY_KEY_LENGTH} characters of letters, digits and _.:-,"
            f" got {key!r}"
        )


def generate_idempotency_key() -> str:
    """A key for a caller that supplied none.

    Version 7, so keys are time ordered and two generated in the same process cannot collide.

    Generating one is safe but it is not retry safety. A caller that lets this happen gets a
    different identity on every request, so a repeat is a new logical operation rather than
    the same one, and it is refused while the first is still in flight rather than answered
    with the first one's result. Retry safety requires the caller to choose a key and send it
    again, which is why the API accepts one.
    """
    return f"{GENERATED_PREFIX}-{uuid.uuid7().hex}"
