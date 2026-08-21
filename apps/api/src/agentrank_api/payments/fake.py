"""A payment provider that does exactly what it is told, and remembers what it was asked.

This is the only provider that exists. Phase 1F is deliberately provider independent: the
execution kernel has to be correct before a real processor is involved, and a real processor
cannot be made to time out on demand, decline on demand, or lose a response on demand, which
are the three things worth testing hardest.

Nothing here is random. There is no `random`, no clock reading and no probability. An outcome
is either configured for a specific idempotency key or it is the default, and both are set by
whoever constructed the fake. A provider that failed one time in ten would make every test
that touched it flaky and would prove nothing about the case it did not happen to produce.

It behaves like an idempotent provider rather than like a stub. Its ledger is keyed by
idempotency key, so a second execute under one key returns the first one's result and creates
no second charge, which is what lets a test validate provider idempotency and application
idempotency separately rather than hoping one covers the other.

The four outcomes are the four things a real processor does:

```text
SUCCESS         the charge goes through and the caller is told
DECLINE         the charge is refused and the caller is told
AMBIGUOUS       nothing is recorded and the caller is told nothing definite
LOST_RESPONSE   the charge goes through and the caller is told nothing definite
```

The last one is the case this whole phase is shaped around, and it is why AMBIGUOUS alone is
not enough. In AMBIGUOUS the provider genuinely did nothing, so reconciliation finds nothing.
In LOST_RESPONSE the money moved and only the answer was lost, so reconciliation finds a
success and the payment has to be honoured. A system that cannot tell those apart cannot be
tested for the failure that actually costs money.

It records every call. `executions` and `queries` are the evidence a test needs to assert that
a provider was called exactly once, or not at all, or with the same identity twice, which is
the difference between an idempotency test and a test of what the database happens to contain.
"""

from dataclasses import dataclass, field
from enum import StrEnum

from agentrank_api.payments.provider import (
    PaymentInstruction,
    ProviderOutcome,
    ProviderQueryResult,
    ProviderResult,
)

DEFAULT_DECLINE_CODE = "CARD_DECLINED"

# The prefix a fake provider reference carries. Present so that a reference produced here is
# never mistaken for one a real processor issued, in a database, a log or a bug report.
REFERENCE_PREFIX = "fake"


class FakeOutcome(StrEnum):
    """What the fake should do with one payment operation.

    `AMBIGUOUS` and `LOST_RESPONSE` are both UNKNOWN to the caller and are completely different
    facts. The first records nothing, so a later query finds nothing and the payment is still
    undecided. The second records a success and loses the answer, so a later query finds the
    charge and the payment has to be honoured however long that takes.
    """

    SUCCESS = "SUCCESS"
    DECLINE = "DECLINE"
    AMBIGUOUS = "AMBIGUOUS"
    LOST_RESPONSE = "LOST_RESPONSE"


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """What the fake believes it did with one identity.

    The provider side of the world, kept apart from this application's `PaymentAttempt` on
    purpose. The two disagreeing is the interesting state, and a fake that shared a record
    with the system under test could never produce it.
    """

    outcome: ProviderOutcome
    reference: str | None = None
    failure_code: str | None = None


