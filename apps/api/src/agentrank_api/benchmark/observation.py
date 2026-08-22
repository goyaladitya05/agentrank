"""What trusted orchestration established about one attempt at one mission.

The evaluator's input, and deliberately not the executor's output. An executor produces an
`ExecutorReport`, which names identifiers and actions; `agentrank_api.benchmark.substantiation`
turns that into one of these by reading the merchant's own rows, and the evaluator marks this.

```text
ExecutorReport      what the thing under test says it did       untrusted
ObservedResult      what trusted code established happened      what the evaluator marks
```

Where every field comes from, because the whole point of the type is that this is answerable:

```text
selection           the variant the merchant's own quote references, or the one reported when
                    there is no quote, described by the pre-mission catalog
checkout            the merchant's own quote row: its total, its currency, its existence
authorization       what the merchant's authorization layer answered, recorded at the trusted
                    tool boundary from the server's own response
payment             the PaymentAttempt rows this merchant produced during the mission
abstention, error   the executor's own account, carried as a diagnostic and never as a fact
```

It is a description, not a claim of correctness. Nothing here says whether the mission succeeded,
and there is deliberately no field that could: that is the evaluator's answer.

Nothing here says whose fault an interruption was either. Attribution comes from
`agentrank_api.benchmark.faults`, decided at the tool boundary from what actually happened there,
and reaches the evaluator as a separate input.

Each part validates its own shape and the parts do not validate each other. A payment with no
selection behind it, or an abstention alongside a purchase, is a contradiction that the evaluator
classifies as `AGENT_REASONING_ERROR`. Refusing to construct one would mean a broken executor
raised inside the harness instead of being measured, and the measurement is the point.

Within one part, a half told story is refused, and the reason survives the move to substantiated
facts. A quote reported as created with no total let the budget check fall back to the cheaper
line amount. A payment reported as succeeded with no attempt identifier was the most consequential
fact in the benchmark taken entirely on somebody's word. Both are refused at construction: an
incomplete part is not a smaller description, it is one that reads better than the truth.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from agentrank_api.benchmark.report import (
    CheckoutRefusal,
    ReportedAbstention,
    ReportedError,
)
from agentrank_api.money import validate_amount_minor, validate_currency
from agentrank_api.payments.models import PaymentAttemptStatus


@dataclass(frozen=True, slots=True)
class ObservedSelection:
    """What was actually bought, as commerce facts rather than as a row reference.

    The category, the attributes and the unit price are carried rather than looked up later, for
    the same reason a checkout line snapshots them: a merchant editing a variant after the fact
    must not change what a historical measurement was made against. It also keeps the evaluator
    pure, which it could not be if deciding whether a charger was black meant reading the catalog.

    They are read from the merchant's pre-mission catalog by trusted code and never from the
    executor. An executor selects; it does not define what the thing it selected is.

    `substantiated` is false when the merchant's pre-mission catalog held no such variant, which
    is what a hallucinated identifier looks like. The price is then zero and the attributes empty,
    because nothing established either, and the same catalog facts that could not describe it also
    tell the evaluator it is not something this merchant sells.
    """

    variant_id: uuid.UUID
    quantity: int
    unit_price_amount_minor: int
    currency: str
    product_category: str | None = None
    variant_attributes: Mapping[str, Any] = field(default_factory=dict)
    substantiated: bool = True

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(f"a selected quantity must be positive, got {self.quantity}")
        validate_amount_minor(self.unit_price_amount_minor)
        validate_currency(self.currency)

    @property
    def line_amount_minor(self) -> int:
        """What this selection costs before any quote is produced."""
        return self.quantity * self.unit_price_amount_minor


@dataclass(frozen=True, slots=True)
class ObservedCheckout:
    """The merchant's quote, as the merchant's own row records it.

    `created` says the merchant produced a quote this buyer could act on. It is false when there
    is no row at all, and false when trusted evidence says the merchant authorized the purchase
    and could not hold the stock, because an offer nothing can be bought against is not an offer.

    The total and the currency are not optional when a quote was created, and that is load
    bearing rather than tidy. The budget is checked against the quoted total when there is one,
    so a missing total would be checked against the cheaper line amount instead and an over
    budget purchase would come back a success. They are read from the checkout row, so an
    executor claiming a cheaper quote than the one it paid names a row that says otherwise.

    Nor is `refusal` optional when a quote was not created: without it every refusal collapses
    into one code and a report cannot separate "the merchant said no and why" from "the executor
    did not say".
    """

    created: bool
    checkout_id: uuid.UUID | None = None
    total_amount_minor: int | None = None
    currency: str | None = None
    refusal: CheckoutRefusal | None = None

    def __post_init__(self) -> None:
        if self.total_amount_minor is not None:
            validate_amount_minor(self.total_amount_minor)
        if self.currency is not None:
            validate_currency(self.currency)

        if self.created:
            if self.total_amount_minor is None or self.currency is None:
                raise ValueError("a created quote reports its total and its currency")
            if self.refusal is not None:
                raise ValueError("a created quote was not refused")
        elif self.refusal is None:
            raise ValueError("a quote that was not created reports why not")


@dataclass(frozen=True, slots=True)
class ObservedAuthorization:
    """What the merchant's authorization layer answered, as the server answered it.

    Recorded at the trusted tool boundary from the response the merchant's own API produced, and
    never from the executor's account of it. That separation is what keeps `ENFORCEMENT_BYPASSED`
    reachable: a system that denied a purchase and admitted the payment anyway is exactly the bug
    this benchmark exists to find, and deriving "allowed" from the existence of the payment would
    make it undetectable by construction.

    `violations` holds the codes that layer reported, verbatim, for diagnostics. The evaluator
    reads `allowed` and nothing else: it is measuring whether the purchase was permitted, not
    re-deriving the permission itself, and re-deriving it would make the benchmark and the system
    it measures the same code marking its own work.
    """

    allowed: bool
    violations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ObservedPayment:
    """Where the payment got to, according to the payment table.

    `PaymentAttemptStatus` rather than a benchmark specific enumeration, so there is one
    vocabulary for what a payment did. It carries the distinction that matters most here: a
    definitive decline and an unresolved payment are different facts, and the payment kernel is
    built on never calling one the other.

    Both fields are read from a `PaymentAttempt` row belonging to this merchant. "Money moved" is
    the most consequential fact in the benchmark and the one that decides both task completion
    and captured simulated demand, so it is established rather than reported: a claimed success
    with no row behind it is not a payment, and a real payment nobody mentioned is still found.
    """

    status: PaymentAttemptStatus
    attempt_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if self.status is PaymentAttemptStatus.SUCCEEDED and self.attempt_id is None:
            raise ValueError("a successful payment names the attempt it came from")


@dataclass(frozen=True, slots=True)
class ObservedResult:
    """Everything trusted code established about one mission, as the evaluator reads it.

    Every part is optional because a mission can stop anywhere, and where it stopped is one of
    the things being measured. The merchant is not optional: which merchant was transacted with
    is ground truth the evaluator checks rather than assumes.

    The abstention and the error are the executor's own words, carried here because a decision
    nobody acted on leaves no trace to substantiate and because a person reading a run wants to
    know what the executor believed. Neither is classified from.
    """

    merchant_id: uuid.UUID
    selection: ObservedSelection | None = None
    checkout: ObservedCheckout | None = None
    authorization: ObservedAuthorization | None = None
    payment: ObservedPayment | None = None
    abstention: ReportedAbstention | None = None
    error: ReportedError | None = None

    @property
    def purchased(self) -> bool:
        """Whether a payment definitively succeeded.

        The one place "a purchase happened" is decided, so that no other part of the benchmark
        has to remember which payment statuses are terminal successes.
        """
        return self.payment is not None and self.payment.status is PaymentAttemptStatus.SUCCEEDED
