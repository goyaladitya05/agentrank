"""The fake provider is a test instrument, so it gets tested.

Everything else in this phase will assert "the provider was called exactly once" or "the
provider reported success". Those assertions are only worth as much as the thing making them,
so the fake's own contract is pinned here: deterministic outcomes, real idempotency, a ledger
that is separate from this application's state, and a query that never charges.

No database. None of this touches one, because a payment provider does not have access to
ours and the whole point of the instruction is that it never did.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from agentrank_api.payments.fake import (
    DEFAULT_DECLINE_CODE,
    FakeOutcome,
    FakePaymentProvider,
)
from agentrank_api.payments.provider import (
    PaymentInstruction,
    PaymentProvider,
    PaymentQuery,
    ProviderOutcome,
    ProviderQueryResult,
    ProviderRecord,
)

pytestmark = pytest.mark.anyio

KEY = "pay-ampere-0001"
OTHER_KEY = "pay-ampere-0002"
DISPATCHED_AT = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
MINUTE = timedelta(minutes=1)


def reference(key: str = KEY) -> str:
    """A stand in for a derived operation reference, stable per key inside one test.

    The real one comes from `provider_operation_reference` and is a digest over a merchant and
    an attempt. Nothing here has either, and what this file tests is the fake's own contract
    rather than the derivation, so a recognizable string keyed the same way is enough. The two
    are kept apart on purpose: a test that reproduced the derivation would pass even if the
    derivation stopped being used.
    """
    return f"ar_test_{key}"


def question(key: str = KEY, *, operation_reference: str | None = None) -> PaymentQuery:
    return PaymentQuery(
        operation_reference=operation_reference or reference(key),
        idempotency_key=key,
        dispatched_at=DISPATCHED_AT,
    )


def instruction(
    key: str = KEY, *, amount_minor: int = 499900, operation_reference: str | None = None
) -> PaymentInstruction:
    return PaymentInstruction(
        attempt_id=uuid.uuid7(),
        operation_reference=operation_reference or reference(key),
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

    found = await provider.query(question())
    assert found.outcome is ProviderOutcome.UNKNOWN
    assert found.record is ProviderRecord.ABSENT


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

    found = await provider.query(question())
    assert found.outcome is ProviderOutcome.SUCCEEDED
    assert found.record is ProviderRecord.PRESENT
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

    found = await provider.query(question())

    assert found.record is ProviderRecord.ABSENT
    assert provider.charges == 0
    assert provider.executions == []
    assert provider.queries == [KEY]


async def test_the_provider_records_what_it_was_asked_to_charge() -> None:
    """The instruction is a frozen record, and the provider sees exactly what it holds."""
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


async def test_absence_is_not_final_without_a_visibility_window() -> None:
    """The default answer for a provider that has promised nothing.

    A processor that cannot guarantee when its records become visible must say "no record
    right now" forever, because the alternative is releasing a merchant's stock under a charge
    that may still be on its way.
    """
    provider = FakePaymentProvider(clock=DISPATCHED_AT + 100 * MINUTE)

    found = await provider.query(question())

    assert found.record is ProviderRecord.ABSENT
    assert found.outcome is ProviderOutcome.UNKNOWN


async def test_absence_stays_temporary_inside_the_visibility_window() -> None:
    provider = FakePaymentProvider(clock=DISPATCHED_AT + 4 * MINUTE, visibility_window=5 * MINUTE)

    found = await provider.query(question())

    assert found.record is ProviderRecord.ABSENT


async def test_absence_becomes_final_once_the_visibility_window_has_passed() -> None:
    """The answer that lets an unresolved payment end, stated by the provider and by nothing else.

    The window is measured from the dispatch instant this application supplied, and the
    provider's own clock decides that it has passed. No duration appears above the interface.
    """
    provider = FakePaymentProvider(visibility_window=5 * MINUTE)
    provider.clock = DISPATCHED_AT + 5 * MINUTE

    found = await provider.query(question())

    assert found.record is ProviderRecord.NEVER_EXECUTED
    assert found.outcome is ProviderOutcome.UNKNOWN
    assert provider.charges == 0


async def test_a_recorded_identity_is_never_reported_as_never_executed() -> None:
    """A window elapsing does not overwrite a fact the provider actually holds."""
    provider = FakePaymentProvider(default=FakeOutcome.LOST_RESPONSE, visibility_window=5 * MINUTE)
    provider.clock = DISPATCHED_AT
    await provider.execute(instruction())
    provider.clock = DISPATCHED_AT + 100 * MINUTE

    found = await provider.query(question())

    assert found.record is ProviderRecord.PRESENT
    assert found.outcome is ProviderOutcome.SUCCEEDED


async def test_time_advances_only_when_a_test_advances_it() -> None:
    """No sleeps and no real clock, so a window elapses because somebody said so."""
    provider = FakePaymentProvider(visibility_window=5 * MINUTE)
    provider.clock = DISPATCHED_AT + 4 * MINUTE

    before = await provider.query(question())
    provider.clock = DISPATCHED_AT + 6 * MINUTE
    after = await provider.query(question())

    assert before.record is ProviderRecord.ABSENT
    assert after.record is ProviderRecord.NEVER_EXECUTED


async def test_an_identity_is_replayed_while_the_retention_window_holds() -> None:
    provider = FakePaymentProvider(default=FakeOutcome.SUCCESS, idempotency_retention=10 * MINUTE)
    provider.clock = DISPATCHED_AT

    first = await provider.execute(instruction())
    provider.clock = DISPATCHED_AT + 9 * MINUTE
    second = await provider.execute(instruction())

    assert second.reference == first.reference
    assert provider.charges == 1


async def test_a_forgotten_identity_is_executed_again() -> None:
    """Provider idempotency is finite everywhere, and the fake says so out loud.

    This is what a real processor does once its idempotency retention has passed, and it is
    exactly why this application never relies on it. Nothing above this interface may treat a
    provider's memory as the thing that stops a second charge.
    """
    provider = FakePaymentProvider(default=FakeOutcome.SUCCESS, idempotency_retention=10 * MINUTE)
    provider.clock = DISPATCHED_AT

    await provider.execute(instruction())
    provider.clock = DISPATCHED_AT + 10 * MINUTE
    await provider.execute(instruction())

    # Two operations, because the provider genuinely forgot the first one.
    assert provider.charges == 2
    assert provider.executions_for(KEY) == 2


async def test_retention_does_not_make_the_provider_forget_the_payment() -> None:
    """An idempotency window and a payment record are different things.

    Collapsing them would make this fake report that a payment it definitely made never
    happened, which is the one lie a provider must never be able to tell.
    """
    provider = FakePaymentProvider(
        default=FakeOutcome.LOST_RESPONSE,
        idempotency_retention=10 * MINUTE,
        visibility_window=MINUTE,
    )
    provider.clock = DISPATCHED_AT
    await provider.execute(instruction())
    provider.clock = DISPATCHED_AT + 100 * MINUTE

    found = await provider.query(question())

    assert found.record is ProviderRecord.PRESENT
    assert found.outcome is ProviderOutcome.SUCCEEDED


def test_a_query_result_cannot_claim_an_outcome_it_has_no_record_for() -> None:
    """Two incompatible statements, caught at the moment a provider makes them."""
    with pytest.raises(ValueError, match="ABSENT"):
        ProviderQueryResult(outcome=ProviderOutcome.SUCCEEDED, record=ProviderRecord.ABSENT)

    with pytest.raises(ValueError, match="NEVER_EXECUTED"):
        ProviderQueryResult(outcome=ProviderOutcome.FAILED, record=ProviderRecord.NEVER_EXECUTED)


async def test_one_caller_key_under_two_operation_references_is_two_charges() -> None:
    """The namespacing property, asserted at the boundary that has to hold it.

    Two merchants may present the same idempotency key, because inside this application a key
    is scoped by a checkout and a checkout belongs to one merchant. A provider account has no
    such scoping, so the identity that reaches it is derived instead. If the fake deduplicated
    on the caller's key, the second payment here would silently inherit the first one's result
    and no money would move for it.
    """
    provider = FakePaymentProvider(default=FakeOutcome.SUCCESS)

    first = await provider.execute(instruction(KEY, operation_reference="ar_merchant_a"))
    second = await provider.execute(instruction(KEY, operation_reference="ar_merchant_b"))

    assert first.outcome is ProviderOutcome.SUCCEEDED
    assert second.outcome is ProviderOutcome.SUCCEEDED
    assert first.reference != second.reference
    assert provider.charges == 2
    assert len(provider.ledger) == 2


async def test_a_query_reads_the_ledger_by_operation_reference() -> None:
    """Asking under the caller's key would ask about whoever else used that string."""
    provider = FakePaymentProvider(default=FakeOutcome.SUCCESS)
    await provider.execute(instruction(KEY, operation_reference="ar_merchant_a"))

    mine = await provider.query(question(KEY, operation_reference="ar_merchant_a"))
    theirs = await provider.query(question(KEY, operation_reference="ar_merchant_b"))

    assert mine.record is ProviderRecord.PRESENT
    assert mine.outcome is ProviderOutcome.SUCCEEDED
    assert theirs.record is ProviderRecord.ABSENT
    assert theirs.outcome is ProviderOutcome.UNKNOWN