@dataclass
class FakePaymentProvider:
    """A deterministic provider, configured before use and inspected afterwards.

    Not frozen, because a ledger is the point. Configuration is set at construction or through
    `set_outcome`, and neither reads a clock or a random number.

    It survives a restart of the application, because it represents the world outside it. A
    test that simulates a crash builds a new session and a new service and keeps this object,
    which is exactly what a real processor would do: our process restarting does not make a
    payment processor forget a charge.
    """

    default: FakeOutcome = FakeOutcome.SUCCESS
    outcomes: dict[str, FakeOutcome] = field(default_factory=dict)
    ledger: dict[str, LedgerEntry] = field(default_factory=dict)
    executions: list[PaymentInstruction] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    decline_code: str = DEFAULT_DECLINE_CODE

    def set_outcome(self, idempotency_key: str, outcome: FakeOutcome) -> None:
        """Decide what happens to one identity, before anything asks.

        Per key rather than per call, so that a test can set up two payments that behave
        differently and then let the code under test decide the order they happen in.
        """
        self.outcomes[idempotency_key] = outcome

    async def execute(self, instruction: PaymentInstruction) -> ProviderResult:
        """Perform the configured operation, once per identity.

        Every call is recorded in `executions`, including a repeat, because "the provider was
        asked twice and only charged once" and "the provider was asked once" are different
        facts and a test asserting idempotency needs to tell them apart.

        A repeat under a known identity returns the recorded result and creates no second
        charge, which is what a real idempotent provider does.

        A repeat under an identity that recorded nothing, which is the AMBIGUOUS case, is
        performed again. That is honest rather than convenient: the provider genuinely has no
        record of the first attempt, so it has nothing to return. This application never
        issues that second call, and the fake does not pretend the case away.
        """
        self.executions.append(instruction)

        recorded = self.ledger.get(instruction.idempotency_key)
        if recorded is not None:
            return ProviderResult(
                outcome=recorded.outcome,
                reference=recorded.reference,
                failure_code=recorded.failure_code,
            )

        outcome = self.outcomes.get(instruction.idempotency_key, self.default)
        reference = f"{REFERENCE_PREFIX}_{instruction.idempotency_key}"

        if outcome is FakeOutcome.SUCCESS:
            self.ledger[instruction.idempotency_key] = LedgerEntry(
                outcome=ProviderOutcome.SUCCEEDED, reference=reference
            )
            return ProviderResult(outcome=ProviderOutcome.SUCCEEDED, reference=reference)

        if outcome is FakeOutcome.DECLINE:
            self.ledger[instruction.idempotency_key] = LedgerEntry(
                outcome=ProviderOutcome.FAILED, failure_code=self.decline_code
            )
            return ProviderResult(outcome=ProviderOutcome.FAILED, failure_code=self.decline_code)

        if outcome is FakeOutcome.LOST_RESPONSE:
            # The charge went through and the answer did not come back. The ledger records
            # the success, the caller is told nothing, and only a query can close the gap.
            self.ledger[instruction.idempotency_key] = LedgerEntry(
                outcome=ProviderOutcome.SUCCEEDED, reference=reference
            )
            return ProviderResult(outcome=ProviderOutcome.UNKNOWN)

        # AMBIGUOUS: nothing reached the provider, or nothing it kept. No ledger entry, so a
        # query finds nothing and the payment stays undecided.
        return ProviderResult(outcome=ProviderOutcome.UNKNOWN)

    async def query(self, idempotency_key: str) -> ProviderQueryResult:
        """Report what the ledger holds for one identity, and perform nothing.

        A query never charges. An identity the provider has never seen answers UNKNOWN with
        `known=False`, which says "no record" rather than "it failed", because those are
        different facts and only one of them is safe to release stock on.
        """
        self.queries.append(idempotency_key)

        recorded = self.ledger.get(idempotency_key)
        if recorded is None:
            return ProviderQueryResult(outcome=ProviderOutcome.UNKNOWN, known=False)
        return ProviderQueryResult(
            outcome=recorded.outcome,
            known=True,
            reference=recorded.reference,
            failure_code=recorded.failure_code,
        )

    @property
    def charges(self) -> int:
        """How many logical charges this provider believes it made.

        The ledger size rather than the call count. Two executes under one identity are one
        charge, and asserting that is the point of a provider having its own record.
        """
        return sum(
            1 for entry in self.ledger.values() if entry.outcome is ProviderOutcome.SUCCEEDED
        )

    def executions_for(self, idempotency_key: str) -> int:
        """How many times this identity was sent to the provider, repeats included."""
        return sum(
            1 for instruction in self.executions if instruction.idempotency_key == idempotency_key
        )
