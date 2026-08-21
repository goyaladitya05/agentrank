"""The fake provider is a test instrument, so it gets tested.

Everything else in this phase will assert "the provider was called exactly once" or "the
provider reported success". Those assertions are only worth as much as the thing making them,
so the fake's own contract is pinned here: deterministic outcomes, real idempotency, a ledger
that is separate from this application's state, and a query that never charges.

No database. None of this touches one, because a payment provider does not have access to
ours and the whole point of the instruction is that it never did.
"""

import uuid

import pytest

from agentrank_api.payments.fake import (
    DEFAULT_DECLINE_CODE,
    FakeOutcome,
    FakePaymentProvider,
)
from agentrank_api.payments.provider import (
    PaymentInstruction,
    PaymentProvider,
    ProviderOutcome,
)

pytestmark = pytest.mark.anyio

KEY = "pay-ampere-0001"
OTHER_KEY = "pay-ampere-0002"


def instruction(key: str = KEY, *, amount_minor: int = 499900) -> PaymentInstruction:
    return PaymentInstruction(
        attempt_id=uuid.uuid7(),
        idempotency_key=key,
        amount_minor=amount_minor,
        currency="INR",
        merchant_reference=str(uuid.uuid7()),
        checkout_reference=str(uuid.uuid7()),
    )


def test_the_fake_satisfies_the_provider_protocol() -> None:
    """Structural typing is only useful if something checks the structure."""
    provider: PaymentProvider = FakePaymentProvider()

    assert provider is not None


async def test_a_configured_success_succeeds_and_carries_a_reference() -> None:
    provider = FakePaymentProvider(default=FakeOutcome.SUCCESS)

    result = await provider.execute(instruction())

    assert result.outcome is ProviderOutcome.SUCCEEDED
    assert result.reference is not None
    assert result.failure_code is None
    assert provider.charges == 1


async def test_a_configured_decline_fails_and_carries_a_code() -> None:
    provider = FakePaymentProvider(default=FakeOutcome.DECLINE)

    result = await provider.execute(instruction())

    assert result.outcome is ProviderOutcome.FAILED
    assert result.failure_code == DEFAULT_DECLINE_CODE
    assert result.reference is None
    # A decline is not a charge.
    assert provider.charges == 0


async def test_an_ambiguous_result_records_nothing() -> None:
    """Nothing reached the provider, so nothing can be found later."""
    provider = FakePaymentProvider(default=FakeOutcome.AMBIGUOUS)

    result = await provider.execute(instruction())

    assert result.outcome is ProviderOutcome.UNKNOWN
    assert result.reference is None
    assert provider.charges == 0

    found = await provider.query(KEY)
    assert found.outcome is ProviderOutcome.UNKNOWN
    assert found.known is False


async def test_a_lost_response_records_a_success_the_caller_never_hears_about() -> None:
    """The case this whole phase is shaped around.

    The money moved and the answer did not come back. A system that treats this as a failure
    releases the stock and invites a second payment for a charge that already went through.
    """
    provider = FakePaymentProvider(default=FakeOutcome.LOST_RESPONSE)

    result = await provider.execute(instruction())

    assert result.outcome is ProviderOutcome.UNKNOWN
    assert result.reference is None
    # The provider disagrees with the caller, which is exactly the state worth simulating.
    assert provider.charges == 1

    found = await provider.query(KEY)
    assert found.outcome is ProviderOutcome.SUCCEEDED
    assert found.known is True
    assert found.reference is not None


async def test_two_executes_under_one_key_make_one_charge() -> None:
    """Provider side idempotency, which is separate from the application's own.

    This application never issues the second call. The fake behaves like a real idempotent
    provider anyway, so that the two properties can be tested apart rather than one being
    assumed from the other.
    """
    provider = FakePaymentProvider(default=FakeOutcome.SUCCESS)

    first = await provider.execute(instruction())
    second = await provider.execute(instruction())

    assert first.outcome is ProviderOutcome.SUCCEEDED
    assert second.outcome is ProviderOutcome.SUCCEEDED
    assert second.reference == first.reference
    assert provider.charges == 1
    # Two calls, one charge. Both facts are recorded, because a test that cannot tell them
    # apart cannot assert idempotency at all.
    assert provider.executions_for(KEY) == 2


async def test_a_repeat_after_an_ambiguous_result_is_performed_again() -> None:
    """Honest rather than convenient.

    The provider recorded nothing the first time, so it has nothing to return and does the
    work. This application never sends that second call, and the fake does not pretend the
    case away by inventing a memory the provider does not have.
    """
    provider = FakePaymentProvider(default=FakeOutcome.AMBIGUOUS)

    await provider.execute(instruction())
    provider.set_outcome(KEY, FakeOutcome.SUCCESS)
    second = await provider.execute(instruction())

    assert second.outcome is ProviderOutcome.SUCCEEDED
    assert provider.executions_for(KEY) == 2


async def test_outcomes_are_configured_per_identity() -> None:
    """Two payments in one test can behave differently without the test ordering them."""
    provider = FakePaymentProvider(default=FakeOutcome.SUCCESS)
    provider.set_outcome(OTHER_KEY, FakeOutcome.DECLINE)

    first = await provider.execute(instruction(KEY))
    second = await provider.execute(instruction(OTHER_KEY))

    assert first.outcome is ProviderOutcome.SUCCEEDED
    assert second.outcome is ProviderOutcome.FAILED


async def test_the_same_configuration_always_produces_the_same_answer() -> None:
    """No randomness anywhere. A provider that failed one time in ten proves nothing."""
    outcomes = []
    for _ in range(5):
        provider = FakePaymentProvider(default=FakeOutcome.DECLINE)
        result = await provider.execute(instruction())
        outcomes.append((result.outcome, result.failure_code))

    assert len(set(outcomes)) == 1


async def test_a_query_never_charges() -> None:
    provider = FakePaymentProvider(default=FakeOutcome.SUCCESS)

    found = await provider.query(KEY)

    assert found.known is False
    assert provider.charges == 0
    assert provider.executions == []
    assert provider.queries == [KEY]


async def test_the_provider_records_what_it_was_asked_to_charge() -> None:
    """The instruction is five frozen values, and the provider sees exactly those."""
    provider = FakePaymentProvider()
    sent = instruction(amount_minor=123456)

    await provider.execute(sent)

    assert provider.executions == [sent]
    recorded = provider.executions[0]
    assert recorded.amount_minor == 123456
    assert recorded.currency == "INR"
    assert recorded.idempotency_key == KEY


def test_an_instruction_cannot_be_changed_after_it_is_built() -> None:
    """A provider is handed this, and a provider is not this application's code."""
    sent = instruction()

    with pytest.raises(AttributeError):
        sent.amount_minor = 1  # type: ignore[misc]
