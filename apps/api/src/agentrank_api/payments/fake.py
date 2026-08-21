"""A payment provider that does exactly what it is told, and remembers what it was asked.

This is the only provider that exists. Phase 1F is deliberately provider independent: the
execution kernel has to be correct before a real processor is involved, and a real processor
cannot be made to time out on demand, decline on demand, or lose a response on demand, which
are the three things worth testing hardest.

Nothing here is random. There is no `random`, no clock reading and no probability. An outcome
is either configured for a specific idempotency key or it is the default, and both are set by
whoever constructed the fake. A provider that failed one time in ten would make every test
that touched it flaky and would prove nothing about the case it did not happen to produce.

It behaves like an idempotent provider rather than like a stub. Its ledger is keyed by the
operation reference, which is the merchant namespaced identity the provider contract says a
provider must key on, so a second execute under one reference returns the first one's result
and creates no second charge. That is what lets a test validate provider idempotency and
application idempotency separately rather than hoping one covers the other, and it is what
makes two merchants presenting one caller key two charges here rather than one.

Configured outcomes are keyed by the caller's idempotency key instead, and the asymmetry is
deliberate rather than an oversight. The ledger models the provider's own namespace, which is
the thing under test. The outcome map is a test knob, decided before an attempt exists and
therefore before its derived reference does, so keying it on the string a test already chose
is the only thing it could be keyed on.

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

Time is a field rather than a reading. `clock` is the instant this provider believes it is,
and a test advances it by assigning to it. Two durations are measured against it and both are
off by default:

```text
visibility_window       after this long since the dispatch began, an identity with no
                        record is reported NEVER_EXECUTED rather than ABSENT
idempotency_retention   after this long, the provider stops honouring an identity it
                        recorded, and executing under it again performs a second operation
```

Both default to None, which means "this provider makes no such guarantee": absence is never
final and an identity is remembered forever. That is the conservative reading in both cases,
and it is what lets every test that does not care about timing ignore all of this.

The retention knob exists to prove something about this application rather than about the
fake. Provider idempotency is a second line of defence and was never the first one: a
duplicate logical payment is stopped by a committed `PaymentAttempt` and by the mandate scoped
uniqueness above it, both of which outlive any provider's memory. A test that makes the
provider forget and then shows nothing is executed twice is a test that this application does
not quietly depend on that memory.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from agentrank_api.payments.provider import (
    PaymentInstruction,
    PaymentQuery,
    ProviderOutcome,
    ProviderQueryResult,
    ProviderRecord,
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

    `recorded_at` is the provider's clock reading at the moment the entry was written, and it
    is None when the fake had no clock set. It is what an idempotency retention window is
    measured from, and nothing else reads it.
    """

    outcome: ProviderOutcome
    reference: str | None = None
    failure_code: str | None = None
    recorded_at: datetime | None = None


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
    # What this provider believes the time is. Assigned by a test rather than read from a
    # real clock, so a window elapses because somebody said so and never because a suite ran
    # slowly. None means it has no reading, and every window below stays unelapsed.
    clock: datetime | None = None
    # How long after a dispatch began this provider guarantees that anything it was ever
    # going to record has become visible. None means it offers no such guarantee and reports
    # ABSENT forever, which is the honest answer for a processor that cannot promise one.
    visibility_window: timedelta | None = None
    # How long this provider honours an identity it has recorded. None means forever.
    idempotency_retention: timedelta | None = None
    # How many logical operations this provider believes it performed. A counter rather than
    # a ledger size, because an identity that expired out of the idempotency window and was
    # executed again is genuinely two operations and the ledger holds one entry per key.
    charges: int = 0

    def set_outcome(self, idempotency_key: str, outcome: FakeOutcome) -> None:
        """Decide what happens to one application identity, before anything asks.

        Per key rather than per call, so that a test can set up two payments that behave
        differently and then let the code under test decide the order they happen in.

        Keyed by the caller's idempotency key and not by the operation reference, because a
        test decides this before the attempt exists and the reference is derived from the
        attempt. Only the ledger below is keyed by the reference, and the ledger is the half
        that models a provider's namespace.
        """
        self.outcomes[idempotency_key] = outcome

    async def execute(self, instruction: PaymentInstruction) -> ProviderResult:
        """Perform the configured operation, once per identity.

        Every call is recorded in `executions`, including a repeat, because "the provider was
        asked twice and only charged once" and "the provider was asked once" are different
        facts and a test asserting idempotency needs to tell them apart.

        A repeat under an identity this provider still honours returns the recorded result and
        creates no second charge, which is what a real idempotent provider does.

        A repeat under an identity that recorded nothing, which is the AMBIGUOUS case, is
        performed again. That is honest rather than convenient: the provider genuinely has no
        record of the first attempt, so it has nothing to return. This application never
        issues that second call, and the fake does not pretend the case away.

        A repeat under an identity whose retention window has passed is performed again for
        the same reason, and the second operation is counted as a second charge. That is the
        case worth being blunt about: provider idempotency is not permanent anywhere, so it is
        not what stops this application from paying twice, and a fake that remembered forever
        would let that dependency hide.
        """
        self.executions.append(instruction)

        identity = instruction.operation_reference
        recorded = self._honoured(identity)
        if recorded is not None:
            return ProviderResult(
                outcome=recorded.outcome,
                reference=recorded.reference,
                failure_code=recorded.failure_code,
            )

        outcome = self.outcomes.get(instruction.idempotency_key, self.default)
        reference = f"{REFERENCE_PREFIX}_{identity}"

        if outcome is FakeOutcome.SUCCESS:
            self._record(identity, ProviderOutcome.SUCCEEDED, reference=reference)
            return ProviderResult(outcome=ProviderOutcome.SUCCEEDED, reference=reference)

        if outcome is FakeOutcome.DECLINE:
            self._record(identity, ProviderOutcome.FAILED, failure_code=self.decline_code)
            return ProviderResult(outcome=ProviderOutcome.FAILED, failure_code=self.decline_code)

        if outcome is FakeOutcome.LOST_RESPONSE:
            # The charge went through and the answer did not come back. The ledger records
            # the success, the caller is told nothing, and only a query can close the gap.
            self._record(identity, ProviderOutcome.SUCCEEDED, reference=reference)
            return ProviderResult(outcome=ProviderOutcome.UNKNOWN)

        # AMBIGUOUS: nothing reached the provider, or nothing it kept. No ledger entry, so a
        # query finds nothing and the payment stays undecided.
        return ProviderResult(outcome=ProviderOutcome.UNKNOWN)

    async def query(self, query: PaymentQuery) -> ProviderQueryResult:
        """Report what the ledger holds for one identity, and perform nothing.

        A query never charges. An identity the provider has no entry for answers UNKNOWN, and
        the interesting half of the answer is which kind of nothing it is.

        ABSENT while the visibility window has not passed, or while there is no window at all.
        That says "no record right now" rather than "it failed", and those are different facts
        with opposite consequences for a merchant's stock.

        NEVER_EXECUTED once `visibility_window` has elapsed since the dispatch began. Only then
        does this provider claim that nothing exists and nothing can appear, and it is a claim
        about this fake's own guarantee rather than a rule anything above it applies.

        Retention is not consulted here. A processor's idempotency window governs whether it
        will replay an operation, not whether it still holds the record of one, and collapsing
        the two would make this fake report that a payment it definitely made never happened.
        """
        self.queries.append(query.idempotency_key)

        recorded = self.ledger.get(query.operation_reference)
        if recorded is not None:
            return ProviderQueryResult(
                outcome=recorded.outcome,
                record=ProviderRecord.PRESENT,
                reference=recorded.reference,
                failure_code=recorded.failure_code,
            )

        return ProviderQueryResult(
            outcome=ProviderOutcome.UNKNOWN,
            record=ProviderRecord.NEVER_EXECUTED
            if self._elapsed(query.dispatched_at, self.visibility_window)
            else ProviderRecord.ABSENT,
        )

    def executions_for(self, idempotency_key: str) -> int:
        """How many times this application identity was sent to the provider, repeats included.

        By idempotency key, because that is what a test naming a payment has in hand. What the
        provider deduplicated on is the operation reference, and `charges` is where the
        difference between the two shows up.
        """
        return sum(
            1 for instruction in self.executions if instruction.idempotency_key == idempotency_key
        )

    def _record(
        self,
        operation_reference: str,
        outcome: ProviderOutcome,
        *,
        reference: str | None = None,
        failure_code: str | None = None,
    ) -> None:
        """Write what this provider did with one identity, and count it if money moved."""
        self.ledger[operation_reference] = LedgerEntry(
            outcome=outcome,
            reference=reference,
            failure_code=failure_code,
            recorded_at=self.clock,
        )
        if outcome is ProviderOutcome.SUCCEEDED:
            self.charges += 1

    def _honoured(self, operation_reference: str) -> LedgerEntry | None:
        """The entry this provider will still replay for an identity, if it will replay one."""
        entry = self.ledger.get(operation_reference)
        if entry is None or entry.recorded_at is None:
            return entry
        if self._elapsed(entry.recorded_at, self.idempotency_retention):
            return None
        return entry

    def _elapsed(self, since: datetime, window: timedelta | None) -> bool:
        """Whether a window measured from an instant has passed on this provider's clock.

        False whenever either half is missing. A provider with no clock reading and a provider
        with no window are both providers that have promised nothing, and the answer that
        promises nothing is the answer that changes nothing.
        """
        if window is None or self.clock is None:
            return False
        return self.clock - since >= window
