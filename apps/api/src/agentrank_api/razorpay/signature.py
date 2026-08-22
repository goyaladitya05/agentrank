"""Proving that a Standard Checkout callback came from Razorpay.

Razorpay documents the payment signature as an HMAC-SHA256 hex digest over the order identifier
and the payment identifier joined by a pipe, keyed with the API key secret:

```text
expected = hmac_sha256(f"{order_id}|{payment_id}", key_secret)
```

Two things about that formula are load bearing and both are easy to get wrong.

The first is which order identifier goes in. Razorpay's own documentation is explicit that the
order id must be the one held on the server and not the `razorpay_order_id` the checkout form
hands back. The reason is the obvious one: a browser that could choose both halves of the
message could compute a matching signature for any pair it liked, given a secret it does not
have, only if the secret leaked; but more simply, verifying a payload against itself proves
nothing about which payment this application was expecting. Anchoring on the stored identifier
is what ties the signature to the order this merchant actually created. So this function takes
the order id as a separate argument from anything a caller sent, and the service passes the
column.

The second is the comparison. A signature check written with `==` leaks, through timing, how
many leading characters of a forged digest were right, and an attacker who can measure that can
build a valid one character at a time. `hmac.compare_digest` takes the same time whatever the
inputs are.

The secret never appears in a return value, a log line, an exception message or an audit
payload. It is unwrapped from its `SecretStr` inside this module and inside the transport, and
nowhere else in the application.
"""

import hashlib
import hmac
import re

from pydantic import SecretStr

# A hex encoded SHA-256 digest and nothing else. Checked before the comparison for two reasons.
# `hmac.compare_digest` raises on a string containing non ASCII characters, so an attacker could
# otherwise turn a verification into a 500 with one Unicode character. And a value of the wrong
# shape is not a signature that failed, it is not a signature at all.
SIGNATURE_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def expected_signature(order_id: str, payment_id: str, key_secret: SecretStr) -> str:
    """The digest Razorpay would have produced for this order and payment.

    Public because a test has to be able to produce a genuine signature without reimplementing
    the formula, and a test that reimplemented it would pass while both copies were wrong
    together. The tamper tests build real signatures with this and then change one input.
    """
    message = f"{order_id}|{payment_id}".encode()
    return hmac.new(key_secret.get_secret_value().encode(), message, hashlib.sha256).hexdigest()


def signature_matches(
    *, order_id: str, payment_id: str, presented: str, key_secret: SecretStr
) -> bool:
    """Whether this callback is authentic, decided in constant time.

    Keyword only, because the three strings are all opaque identifiers of similar shape and a
    positional call that swapped two of them would compile, run, and reject every genuine
    payment.

    `order_id` is the caller's responsibility to source correctly, and there is exactly one
    correct source: the `provider_order_id` column on the binding. Passing the identifier from
    the callback body would make this function verify a payload against itself.

    False rather than an exception for a malformed signature. There is one answer to "is this
    authentic" and it is no; raising would give a forger a way to tell a wrong signature from an
    unparseable one, and would turn one Unicode character into a 500.
    """
    if SIGNATURE_PATTERN.fullmatch(presented) is None:
        return False
    return hmac.compare_digest(expected_signature(order_id, payment_id, key_secret), presented)
