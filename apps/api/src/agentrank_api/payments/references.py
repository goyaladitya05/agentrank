"""The identity this application gives a payment provider, which is not the caller's key.

Two strings name one payment and they are deliberately different strings.

```text
idempotency_key        chosen by the caller, scoped to one checkout, never leaves
                       this application as an identity
operation reference    derived here from authoritative identity, globally unique
                       inside one shared provider account, and the only thing a
                       provider is asked to be idempotent on
```

The distinction is the whole of this module and it is a correctness property rather than a
naming preference. Inside this application an idempotency key is scoped by the checkout it was
presented against, and a checkout belongs to exactly one merchant, so two merchants choosing
the string `order-1` produce two unrelated payments that neither can read. That scoping is
structural and it is tested.

What has no such scoping is a payment provider. A provider account is one namespace, and
several merchants sharing one account is not a hypothetical: it is the ordinary arrangement in
a Razorpay Test Mode integration and the one this project starts in. Forward the caller's
string and the second merchant's payment inherits the first one's provider record, which for a
receipt treated as an idempotency key means the second order is rejected as a duplicate of a
payment belonging to somebody else.

So the reference is derived rather than accepted. It is a digest over the merchant and the
payment attempt, both of which are immutable at the database and neither of which any caller
can state. A caller can choose its own key freely, including choosing one another merchant is
already using, and it changes nothing about what reaches a provider.

Deterministic, and that is load bearing twice. Creating a provider order is an external side
effect that cannot be committed atomically with a database transaction, so a lost response has
to be recoverable by asking the provider about an identity this application can recompute
without having stored anything. And a repeat of the same preparation has to arrive at the same
identity rather than at a second one.

Bounded, because the place this lands is bounded. Razorpay documents an order receipt as at
most 40 characters and unique on the account, so the reference is 35 characters of ASCII
letters, digits and one underscore, which needs no escaping anywhere it travels. Two raw UUIDs
would be 64 characters before any separator, which is why this is a digest and not a
concatenation. The attempt identifier travels beside the order in the provider's own notes,
so a human reading a provider dashboard can still get back to the row.
"""

import base64
import hashlib
import uuid

# What an AgentRank issued reference starts with, so that one is recognizable in a provider
# dashboard shared with whatever else that account is used for.
OPERATION_REFERENCE_PREFIX = "ar"

# Domain separation. A digest over two UUIDs with no context is a digest that could collide
# with any other digest over two UUIDs computed anywhere else in this system for a different
# purpose. Versioned, because changing what goes into the digest changes every reference and
# must be a deliberate act with a name rather than a silent edit.
_DIGEST_DOMAIN = b"agentrank.payment.operation-reference.v1"

# Twenty bytes of SHA-256, which is 160 bits and encodes to exactly 32 base32 characters with
# no padding. Truncating a digest costs collision resistance and 160 bits leaves far more than
# any provider account will ever need.
_DIGEST_BYTES = 20

# Razorpay's documented ceiling for an order receipt. Stated here rather than in the Razorpay
# package because this is the module that has to fit inside it.
MAX_OPERATION_REFERENCE_LENGTH = 40


def provider_operation_reference(merchant_id: uuid.UUID, attempt_id: uuid.UUID) -> str:
    """The identity a provider is given for one payment operation.

    Pure. No clock, no randomness, no database and no caller input, so the same attempt always
    produces the same reference and two attempts never produce one.

    Both identifiers are immutable on `payment_attempt`: the guard trigger refuses an update to
    either, and the composite foreign key ties the merchant to the checkout the attempt was
    admitted against. So this is derived from authoritative state in the strong sense, rather
    than from values that happened to be correct when they were read.

    The merchant is in the digest even though a version 7 attempt identifier is already
    globally unique. That is deliberate: the property being enforced is that the provider
    namespace is derived from merchant identity, and a reference that would still be unique
    without it would leave that property resting on the identifier generator instead of on
    this function.
    """
    digest = hashlib.sha256(
        _DIGEST_DOMAIN + merchant_id.bytes + attempt_id.bytes,
    ).digest()[:_DIGEST_BYTES]
    encoded = base64.b32encode(digest).decode("ascii").lower()
    return f"{OPERATION_REFERENCE_PREFIX}_{encoded}"
