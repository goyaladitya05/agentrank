"""The identity that leaves this application, and the caller string that does not.

Two properties are worth this file. The first is arithmetic: a derived reference is
deterministic, is unique per attempt, and fits inside what a provider will accept. The second
is the one Phase 1H found and deliberately left: two merchants presenting the same idempotency
key must not collide at a provider that keeps one namespace per account.

The second property is asserted twice over, once on the pure function and once against a real
database with two merchants, two mandates, two checkouts and one key string. The pure test says
the derivation is right. The database test says the derivation is what the application actually
uses, which is a different claim and the one that would break silently.
"""

import re
import uuid

import pytest
from commerce_support import admit, build_shop, quote
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.payments.execution import _instruction
from agentrank_api.payments.references import (
    MAX_OPERATION_REFERENCE_LENGTH,
    OPERATION_REFERENCE_PREFIX,
    provider_operation_reference,
)

pytestmark = pytest.mark.anyio

# The same string, presented by two unrelated merchants. That is legal here and always was: an
# idempotency key is scoped by the checkout it is presented against.
SHARED_KEY = "checkout-0001"

# Razorpay documents an order receipt as ASCII and at most 40 characters. Anything outside this
# would either be rejected or would have to be escaped differently in different places, and a
# reference that is encoded two ways is two references.
SAFE_REFERENCE = re.compile(r"^[a-z0-9_]{1,40}$")


def test_a_reference_is_deterministic() -> None:
    """The same attempt always produces the same identity, or recovery is impossible.

    Recovering a provider order after a lost create response means asking the provider about a
    reference this application can recompute. A derivation that varied would have to be stored
    before the call and would still be lost if that store failed, which is the same problem one
    layer down.
    """
    merchant_id = uuid.uuid7()
    attempt_id = uuid.uuid7()

    assert provider_operation_reference(merchant_id, attempt_id) == provider_operation_reference(
        merchant_id, attempt_id
    )


def test_a_reference_fits_what_a_provider_accepts() -> None:
    """Bounded and unescaped, because the place it lands is bounded."""
    generated = provider_operation_reference(uuid.uuid7(), uuid.uuid7())

    assert len(generated) <= MAX_OPERATION_REFERENCE_LENGTH
    assert SAFE_REFERENCE.fullmatch(generated)
    assert generated.startswith(f"{OPERATION_REFERENCE_PREFIX}_")


def test_two_merchants_with_one_attempt_identifier_get_different_references() -> None:
    """The merchant is genuinely in the digest rather than decorative.

    A version 7 attempt identifier is already globally unique, so a derivation that ignored the
    merchant would still be collision free and would still pass every other test here. This is
    the one that says the namespace is derived from merchant identity.
    """
    attempt_id = uuid.uuid7()

    first = provider_operation_reference(uuid.uuid7(), attempt_id)
    second = provider_operation_reference(uuid.uuid7(), attempt_id)

    assert first != second


def test_two_attempts_under_one_merchant_get_different_references() -> None:
    merchant_id = uuid.uuid7()

    first = provider_operation_reference(merchant_id, uuid.uuid7())
    second = provider_operation_reference(merchant_id, uuid.uuid7())

    assert first != second


def test_a_caller_key_cannot_influence_the_reference() -> None:
    """Nothing a caller sends is an input, so nothing a caller sends can aim it.

    Stated as a test rather than as a comment because the whole defect being closed is that a
    caller chosen string used to be the provider namespace. A signature that took one would
    make this file compile and would reintroduce it.
    """
    parameters = provider_operation_reference.__code__.co_varnames[
        : provider_operation_reference.__code__.co_argcount
    ]

    assert parameters == ("merchant_id", "attempt_id")


async def test_two_merchants_using_one_key_produce_different_provider_references(
    session: AsyncSession,
) -> None:
    """The Phase 1H finding, closed, through the path the application actually uses.

    Both merchants present `checkout-0001`. Inside the application that is two payments and it
    always was, because the unique constraint is on the checkout and the key together. What is
    new is that the two identities reaching a shared provider account are also different, so
    neither can deduplicate against the other and neither can be refused as a duplicate receipt
    belonging to somebody else.

    The references are read off `_instruction`, which is what a dispatch hands a provider,
    rather than recomputed here. A test that recomputed them would keep passing if the
    application went back to forwarding the caller's key.
    """
    first_shop = await build_shop(session, "ampere-supply")
    second_shop = await build_shop(session, "volta-goods")

    first = await admit(session, first_shop, await quote(session, first_shop), key=SHARED_KEY)
    first_instruction = _instruction(first)
    second = await admit(session, second_shop, await quote(session, second_shop), key=SHARED_KEY)
    second_instruction = _instruction(second)

    assert first.id != second.id
    assert first_instruction.idempotency_key == second_instruction.idempotency_key == SHARED_KEY
    assert first_instruction.operation_reference != second_instruction.operation_reference
