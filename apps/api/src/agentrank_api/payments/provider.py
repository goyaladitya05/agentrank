"""The boundary between this application and whatever moves the money.

Two operations, and deliberately not a third. `execute` asks a provider to perform one payment
operation. `query` asks what it did with one. There is no refund, no capture, no void, no
settlement and no webhook, because nothing in this phase needs any of them and an interface
method with no caller is a guess about a vendor's API rather than a boundary.

The instruction is the important half of this module. A provider receives a
`PaymentInstruction`, which is a frozen record of values read off a committed
`PaymentAttempt`. It never receives a `CheckoutSession`, a `SpendingMandate`, an
`InventoryReservation` or a database session. That is not tidiness: a provider that could
navigate to a live checkout could read a number the attempt was supposed to have frozen, and
the whole guarantee of this phase is that what was authorized and what was charged are the
same value because they came from the same row.

The identity a provider is idempotent on is `operation_reference`, and it is deliberately not
the caller's idempotency key. Inside this application a key is scoped by the checkout it was
presented against, so two merchants may choose the same string and mean two payments. A
provider account has no such scoping, so what travels outward is derived from the merchant and
the attempt instead. See `agentrank_api.payments.references`.

Three outcome classes and no fourth. Definitive success, definitive failure, and ambiguous.
The last one is the reason this interface exists in this shape:

```text
provider says yes            ->  SUCCEEDED   money moved
provider says no             ->  FAILED      no money moved
the answer never arrived     ->  UNKNOWN     nobody knows yet
```

A transport exception is the third case, never the second. Collapsing a timeout into a
decline is the single most expensive mistake available here, because it releases the stock and
invites a second payment for a charge that may already have gone through. An implementation of
this interface that raises on a network failure has not implemented it.

A query answers a second question beside the outcome, and the two are kept apart on purpose.
`ProviderRecord` says whether the provider has a record of the identity at all, and whether
that answer is final:

```text
PRESENT          there is a record, and `outcome` describes it
ABSENT           no record is visible right now, and one may still appear
NEVER_EXECUTED   the provider guarantees no operation for this identity exists,
                 and none can appear later from the original dispatch
```

The distance between the middle value and the last one is the whole reason this enumeration
exists. "I cannot find it" and "it never happened" are different facts, and only the second
one is safe to release stock on. Which of the two an implementation may report, and after how
long, is that implementation's decision and never this application's: a processor knows its
own visibility guarantee and nothing above the interface does. `PaymentQuery` carries the
instant the dispatch began so that a provider with such a guarantee can evaluate it, and no
duration appears anywhere outside a provider.

What this interface does not promise, and what nothing in this system promises, is exactly
once execution. PostgreSQL and an external payment processor cannot be committed atomically
together, and no amount of careful ordering changes that. What is promised is at most one
provider operation per idempotency identity, plus reconciliation for the results that come
back ambiguous. See docs/security.md.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol


class ProviderOutcome(StrEnum):
    """What a provider says happened, in three classes.

    Deliberately three, and deliberately not a vendor's status vocabulary. A processor may
    report a dozen decline codes and half a dozen pending states; what this application has to
    decide is whether to consume the stock, give it back, or wait and ask again, and there are
    exactly three answers to that.

    SUCCEEDED
        Definitive. The money moved. Terminal, and the reservation is consumed.

    FAILED
        Definitive, and specifically definitive that no money moved. The stock goes back.

    UNKNOWN
        Nothing definitive is known. A timeout, a reset connection, a response that was sent
        and never arrived. It is not FAILED and must never be treated as one.
    """

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class PaymentInstruction:
    """Everything a provider is told, and nothing that could change under it.

    Every value is read off a committed `PaymentAttempt`. Frozen because a provider is handed
    this and a provider is not this application's code: an object it could mutate is an object
    that could disagree with the row the money was authorized against.

    `operation_reference` is the identity, and it is the one field a provider must key its own
    idempotency on. It is derived from the merchant and the attempt, so it is globally unique
    inside one provider account however many merchants share it, and no caller can state it.

    `idempotency_key` is the application's own name for the same operation, carried for
    correlation and for a provider's logs. It is caller chosen and it is scoped by a checkout
    rather than globally, so an implementation that used it as its idempotency identity would
    let one merchant's payment answer for another's. Nothing may key on it.

    `merchant_reference` and `checkout_reference` are strings rather than identifiers, because
    they are for the provider's records and its dashboard rather than for a join. Nothing this
    application does depends on what a provider stores under them.

    There is no mandate here, no reservation, no line detail and no buyer. A payment processor
    is told what to charge and against which identity, and everything else is this
    application's business.
    """

    attempt_id: uuid.UUID
    operation_reference: str
    idempotency_key: str
    amount_minor: int
    currency: str
    merchant_reference: str
    checkout_reference: str


@dataclass(frozen=True, slots=True)
class ProviderResult:
    """What one `execute` call produced.

    `reference` is the provider's own identifier for the operation, recorded on a success so
    that a charge can be found in the provider's records later. `failure_code` is its reason
    for a definitive decline, carried as a stable code rather than prose for the same reason
    every other code here is one.

    An UNKNOWN result carries neither, because a caller that did not receive an answer did not
    receive a reference either. Learning one is reconciliation's job.
    """

    outcome: ProviderOutcome
    reference: str | None = None
    failure_code: str | None = None


class ProviderRecord(StrEnum):
    """Whether a provider has a record of one identity, and whether that answer is final.

    Separate from the outcome on purpose, and the separation is the safety property. A
    provider that cannot find an identity has not told us the payment failed; it has told us
    it cannot find one, which is a different fact and usually a temporary one. Folding "not
    found" into FAILED would turn a lookup that arrived a moment early into a released
    reservation and an invitation to pay twice.

    PRESENT
        The provider has a record. `outcome` describes it, and may itself still be UNKNOWN
        while the operation is undecided at the provider.

    ABSENT
        Nothing is visible right now, and something may still appear. Not a failure, and never
        to be treated as one. An attempt stays exactly where it is.

    NEVER_EXECUTED
        The provider guarantees that no operation exists for this identity and that none can
        appear later from the original dispatch. This is the one answer that lets an
        unresolved attempt terminate, because it is the one answer that says no money moved.
        Only an implementation that actually has such a guarantee may report it.
    """

    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    NEVER_EXECUTED = "NEVER_EXECUTED"


@dataclass(frozen=True, slots=True)
class PaymentQuery:
    """Everything a provider is told when it is asked what happened to one identity.

    Three values, all facts this application holds and a provider does not.

    `operation_reference` is the same derived identity `execute` was given, and it is what a
    provider looks its records up by. Asking under the caller's key instead would ask about
    whatever payment happened to share that string on the account.

    `idempotency_key` rides along for correlation and for nothing else, exactly as it does on
    an instruction.

    `dispatched_at` is the instant the dispatch began, committed before the network call, and
    it is here for one reason: a provider whose visibility guarantee is a duration cannot
    evaluate it without knowing when the clock started. Nothing above this interface knows what
    that duration is, and no code outside a provider implementation may contain one.

    There is no instant for now. A provider reads its own clock, exactly as it reads its own
    records, and a fake reads an injected one so a test never sleeps.
    """

    operation_reference: str
    idempotency_key: str
    dispatched_at: datetime


@dataclass(frozen=True, slots=True)
class ProviderQueryResult:
    """What a provider says about an identity it may or may not have seen.

    `record` is separate from `outcome` because they answer different questions: whether there
    is anything to report, and what it says. A query that finds nothing reports UNKNOWN with
    ABSENT or NEVER_EXECUTED, and only the second of those is a fact this application may act
    on.

    An outcome is refused for anything other than PRESENT. A provider claiming it definitively
    succeeded and that it has no record of the operation is stating two incompatible things,
    and the cheapest place to catch that is the moment it says it.
    """

    outcome: ProviderOutcome
    record: ProviderRecord = ProviderRecord.PRESENT
    reference: str | None = None
    failure_code: str | None = None

    def __post_init__(self) -> None:
        if (
            self.record is not ProviderRecord.PRESENT
            and self.outcome is not ProviderOutcome.UNKNOWN
        ):
            raise ValueError(
                f"a {self.record.value} query result cannot also report {self.outcome.value}"
            )


class PaymentProvider(Protocol):
    """The narrow contract every payment provider implements.

    A `Protocol` rather than a base class. Nothing here has behavior to inherit, and structural
    typing means a provider is anything with these two methods rather than anything that
    remembered to subclass.

    Both operations are asked outside any database transaction, always. A transaction held open
    across a network call holds its locks for as long as the network takes, which for a payment
    processor is unbounded.
    """

    async def execute(self, instruction: PaymentInstruction) -> ProviderResult:
        """Perform one payment operation, or report that the answer is unknown.

        Idempotent on `instruction.operation_reference`, and on nothing else. Two calls
        carrying one reference are one logical charge, and the second returns the first one's
        result rather than creating a second. This application makes that unnecessary by never
        issuing the second call, and the contract requires it anyway, because the case that
        matters is the one where the first call's response never arrived and nobody knows
        whether it happened.

        Keying on `instruction.idempotency_key` instead is a defect rather than a shortcut. It
        is a caller chosen string scoped by one checkout, so on an account serving several
        merchants two unrelated payments can present the same one.

        Never raises for a transport failure. A timeout, a reset connection and a response
        that never arrived are all UNKNOWN results, not exceptions and not declines.
        """
        ...

    async def query(self, query: PaymentQuery) -> ProviderQueryResult:
        """Ask what happened to one identity.

        The other half of the answer to an ambiguous execute. It reports what the provider
        knows now, including that it knows nothing, and it never performs a payment: a query
        for an identity a provider has never seen must not create one.

        An implementation reports NEVER_EXECUTED only if it can honestly guarantee it. The
        guarantee is the implementation's own and is usually a visibility window measured from
        `query.dispatched_at`: after it, anything the provider was ever going to record has
        been recorded, so a still empty answer means the operation never happened. A provider
        that offers no such guarantee reports ABSENT forever, and the attempt is resolved by
        `agentrank_api.payments.recovery` instead. Reporting NEVER_EXECUTED without the
        guarantee behind it releases stock under a charge that may have gone through, which is
        the one mistake this whole boundary is shaped to prevent.
        """
        ...
