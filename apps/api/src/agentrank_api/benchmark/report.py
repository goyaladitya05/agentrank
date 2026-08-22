"""What an executor says it did. Untrusted by construction, and small for that reason.

This is the executor's half of the benchmark's trust boundary and it is deliberately not the
evaluator's input. An executor names what it acted on and what it decided; trusted orchestration
establishes what those actions came to, in `agentrank_api.benchmark.substantiation`, and the
evaluator marks the result of that.

```text
the executor may say      which variant it selected and how many
                          which quote it created, or that the merchant refused one
                          which payment it dispatched
                          that it declined to buy, and why it says so
                          what stopped it, in its own words

it cannot say             what that variant's price, category or attributes are
                          what the quote came to
                          whether authorization allowed the purchase
                          whether the payment succeeded
                          whose fault an interruption was
                          what the mission was worth or whether it passed
```

The right hand column is not enforced by a rule or a review. There is no field here to write any
of it in, which is the same mechanism that closed `ErrorOrigin` in Phase 2B-R: the executor did
not stop being believed about the origin of a fault, it stopped having anywhere to state one.

Every item on the left is an identifier or an action, and every one of them is checked against
trusted state before it means anything. A variant identifier is looked up in the merchant's
pre-mission catalog. A quote identifier is resolved to the merchant's own row. A payment
identifier is resolved to a `PaymentAttempt`, and a payment the executor never mentioned is found
anyway. So a lie is not believed, and it is also not useful: naming a cheaper quote than the one
that was paid names a row that says what it says.

The two things that do pass through are the abstention and the error detail, and both are
diagnostics the evaluator never classifies from. An abstention decides the ABSTAINED status
because declining is a decision only the buyer can make and there is no trusted record of a
decision nobody acted on. Whether it was the right decision is decided from the catalog, and an
abstention beside a payment that actually happened is a contradiction the evaluator marks as one.
"""

import uuid
from dataclasses import dataclass
from enum import StrEnum


class AbstentionCode(StrEnum):
    """Why an executor says it declined to buy.

    Diagnostic only. The evaluator never classifies a mission from this: an incorrect
    abstention is a `DISCOVERY_FAILURE` whichever of these the executor claimed, because
    marking a mission from an executor's account of its own reasoning would be trusting the
    thing under test. It is recorded so that a human reading a run can see what the executor
    believed, and for nothing else.
    """

    NO_CANDIDATE_FOUND = "NO_CANDIDATE_FOUND"
    NO_COMPLIANT_CANDIDATE = "NO_COMPLIANT_CANDIDATE"
    MERCHANT_DATA_INSUFFICIENT = "MERCHANT_DATA_INSUFFICIENT"
    BUDGET_INSUFFICIENT = "BUDGET_INSUFFICIENT"


class CheckoutRefusal(StrEnum):
    """Why the merchant would not produce a usable quote, as the executor understood it.

    Three, and each maps to a different failure reason, because the repairs are different.

    OUT_OF_STOCK
        The merchant sells it and could not hold enough of it.

    VARIANT_UNAVAILABLE
        The merchant does not sell it, or no longer does. A different finding from having run
        out, and folding the two together would hide a merchant whose catalog offers things it
        does not sell.

    MERCHANT_REFUSED
        Anything else the merchant refused a quote for.

    Reported rather than substantiated, and the safety consequence of that is closed elsewhere
    rather than trusted here. Claiming OUT_OF_STOCK for a variant the merchant does not sell
    would move the finding out of the unauthorized set, and it does not, because
    `INVALID_VARIANT` is also derived from the pre-mission catalog by trusted code. What is left
    is which repair a merchant is handed for a refusal nothing else witnessed, and where trusted
    evidence exists it wins: a preparation that authorized and could not hold stock is recorded
    at the tool boundary.
    """

    OUT_OF_STOCK = "OUT_OF_STOCK"
    VARIANT_UNAVAILABLE = "VARIANT_UNAVAILABLE"
    MERCHANT_REFUSED = "MERCHANT_REFUSED"


@dataclass(frozen=True, slots=True)
class ReportedSelection:
    """What the executor says it chose to buy.

    An identifier and a count, and nothing describing them. What that variant costs, what
    category it is in and what its attributes are come from the merchant's own pre-mission
    catalog, because those are the facts a mission is decided against and an executor that could
    state them would be answering the question it is being asked.

    The quantity is here rather than derived because a selection with no quote behind it has no
    other record: an executor that identified something and never quoted it still chose a number
    of units, and comparing that against what the mission asked for is a finding.
    """

    variant_id: uuid.UUID
    quantity: int

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(f"a selected quantity must be positive, got {self.quantity}")


@dataclass(frozen=True, slots=True)
class ReportedCheckout:
    """What the executor says happened when it asked the merchant to quote.

    `checkout_id` names a row, which trusted code then reads for the total and the currency. It
    is optional because a merchant that refused to quote produced no row to name, and a benchmark
    that could only record real rows could not measure the merchants that refuse to make them.

    `refusal` is the executor's account of why there is no usable quote. It is diagnostic, and it
    is only read when nothing trusted contradicts it.
    """

    checkout_id: uuid.UUID | None = None
    refusal: CheckoutRefusal | None = None

    def __post_init__(self) -> None:
        if self.checkout_id is None and self.refusal is None:
            raise ValueError("a checkout report names a quote or says why there is none")


@dataclass(frozen=True, slots=True)
class ReportedPayment:
    """The payment the executor says it dispatched.

    An identifier and nothing else. There is no status here and there is deliberately nowhere to
    put one: whether money moved is read from the `PaymentAttempt` this names, and from every
    attempt this merchant produced during the mission, so a payment the executor does not mention
    is found and a payment it invents is not.
    """

    attempt_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class ReportedAbstention:
    """The executor decided not to buy anything."""

    code: AbstentionCode
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ReportedError:
    """The executor's own account of what stopped it.

    Diagnostic only, exactly like `AbstentionCode`, and for the same reason: it is the thing
    under test describing its own situation. It carries no origin and there is no field here that
    could become one. Whether an interruption was the merchant's, the buyer's or the benchmark's
    own is decided from what happened at the tool boundary and reaches the evaluator as a
    separate trusted input.
    """

    detail: str

    def __post_init__(self) -> None:
        if not self.detail.strip():
            raise ValueError("a reported error must say what went wrong")


@dataclass(frozen=True, slots=True)
class ExecutorReport:
    """One executor's complete account of one mission.

    Every part is optional because a mission can stop anywhere, and where it stopped is one of
    the things being measured. The merchant is not optional: which merchant the executor believes
    it transacted with is a claim the evaluator checks rather than assumes.

    Nothing here says whether the mission succeeded and there is deliberately no field that
    could. That is the evaluator's answer, and an executor that could assert its own result would
    be marking its own work.
    """

    merchant_id: uuid.UUID
    selection: ReportedSelection | None = None
    checkout: ReportedCheckout | None = None
    payment: ReportedPayment | None = None
    abstention: ReportedAbstention | None = None
    error: ReportedError | None = None
