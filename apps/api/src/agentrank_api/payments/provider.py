"""The boundary between this application and whatever moves the money.

Two operations, and deliberately not a third. `execute` asks a provider to perform one payment
operation. `query` asks what it did with one. There is no refund, no capture, no void, no
settlement and no webhook, because nothing in this phase needs any of them and an interface
method with no caller is a guess about a vendor's API rather than a boundary.

The instruction is the important half of this module. A provider receives a
`PaymentInstruction`, which is a frozen record of five values read off a committed
`PaymentAttempt`. It never receives a `CheckoutSession`, a `SpendingMandate`, an
`InventoryReservation` or a database session. That is not tidiness: a provider that could
navigate to a live checkout could read a number the attempt was supposed to have frozen, and
the whole guarantee of this phase is that what was authorized and what was charged are the
same value because they came from the same row.

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

What this interface does not promise, and what nothing in this system promises, is exactly
once execution. PostgreSQL and an external payment processor cannot be committed atomically
together, and no amount of careful ordering changes that. What is promised is at most one
provider operation per idempotency identity, plus reconciliation for the results that come
back ambiguous. See docs/security.md.
"""

import uuid
from dataclasses import dataclass
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

    Five values, all read off a committed `PaymentAttempt`. Frozen because a provider is
    handed this and a provider is not this application's code: an object it could mutate is an
    object that could disagree with the row the money was authorized against.

    `merchant_reference` and `checkout_reference` are strings rather than identifiers, because
    they are for the provider's records and its dashboard rather than for a join. Nothing this
    application does depends on what a provider stores under them.

    There is no mandate here, no reservation, no line detail and no buyer. A payment processor
    is told what to charge and against which identity, and everything else is this
    application's business.
    """

    attempt_id: uuid.UUID
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


@dataclass(frozen=True, slots=True)
class ProviderQueryResult:
    """What a provider says about an identity it may or may not have seen.

    `known` is separate from the outcome on purpose. A provider that has no record of an
    identity has not told us the payment failed; it has told us it cannot find one, which is a
    different fact and often a temporary one. Folding "not found" into FAILED would turn a
    lookup that arrived a moment early into a released reservation and an invitation to pay
    twice, so a query that finds nothing reports UNKNOWN and `known=False`, and the attempt
    stays where it is.
    """

    outcome: ProviderOutcome
    known: bool = True
    reference: str | None = None
    failure_code: str | None = None


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

        Idempotent on `instruction.idempotency_key`. Two calls carrying one key are one
        logical charge, and the second returns the first one's result rather than creating a
        second. This application makes that unnecessary by never issuing the second call, and
        the contract requires it anyway, because the case that matters is the one where the
        first call's response never arrived and nobody knows whether it happened.

        Never raises for a transport failure. A timeout, a reset connection and a response
        that never arrived are all UNKNOWN results, not exceptions and not declines.
        """
        ...

    async def query(self, idempotency_key: str) -> ProviderQueryResult:
        """Ask what happened to one identity.

        The other half of the answer to an ambiguous execute. It reports what the provider
        knows now, including that it knows nothing, and it never performs a payment: a query
        for an identity a provider has never seen must not create one.
        """
        ...
