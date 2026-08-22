"""The signature formula itself, pinned against a digest this repository did not compute.

Every other signature test in the suite builds its signature with `expected_signature` and then
verifies it with `signature_matches`, which share that function. That is the right shape for a
tamper test, because tampering is a difference between what was signed and what is claimed. It
is the wrong shape for checking the formula: if the message were joined with a colon, or the
operands were the other way round, or the digest were SHA-1, both halves would be wrong together
and every one of those tests would still pass.

So the vector below was produced by OpenSSL, from Razorpay's documented formula, outside this
codebase:

```text
printf '%s|%s' "$ORDER" "$PAYMENT" | openssl dgst -sha256 -hmac "$SECRET"
```

If the implementation drifts from what Razorpay documents, this is the test that fails, and it
is the only one that can. The negative vectors beside it are the three near misses that would
otherwise look identical from inside, each also computed by OpenSSL, so the test is capable of
failing for the right reason rather than merely present.

Razorpay's own documentation uses these identifier shapes in its worked example. The secret is a
fixed string in a test file and has never been a credential anywhere.
"""

import pytest
from pydantic import SecretStr

from agentrank_api.razorpay.signature import (
    SIGNATURE_PATTERN,
    expected_signature,
    signature_matches,
)

ORDER_ID = "order_9A33XWu170gUtm"
PAYMENT_ID = "pay_29QQoUBi66xm2f"
KEY_SECRET = SecretStr("razorpay_documentation_example_secret")

# HMAC-SHA256 of "order_9A33XWu170gUtm|pay_29QQoUBi66xm2f", computed by OpenSSL.
KNOWN_SIGNATURE = "2fe8806f94176e799d638aec77f26adbe492959ae3e415bee53e084f5c7e03e3"

# The same three inputs joined the wrong ways, also computed by OpenSSL. Each is what this
# application would produce if one detail of the documented formula were misread.
JOINED_WITH_A_COLON = "bb223d110c16205b800d0603ebc27c179a7c1f261325120a53a44d260867e557"
OPERANDS_REVERSED = "7f0a71a2dfdfd3ad0b727b7025746715ba2b9ea99aa7521665ff7f275726b61d"
JOINED_WITH_NOTHING = "0e85f018f96237136e951c2a517e0dfc277287b2722d28c0c01f3a202e407c15"


def test_the_formula_matches_what_razorpay_documents() -> None:
    """The known answer. Everything else in this file exists to make this one meaningful."""
    assert expected_signature(ORDER_ID, PAYMENT_ID, KEY_SECRET) == KNOWN_SIGNATURE


@pytest.mark.parametrize("near_miss", [JOINED_WITH_A_COLON, OPERANDS_REVERSED, JOINED_WITH_NOTHING])
def test_the_near_misses_are_genuinely_different(near_miss: str) -> None:
    """A known answer test is only worth having if the wrong answers are actually wrong.

    Each of these is a real HMAC over these three values, differing from the correct one by a
    single decision about the message. Asserting that none of them is what this application
    produces is what turns the test above from a tautology into a check.
    """
    assert near_miss != KNOWN_SIGNATURE
    assert expected_signature(ORDER_ID, PAYMENT_ID, KEY_SECRET) != near_miss


def test_a_genuine_signature_verifies() -> None:
    assert signature_matches(
        order_id=ORDER_ID,
        payment_id=PAYMENT_ID,
        presented=KNOWN_SIGNATURE,
        key_secret=KEY_SECRET,
    )


@pytest.mark.parametrize(
    ("order_id", "payment_id"),
    [
        ("order_SOMETHING_ELSE", PAYMENT_ID),
        (ORDER_ID, "pay_SOMETHING_ELSE"),
    ],
)
def test_changing_either_half_of_the_message_breaks_it(order_id: str, payment_id: str) -> None:
    """Both identifiers are in the message, which is what ties one payment to one order."""
    assert not signature_matches(
        order_id=order_id,
        payment_id=payment_id,
        presented=KNOWN_SIGNATURE,
        key_secret=KEY_SECRET,
    )


def test_a_different_secret_does_not_verify() -> None:
    assert not signature_matches(
        order_id=ORDER_ID,
        payment_id=PAYMENT_ID,
        presented=KNOWN_SIGNATURE,
        key_secret=SecretStr("a-different-secret"),
    )


@pytest.mark.parametrize(
    "presented",
    [
        "",
        "not-a-digest",
        KNOWN_SIGNATURE.upper(),
        KNOWN_SIGNATURE[:-1],
        f"{KNOWN_SIGNATURE}0",
        "signature-with-a-non-ascii-character-é",
        "2fe8806f94176e799d638aec77f26adbe492959ae3e415bee53e084f5c7e03é",
    ],
)
def test_a_value_that_cannot_be_a_digest_answers_no_rather_than_raising(
    presented: str,
) -> None:
    """There is one answer to "is this authentic" and it is no.

    The non ASCII cases are the reason this guard exists at all. `hmac.compare_digest` raises on
    a string it cannot encode, so without the shape check one accented character in a callback
    would turn a signature check into a 500, which is a denial of service handed to whoever
    posts it. Raising would also give a forger a way to tell a wrong signature from an
    unparseable one.
    """
    assert not signature_matches(
        order_id=ORDER_ID,
        payment_id=PAYMENT_ID,
        presented=presented,
        key_secret=KEY_SECRET,
    )


def test_the_shape_check_accepts_exactly_a_hex_sha256_digest() -> None:
    assert SIGNATURE_PATTERN.fullmatch(KNOWN_SIGNATURE)
    assert SIGNATURE_PATTERN.fullmatch("0" * 64)
    assert SIGNATURE_PATTERN.fullmatch("0" * 63) is None
    assert SIGNATURE_PATTERN.fullmatch("g" * 64) is None
