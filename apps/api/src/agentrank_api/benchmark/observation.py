"""What an executor reports about one attempt at one mission.

Provider independent and agent independent on purpose. There is no LLM in this module, no
prompt, no trace and no model identifier: an `ObservedResult` describes what happened in
commerce terms, so a scripted runner, a future LLM buyer and anything after that all produce
the same shape and are all marked by the same evaluator.

It is a report, not a claim of correctness. Nothing here says whether the mission succeeded,
and there is deliberately no field that could: that is the evaluator's answer, and an executor
that could assert its own result would be marking its own work.

Local shape is validated here and coherence between the parts is not. A result claiming a
payment with no selection behind it, or an abstention alongside a purchase, is a contradiction
that the evaluator classifies as `AGENT_REASONING_ERROR`. Refusing to construct one would mean
a broken executor raised inside the harness instead of being measured, and the measurement is
the point.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from agentrank_api.money import validate_amount_minor, validate_currency
from agentrank_api.payments.models import PaymentAttemptStatus


class ErrorOrigin(StrEnum):
    """Whose fault an error was, which decides whether it is a finding or a fault.

    MERCHANT
        A merchant surface returned an error rather than an answer or a business refusal. A
        commerce readiness finding, and the mission is marked FAILED with MERCHANT_API_ERROR.

    HARNESS
        The benchmark harness itself could not carry the mission out. Not a fact about the
        merchant, and the mission is ERRORED rather than FAILED so that a flaky runner cannot
        look like a bad catalog.
    """

    MERCHANT = "MERCHANT"
    HARNESS = "HARNESS"


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
    """Why the merchant would not produce a quote.

    Three, and each maps to a different failure reason, because the repairs are different.

    OUT_OF_STOCK
        The merchant sells it and could not hold enough of it.

    VARIANT_UNAVAILABLE
        The merchant does not sell it, or no longer does. A different finding from having run
        out, and folding the two together would hide a merchant whose catalog offers things it
        does not sell.

    MERCHANT_REFUSED
        Anything else the merchant refused a quote for.
    """

    OUT_OF_STOCK = "OUT_OF_STOCK"
    VARIANT_UNAVAILABLE = "VARIANT_UNAVAILABLE"
    MERCHANT_REFUSED = "MERCHANT_REFUSED"


@dataclass(frozen=True, slots=True)
class ObservedSelection:
    """What the executor chose to buy, as commerce facts rather than as a row reference.

    The category and the attributes are carried rather than looked up, for the same reason a
    checkout line snapshots them: a merchant editing a variant after the fact must not change
    what a historical measurement was made against. It also keeps the evaluator pure, which it
    could not be if deciding whether a charger was black meant reading the catalog.
    """

    variant_id: uuid.UUID
    quantity: int
    unit_price_amount_minor: int
    currency: str
    product_category: str | None = None
    variant_attributes: Mapping[str, Any] = field(default_factory=dict)

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
    """What happened when the executor asked the merchant to quote the selection.

    `created` is the fact; `refusal` says why not when it is false. `checkout_id` is optional
    because an executor that never produced a real quote row still has something to report, and
    a benchmark that could only record real rows could not measure the merchants that refuse to
    make them.
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


@dataclass(frozen=True, slots=True)
class ObservedAuthorization:
    """What the merchant's authorization layer said about the quote.

    `violations` holds the codes that layer reported, verbatim, for diagnostics. The evaluator
    reads `allowed` and nothing else: it is measuring whether the purchase was permitted, not
    re-deriving the permission itself, and re-deriving it would make the benchmark and the
    system it measures the same code marking its own work.
    """

    allowed: bool
    violations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ObservedPayment:
    """Where the payment got to.

    `PaymentAttemptStatus` rather than a benchmark specific enumeration, so there is one
    vocabulary for what a payment did. It carries the distinction that matters most here: a
    definitive decline and an unresolved payment are different facts, and the payment kernel is
    built on never calling one the other.
    """

    status: PaymentAttemptStatus
    attempt_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class ObservedAbstention:
    """The executor decided not to buy anything."""

    code: AbstentionCode
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ObservedError:
    """Something went wrong that was not a business refusal."""

    origin: ErrorOrigin
    detail: str

    def __post_init__(self) -> None:
        if not self.detail.strip():
            raise ValueError("an observed error must say what went wrong")


@dataclass(frozen=True, slots=True)
class ObservedResult:
    """One executor's complete report on one mission.

    Every part is optional because a mission can stop anywhere, and where it stopped is one of
    the things being measured. The merchant is not optional: which merchant was actually
    transacted with is ground truth the evaluator checks rather than assumes.
    """

    merchant_id: uuid.UUID
    selection: ObservedSelection | None = None
    checkout: ObservedCheckout | None = None
    authorization: ObservedAuthorization | None = None
    payment: ObservedPayment | None = None
    abstention: ObservedAbstention | None = None
    error: ObservedError | None = None

    @property
    def purchased(self) -> bool:
        """Whether a payment definitively succeeded.

        The one place "a purchase happened" is decided, so that no other part of the benchmark
        has to remember which payment statuses are terminal successes.
        """
        return self.payment is not None and self.payment.status is PaymentAttemptStatus.SUCCEEDED
