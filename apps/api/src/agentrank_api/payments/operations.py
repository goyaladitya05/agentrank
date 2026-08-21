"""Reading payments the way somebody recovering them has to read them.

The payment kernel is written for the request that is happening now. It admits, dispatches,
records and reconciles one identity at a time, and every one of those operations is named by
the caller that wants it. Nothing in it answers the question an operator actually arrives
with, which is not about one payment at all:

```text
which payments need attention
why does each one need attention
how long has each one been waiting
what does it belong to and how much money is involved
what does the hold and the quote say right now
```

This module is the read side of that. It queries, it converts, and it changes nothing. There
is no write here at all, deliberately: the operations that move a payment already exist in
`agentrank_api.payments.service` and `agentrank_api.payments.recovery`, and a second module
that could also terminalize one would be a second answer to a question that must have one.

Three reads and one classifier:

```text
list_unresolved   the bounded, ordered work list
show              one payment, joined, with a bounded slice of its trail
counts            how many attempts are in each state
classify          what one reconciliation actually did, in operator words
```

The classifier exists because `PaymentOutcome` is written for correctness rather than for a
terminal. It carries whether anything changed, whether a provider was asked and whether two
definitive answers disagreed, and an operator has to turn those into one of a handful of
sentences: it resolved, it is still stuck, the provider has no record yet, the provider
guaranteed it never happened. Doing that in a command would put the meaning of a payment
outcome inside a print statement.

Nothing here reads the audit log to decide anything. `show` can include recent events and
they are informational, exactly as they are everywhere else in this system: authoritative
payment state is `PaymentAttempt` and its locked transitions, and event order is transaction
start order rather than commit order. See docs/decisions.md.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.audit.models import ActorType
from agentrank_api.audit.repository import AuditRepository
from agentrank_api.errors import NotFoundError
from agentrank_api.payments.admission import PAYMENT_RESOURCE
from agentrank_api.payments.execution import PROVIDER_NEVER_EXECUTED, PaymentOutcome
from agentrank_api.payments.models import OPEN_STATUSES, PaymentAttemptStatus
from agentrank_api.payments.provider import ProviderRecord
from agentrank_api.payments.repository import (
    DEFAULT_UNRESOLVED_LIMIT,
    PaymentAttemptRepository,
    PaymentOperationRow,
    bounded_unresolved_limit,
)

# The states an operator has work to do about. This is `OPEN_STATUSES` under an operational
# name and not a second definition of it: ADMITTED, IN_FLIGHT and UNKNOWN are exactly the
# states in which an identity may still reach a provider or is waiting on one, and a separate
# tuple here would be a copy that could drift away from the one the mandate scoped uniqueness
# is built on.
UNRESOLVED_STATUSES: tuple[PaymentAttemptStatus, ...] = OPEN_STATUSES

# How much of a payment's trail `show` includes, and the most it will include. Bounded for
# the same reason every audit read in this repository is: the trail only grows.
DEFAULT_EVENT_LIMIT = 10
MAX_EVENT_LIMIT = 50


class PaymentOperationResult(StrEnum):
    """What one reconciliation did, said in the words an operator has to act on.

    Derived from a `PaymentOutcome` rather than stored anywhere. The outcome is written for
    correctness and carries three orthogonal facts: whether this call changed the row, whether
    a provider was asked, and what the provider said about having a record at all. Those are
    the right fields to reason with and the wrong ones to print, because the next move differs
    between cases that share two of the three.

    RESOLVED_SUCCESS
        The provider reported a definitive success and this call recorded it. The stock was
        consumed and the checkout is paid. Nothing further is needed.

    RESOLVED_FAILURE
        The provider reported a definitive decline. The hold went back and the checkout is
        still open, so the buyer may pay again with a new identity.

    PROVIDER_NEVER_EXECUTED
        Stronger than a decline and the reason this value is separate from one. The provider
        guaranteed that no operation exists for this identity and that none can appear, so no
        money moved and the hold went back on that guarantee rather than on a judgement.

    PROVIDER_ABSENT
        The provider has no record right now and will not promise that it never will. Nothing
        changed and nothing should. This is the state that ends in an abandonment if it never
        moves, and the state where waiting is still the correct action.

    STILL_UNRESOLVED
        The provider has the operation and has not decided it. Different from absence: there
        is something to wait for.

    ALREADY_TERMINAL
        The payment was settled before this call, so no provider was asked. A sweep meeting a
        payment somebody else resolved has done its job.

    OUTCOME_CONFLICT
        This call observed something definitive that contradicts the terminal state already
        recorded. Nothing was rewritten in either direction and the disagreement is in the
        trail. It needs a person, and it is the one result here that does.

    SKIPPED_NOT_DISPATCHED
        Only a sweep produces this. The attempt is ADMITTED, which means the provider provably
        never heard of it, so there is nothing to reconcile and a query would learn nothing.
        What it needs is a dispatch, which is a money moving operation and therefore a
        different command. See `PaymentService.resume`.

    REFUSED
        Only a sweep produces this. The kernel refused this attempt for a reason the sweep
        recorded and carried on from, so that one payment cannot cost an operator the report
        on all the others.
    """

    RESOLVED_SUCCESS = "resolved_success"
    RESOLVED_FAILURE = "resolved_failure"
    PROVIDER_NEVER_EXECUTED = "provider_never_executed"
    PROVIDER_ABSENT = "provider_absent"
    STILL_UNRESOLVED = "still_unresolved"
    ALREADY_TERMINAL = "already_terminal"
    OUTCOME_CONFLICT = "outcome_conflict"
    SKIPPED_NOT_DISPATCHED = "skipped_not_dispatched"
    REFUSED = "refused"


def classify(outcome: PaymentOutcome) -> PaymentOperationResult:
    """Turn one reconciliation's result into the sentence an operator needs.

    Order matters and each branch is a decision.

    A conflict first, because it is the only result that is about two answers rather than one
    and it must never be reported as whichever of them happens to stand.

    Then "no provider was asked", which for a reconciliation means exactly one thing: the
    attempt was already terminal and there was nothing to learn.

    Then the two definitive resolutions, read off the authoritative row rather than off what
    the provider said. A failure carrying `PROVIDER_NEVER_EXECUTED` is reported as the
    guarantee it is rather than as a decline, because the two are different facts and only one
    of them is a refusal.

    Everything left is a payment that is still unresolved, and the provider's record is what
    separates "there is nothing to wait for" from "there is". That distinction is why
    `provider_record` is carried on the outcome at all.
    """
    if outcome.conflict is not None:
        return PaymentOperationResult.OUTCOME_CONFLICT
    if not outcome.provider_called:
        return PaymentOperationResult.ALREADY_TERMINAL
    if outcome.attempt.status is PaymentAttemptStatus.SUCCEEDED:
        return PaymentOperationResult.RESOLVED_SUCCESS
    if outcome.attempt.status is PaymentAttemptStatus.FAILED:
        if outcome.attempt.failure_code == PROVIDER_NEVER_EXECUTED:
            return PaymentOperationResult.PROVIDER_NEVER_EXECUTED
        return PaymentOperationResult.RESOLVED_FAILURE
    if outcome.provider_record is ProviderRecord.ABSENT:
        return PaymentOperationResult.PROVIDER_ABSENT
    return PaymentOperationResult.STILL_UNRESOLVED


@dataclass(frozen=True, slots=True)
class PaymentAuditEntry:
    """One recorded event about a payment, for reading and for nothing else.

    Informational, and the docstring says so because the temptation is real. Event order here
    is the order transactions started in, which is not the order they committed in, so nothing
    may be inferred from two of these being adjacent. What a payment is, is its attempt row.
    """

    occurred_at: datetime
    event_type: str
    actor_type: ActorType
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class UnresolvedPayments:
    """The bounded work list, and the two facts that make it readable.

    `observed_at` is the database clock at the instant the listing was taken, so every age in
    it is measured against one reading rather than against an operator's laptop.

    `limit` is the bound that was actually applied rather than the one that was asked for, and
    `truncated` is what an operator needs in order to tell a short list from a cut off one. A
    listing that is exactly as long as its limit may well have more behind it, and reporting
    that is cheaper than counting the whole table to find out.
    """

    observed_at: datetime
    limit: int
    payments: tuple[PaymentOperationRow, ...]

    @property
    def truncated(self) -> bool:
        """Whether the bound may have hidden something."""
        return len(self.payments) >= self.limit


@dataclass(frozen=True, slots=True)
class PaymentOperationView:
    """One payment, joined, aged and with a bounded slice of its trail beside it."""

    payment: PaymentOperationRow
    observed_at: datetime
    events: tuple[PaymentAuditEntry, ...]


@dataclass(frozen=True, slots=True)
class PaymentStatusCounts:
    """How many attempts are in each state.

    The terminal counts are lifetime totals rather than a recent window, and that is stated
    here as well as in the repository because a number labelled "failed" invites being read as
    "failed lately". Every status is present, including the ones with no rows, so a caller
    renders a stable set of lines rather than a set that changes shape with the data.
    """

    observed_at: datetime
    counts: Mapping[PaymentAttemptStatus, int]

    @property
    def unresolved(self) -> int:
        """How many payments an operator currently has work to do about."""
        return sum(self.counts[status] for status in UNRESOLVED_STATUSES)


class PaymentOperationsService:
    """The read side of payment operations. It answers questions and writes nothing."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._attempts = PaymentAttemptRepository(session)
        self._audit = AuditRepository(session)

    async def list_unresolved(self, *, limit: int = DEFAULT_UNRESOLVED_LIMIT) -> UnresolvedPayments:
        """The payments that need attention, oldest first, never more than the bound.

        The bound is applied rather than trusted: an operator asking for a hundred thousand
        gets the maximum this repository serves, and the listing reports which bound it used
        so nobody reads a truncated answer as a complete one.

        The clock is read first and in the same transaction as the rows, so `now()` is the
        instant that transaction began and every age in the result is measured from it.
        """
        applied = bounded_unresolved_limit(limit)
        observed_at = await self._attempts.clock()
        payments = await self._attempts.list_unresolved(limit=applied)
        # A read closes its own transaction. Nothing was written and holding a snapshot open
        # for as long as a terminal takes to print is a cost with no purpose.
        await self._session.commit()
        return UnresolvedPayments(observed_at=observed_at, limit=applied, payments=tuple(payments))

    async def show(
        self, attempt_id: uuid.UUID, *, events: int = DEFAULT_EVENT_LIMIT
    ) -> PaymentOperationView:
        """One payment as an operator needs to see it, whatever state it is in.

        Raises rather than returning None. A caller naming an attempt has already decided it
        should exist, which is the same choice `PaymentService.get_attempt` makes.

        The events are a bounded tail of the trail and are explicitly not where the payment's
        state comes from. Every field above them is read from the attempt row and its joins.
        """
        observed_at = await self._attempts.clock()
        payment = await self._attempts.get_operational(attempt_id)
        if payment is None:
            await self._session.rollback()
            raise NotFoundError(PAYMENT_RESOURCE, str(attempt_id))

        recorded = await self._audit.list_for_resource(
            resource_type=PAYMENT_RESOURCE,
            resource_id=attempt_id,
            limit=min(max(events, 1), MAX_EVENT_LIMIT),
        )
        await self._session.commit()
        return PaymentOperationView(
            payment=payment,
            observed_at=observed_at,
            events=tuple(
                PaymentAuditEntry(
                    occurred_at=event.occurred_at,
                    event_type=event.event_type,
                    actor_type=event.actor_type,
                    payload=dict(event.payload),
                )
                for event in recorded
            ),
        )

    async def counts(self) -> PaymentStatusCounts:
        """How many attempts are in each state, with every state present."""
        observed_at = await self._attempts.clock()
        counted = await self._attempts.count_by_status()
        await self._session.commit()
        return PaymentStatusCounts(
            observed_at=observed_at,
            counts={status: counted.get(status, 0) for status in PaymentAttemptStatus},
        )


def unresolved_ids(payments: Sequence[PaymentOperationRow]) -> tuple[uuid.UUID, ...]:
    """The identifiers out of a listing, in the listing's own order.

    A sweep works from identifiers rather than from the records it listed, because the records
    are a snapshot and the reconciliation that follows re-reads and locks each row for itself.
    Carrying the snapshot into the decision would be deciding from state that may already have
    moved.
    """
    return tuple(payment.attempt_id for payment in payments)
