"""A scripted deterministic buyer, for proving the benchmark path rather than for measuring one.

What this is: a reference executor. It reads a mission brief, browses the merchant's catalog
through the ordinary buyer surface, filters candidates against the buyer's own stated
requirements, picks one by a fixed rule, creates the authorization, quotes it, holds the stock
and pays through the real payment kernel.

What this is not, and the distinction is load bearing rather than modest: it is not an AI buyer,
not an agent baseline and not a model baseline. It contains no model, no prompt and no language
understanding, and it reads structured commerce fields a real storefront does not publish. Its
completion rate is a statement about whether the benchmark plumbing works, and it must never be
presented as evidence of what an autonomous agent can do. See docs/benchmark.md.

Three rules shape it.

It sees only the brief. The signature takes an `AgentMissionBrief` and a merchant, this module
imports nothing that knows an oracle exists, and a test asserts both against the source rather
than against a promise.

It reads only what a buyer can read. Every fact it decides on arrives through
`BuyerCommerceSurface`, which is the application service layer with a merchant bound to it and
which returns the same view models the HTTP routes serialize. There is no session here, no
repository and no ORM row, so the harness cannot read an answer out of rows a buyer would have to
discover.

And it decides deterministically. No randomness, no clock in any decision, no set iteration and
no reliance on the order rows came back in. The same world and the same brief always produce the
same selection, which is what makes a rerun comparable with a run.

The objective is natural language and is deliberately not parsed. It travels into the buyer
intent recorded with the mandate, exactly as a real buyer's own words would, and nothing
deterministic is read out of it. Preferences are the same: they are advisory prose with no
machine readable semantics, so this executor records them and does not act on them. Turning
"prefer more ports" into a tie break would be inventing a requirement the buyer did not state,
and a soft preference silently promoted to a hard rule is exactly the mistake the intent model
exists to prevent.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from agentrank_api.benchmark.buyer import BuyerCommerceSurface
from agentrank_api.benchmark.definitions import AgentMissionBrief
from agentrank_api.benchmark.execution import ExecutorIdentity
from agentrank_api.benchmark.observation import (
    AbstentionCode,
    CheckoutRefusal,
    ErrorOrigin,
    ObservedAbstention,
    ObservedAuthorization,
    ObservedCheckout,
    ObservedError,
    ObservedPayment,
    ObservedResult,
    ObservedSelection,
)
from agentrank_api.checkout.schemas import (
    CheckoutItemInput,
    CheckoutView,
    CreateCheckoutRequest,
    ExecutionAuthorizationView,
)
from agentrank_api.commerce.schemas import (
    ProductSearchRequest,
    ProductSearchResult,
    VariantView,
)
from agentrank_api.commerce.search import MAX_SEARCH_LIMIT
from agentrank_api.constraints.rules import ConstraintOperator, compare, lookup_attribute
from agentrank_api.constraints.schemas import CreateIntentConstraintsRequest
from agentrank_api.errors import AgentRankError, ConflictError, NotFoundError
from agentrank_api.mandates.intent import (
    AllowedCategory,
    ConstraintKind,
    HardConstraint,
    MaxQuantity,
    MaxTotalAmount,
    RequiredAttribute,
)
from agentrank_api.mandates.schemas import (
    AllowedCategoryInput,
    BuyerIntentInput,
    CreateMandateRequest,
    HardConstraintInput,
    MaxQuantityInput,
    MaxTotalAmountInput,
    RequiredAttributeInput,
)
from agentrank_api.payments.schemas import AdmissionRefusal, CreatePaymentRequest

REFERENCE_EXECUTOR = ExecutorIdentity(kind="reference", version=1)

# How long the buyer authorizes spending for. Long enough that quoting, holding stock and paying
# cannot lapse mid mission on a slow machine, short enough that a mission which stops leaves
# nothing standing for long. A reservation expires at the earlier of this and the quote, so the
# quote's own default window is what actually governs.
MANDATE_WINDOW = timedelta(hours=1)

# The prefix every benchmark payment identity carries, so one is recognizable as this executor's
# in a payment table and in a provider's records.
IDEMPOTENCY_PREFIX = "ar-benchmark"

# Which admission refusals mean the buyer's own authorization said no. Only one does. Everything
# else, on a mandate and a quote and a hold this execution created moments ago, means the harness
# reached a state it should not have, and reporting one of those as a commerce finding would
# manufacture evidence about a merchant.
AUTHORIZATION_REFUSALS = frozenset({AdmissionRefusal.NOT_AUTHORIZED})

# What each merchant refusal to quote means as an observation. Written out rather than matched on
# prose, and a refusal that is not here is reported as a plain refusal rather than guessed at.
QUOTE_REFUSALS = {
    "insufficient_inventory": CheckoutRefusal.OUT_OF_STOCK,
    "variant_inactive": CheckoutRefusal.VARIANT_UNAVAILABLE,
    "product_inactive": CheckoutRefusal.VARIANT_UNAVAILABLE,
}


class Rejection(StrEnum):
    """Why one variant cannot satisfy this mission.

    Diagnostic, and used for exactly one thing: choosing which abstention code to report when
    nothing qualifies. The evaluator never reads an abstention code, because marking a mission
    from an executor's account of its own reasoning would be trusting the thing under test.

    WRONG_CURRENCY
        Priced in a currency the buyer's budget does not authorize. No amount comparison is made
        against it, because comparing across two currencies is meaningless rather than strict.

    UNSTATED
        The merchant's data does not say whether this qualifies. A category it never published,
        an attribute it never carried, or a value that cannot be compared with what was required.

    MISMATCH
        The merchant did publish the value and the value does not satisfy the requirement.

    OUT_OF_STOCK
        The merchant sells it and does not have enough of it.

    OVER_BUDGET
        Everything the buyer stated is satisfied and the money is not.
    """

    WRONG_CURRENCY = "WRONG_CURRENCY"
    UNSTATED = "UNSTATED"
    MISMATCH = "MISMATCH"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    OVER_BUDGET = "OVER_BUDGET"


# Which abstention this executor reports, given everything that was wrong with everything it saw.
# Ordered most informative first, and budget leads because a candidate rejected only on price is
# one that satisfied every stated requirement, which is the most useful thing an abstention can
# say. A test pins this order.
ABSTENTION_FOR: tuple[tuple[Rejection, AbstentionCode], ...] = (
    (Rejection.OVER_BUDGET, AbstentionCode.BUDGET_INSUFFICIENT),
    (Rejection.UNSTATED, AbstentionCode.MERCHANT_DATA_INSUFFICIENT),
)


@dataclass(frozen=True, slots=True)
class Candidate:
    """One purchasable configuration, as the buyer surface described it.

    Everything the decision rests on, carried rather than looked up again, so a selection is
    made from one consistent reading of the merchant's own answer rather than from several.
    """

    variant_id: uuid.UUID
    sku: str
    unit_price_amount_minor: int
    currency: str
    inventory_quantity: int
    product_category: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    def line_amount_minor(self, quantity: int) -> int:
        return self.unit_price_amount_minor * quantity


@dataclass(frozen=True, slots=True)
class Quoted:
    """A request to quote, answered. One of the two fields is always set and never both."""

    checkout: CheckoutView | None = None
    refusal: CheckoutRefusal | None = None


def assess(brief: AgentMissionBrief, candidate: Candidate) -> Rejection | None:
    """Why this candidate cannot satisfy this mission, or None when it can.

    The order is fixed and it is not the order a purchase happens in. Currency first, because
    every later comparison is meaningless without it. Then what the merchant says the thing is,
    because "the data does not answer" and "the answer is wrong" are different findings and the
    first is the thing this benchmark exists to measure. Then stock, then money, so that a
    candidate rejected only on price is reported as exactly that: everything the buyer asked for,
    at a price they did not authorize.

    Fail closed in the same three ways the evaluator is, through the same comparison vocabulary
    the authorization gate uses. A category the merchant never published cannot be an allowed
    one, an attribute that is absent is never a pass, and a value that cannot be compared is
    never a pass either. Sharing the vocabulary is deliberate: an executor that considered
    something acceptable which the gate would then deny would be measuring its own optimism.
    """
    if candidate.currency != brief.currency:
        return Rejection.WRONG_CURRENCY

    allowed = tuple(
        constraint.category
        for constraint in brief.hard_constraints
        if isinstance(constraint, AllowedCategory)
    )
    if allowed:
        if candidate.product_category is None:
            return Rejection.UNSTATED
        satisfied = compare(ConstraintOperator.IN, allowed, candidate.product_category)
        if satisfied is None:
            return Rejection.UNSTATED
        if not satisfied:
            return Rejection.MISMATCH

    for constraint in brief.hard_constraints:
        if not isinstance(constraint, RequiredAttribute):
            continue
        found, actual = lookup_attribute(candidate.attributes, constraint.name)
        if not found:
            return Rejection.UNSTATED
        satisfied = compare(constraint.operator, constraint.value, actual)
        if satisfied is None:
            return Rejection.UNSTATED
        if not satisfied:
            return Rejection.MISMATCH

    if candidate.inventory_quantity < brief.quantity:
        return Rejection.OUT_OF_STOCK
    if candidate.line_amount_minor(brief.quantity) > brief.budget.amount_minor:
        return Rejection.OVER_BUDGET
    return None


def select(candidates: list[Candidate], *, quantity: int) -> Candidate:
    """The one candidate this executor buys, chosen by a rule that never moves.

    Cheapest total first, then SKU ascending. Both halves matter.

    Cheapest is the only defensible preference a buyer who stated no preference has: every
    candidate reaching here satisfies every requirement the buyer stated, so the buyer is
    indifferent between them on everything they said, and spending more of their own budget than
    the task needs is a choice nobody asked for.

    The SKU tie break is what makes the rule total. Two variants at one price would otherwise be
    separated by whatever the merchant's search happened to return first, which depends on the
    query plan, and a benchmark whose selection depends on a query plan is not reproducible. The
    SKU is the merchant's own identifier, unique within a merchant and stable across databases,
    which a generated identifier and a row position are both not.

    Nothing here reads a preference. A preference is advisory prose with no machine readable
    semantics, and turning one into a tie break would convert a soft statement into a hard rule.

    The evaluator does not require this particular choice. Any variant the mission's ground truth
    allows completes the mission, and this rule decides which of them the reference executor
    takes.
    """
    if not candidates:
        raise ValueError("nothing to select from")
    return min(candidates, key=lambda entry: (entry.line_amount_minor(quantity), entry.sku))


class ReferenceMissionExecutor:
    """The deterministic reference executor, as one object with one call.

    Constructed with a buyer surface rather than a session, which is what makes "it cannot read
    the database" structural rather than a rule somebody follows. It holds no state between
    missions and none within one: every decision is made from values passed down the call.
    """

    identity = REFERENCE_EXECUTOR

    def __init__(self, surface: BuyerCommerceSurface) -> None:
        self._surface = surface

    async def __call__(self, brief: AgentMissionBrief, *, merchant_id: uuid.UUID) -> ObservedResult:
        """Carry out one mission and report what happened, never what it meant.

        The merchant is required to be the one this surface shops at, and a mismatch raises
        rather than being reported. A harness pointed at the wrong merchant is a
        misconfiguration, and letting it through would fill a run with `WRONG_MERCHANT` findings
        that say nothing about any merchant.

        A refusal that reaches here is one no step below expected, so it is reported as a
        merchant surface answering with an error rather than with a business outcome. Anything
        else propagates: an unexpected exception inside a benchmark harness should be loud.
        """
        if merchant_id != self._surface.merchant_id:
            raise ValueError(
                f"this executor shops at merchant {self._surface.merchant_id} and was asked to"
                f" execute mission {brief.key!r} at {merchant_id}"
            )

        try:
            return await self._execute(brief)
        except AgentRankError as refused:
            return ObservedResult(
                merchant_id=merchant_id,
                error=ObservedError(origin=ErrorOrigin.MERCHANT, detail=_detail(refused)),
            )

    async def _execute(self, brief: AgentMissionBrief) -> ObservedResult:
        merchant_id = self._surface.merchant_id
        candidates, rejections = await self._candidates(brief)
        if not candidates:
            return ObservedResult(
                merchant_id=merchant_id,
                abstention=ObservedAbstention(code=_abstention(rejections)),
            )

        chosen = select(candidates, quantity=brief.quantity)
        selection = ObservedSelection(
            variant_id=chosen.variant_id,
            quantity=brief.quantity,
            unit_price_amount_minor=chosen.unit_price_amount_minor,
            currency=chosen.currency,
            product_category=chosen.product_category,
            variant_attributes=chosen.attributes,
        )

        mandate_id = await self._authorize(brief)
        quoted = await self._quote(brief, chosen, mandate_id=mandate_id)
        if quoted.checkout is None:
            return ObservedResult(
                merchant_id=merchant_id,
                selection=selection,
                checkout=ObservedCheckout(created=False, refusal=quoted.refusal),
            )
        checkout_id = quoted.checkout.id

        preparation = await self._surface.prepare_checkout(checkout_id)
        if not preparation.authorization.authorized:
            # The buyer's own authorization refused, and the merchant offered nothing but the
            # quote. Reported as a created quote with a denial beside it, which is what makes
            # this a mandate denial rather than a merchant that could not supply the item.
            return ObservedResult(
                merchant_id=merchant_id,
                selection=selection,
                checkout=_quoted(quoted.checkout),
                authorization=_authorization(preparation.authorization),
            )
        if not preparation.ready:
            # Authorized, and the merchant could not hold the stock. An offer nothing can be
            # bought against is not an offer, so it is reported as a merchant refusal rather
            # than as a quote the buyer simply failed to act on.
            return ObservedResult(
                merchant_id=merchant_id,
                selection=selection,
                checkout=ObservedCheckout(
                    created=False,
                    checkout_id=checkout_id,
                    refusal=CheckoutRefusal.OUT_OF_STOCK,
                ),
            )

        payment = await self._surface.complete_checkout(
            checkout_id, CreatePaymentRequest(idempotency_key=idempotency_key(checkout_id))
        )
        attempt = payment.attempt
        if attempt is None:
            return self._refused_payment(payment.refusal, selection, quoted.checkout)

        # Read back rather than remembered. The quote is reported as the merchant records it
        # after the payment, which is the authoritative account of what the buyer was charged.
        settled = await self._surface.get_checkout(checkout_id)
        return ObservedResult(
            merchant_id=merchant_id,
            selection=selection,
            checkout=_quoted(settled),
            authorization=_authorization(preparation.authorization),
            payment=ObservedPayment(status=attempt.status, attempt_id=attempt.id),
        )

    async def _candidates(self, brief: AgentMissionBrief) -> tuple[list[Candidate], set[Rejection]]:
        """Everything the merchant offers that satisfies this mission, and why the rest do not.

        Browse and then open, which is how a buyer reads a catalog. The search is unfiltered
        because a buyer's requirements are structured and this merchant's search surface cannot
        express them: it filters on prose, price and currency, and no combination of those
        answers "black, at least a hundred watts, from the chargers range". So the executor takes
        the catalog and applies the buyer's own requirements itself, which is also what a future
        agent will have to do until a merchant publishes something better.

        Every hit is opened, because a search result carries only the variants that passed the
        search filters while a product read carries every variant the merchant has. A withdrawn
        variant is visible there and is skipped here, and skipped without being counted as a
        rejection: the merchant is not offering it at all, so there is nothing about this buyer
        it failed.
        """
        listing = await self._surface.search_products(ProductSearchRequest(limit=MAX_SEARCH_LIMIT))
        candidates: list[Candidate] = []
        rejections: set[Rejection] = set()

        for hit in _in_catalog_order(listing.results):
            product = await self._surface.get_product(hit.id)
            for offered in product.variants:
                if not offered.is_active:
                    continue
                candidate = _candidate(product.category, offered)
                rejection = assess(brief, candidate)
                if rejection is None:
                    candidates.append(candidate)
                else:
                    rejections.add(rejection)
        return candidates, rejections

    async def _authorize(self, brief: AgentMissionBrief) -> uuid.UUID:
        """Create the mandate and the constraint set this purchase will be made under.

        The mission's own vocabulary, unchanged. The budget becomes the mandate's ceiling and the
        stated quantity ceiling becomes the mandate's, so the authorization is exactly as wide as
        the buyer said and never wider. The semantic requirements become the constraint set the
        execution gate reads.

        Nothing here is a benchmark specific authorization path. A mission is turned into the
        same `BuyerIntent`, the same `SpendingMandate` and the same `IntentConstraintSet` an
        ordinary purchase uses, which is what makes the benchmark exercise the rules that
        actually protect a buyer rather than a copy of them.

        A brief that states no semantic requirement gets no constraint set, and the execution
        gate then denies for `INTENT_CONSTRAINTS_MISSING`. That is the system's own rule that the
        absence of a semantic authorization is not a passed one, and the honest thing is to let
        the mission fail on it rather than to invent a requirement so that it can proceed.
        """
        stated = [_constraint_input(constraint) for constraint in brief.hard_constraints]
        semantic = [
            entry
            for entry, constraint in zip(stated, brief.hard_constraints, strict=True)
            if isinstance(constraint, AllowedCategory | RequiredAttribute)
        ]
        now = datetime.now(UTC)
        mandate = await self._surface.authorize_spending(
            CreateMandateRequest(
                max_total_amount_minor=brief.budget.amount_minor,
                currency=brief.currency,
                max_quantity=brief.max_quantity,
                valid_from=now,
                valid_until=now + MANDATE_WINDOW,
                intent=BuyerIntentInput(
                    # The buyer's own words, carried through and never parsed.
                    description=brief.objective,
                    hard_constraints=[_constraint_input(brief.budget), *stated],
                    preferences=[preference.statement for preference in brief.preferences],
                ),
            )
        )
        if semantic:
            await self._surface.state_requirements(
                mandate.id, CreateIntentConstraintsRequest(constraints=semantic)
            )
        return mandate.id

    async def _quote(
        self, brief: AgentMissionBrief, chosen: Candidate, *, mandate_id: uuid.UUID
    ) -> Quoted:
        """Ask the merchant to quote the selection, or record why it would not.

        A refusal to quote is a commerce fact rather than an error, so it is caught and
        translated into the refusal it means. An unrecognized refusal is reported as a plain
        merchant refusal rather than guessed into a more specific one.
        """
        try:
            checkout = await self._surface.create_checkout(
                CreateCheckoutRequest(
                    mandate_id=mandate_id,
                    items=[
                        CheckoutItemInput(variant_id=chosen.variant_id, quantity=brief.quantity)
                    ],
                )
            )
        except ConflictError as refused:
            return Quoted(
                refusal=QUOTE_REFUSALS.get(refused.reason, CheckoutRefusal.MERCHANT_REFUSED)
            )
        except NotFoundError:
            # The merchant does not have this variant at all, which is what a hallucinated
            # identifier looks like and is a different finding from having run out of one.
            return Quoted(refusal=CheckoutRefusal.VARIANT_UNAVAILABLE)
        return Quoted(checkout=checkout)

    def _refused_payment(
        self,
        refusal: AdmissionRefusal | None,
        selection: ObservedSelection,
        checkout: CheckoutView,
    ) -> ObservedResult:
        """What to report when a payment was refused before any provider was involved.

        One refusal is a finding and the rest are faults, and the split is the point. A payment
        the authorization gates denied is the safety layer working, and it is reported as a
        denial. Every other refusal, on a mandate and a quote and a hold this execution created
        moments ago, means the harness reached a state it should not have: a mandate already
        consumed, a quote already paid, a payment already in progress. None of those is a fact
        about the merchant, so they are reported as harness errors instead.
        """
        if refusal in AUTHORIZATION_REFUSALS:
            return ObservedResult(
                merchant_id=self._surface.merchant_id,
                selection=selection,
                checkout=_quoted(checkout),
                authorization=ObservedAuthorization(
                    allowed=False, violations=() if refusal is None else (refusal.value,)
                ),
            )
        return ObservedResult(
            merchant_id=self._surface.merchant_id,
            error=ObservedError(
                origin=ErrorOrigin.HARNESS,
                detail=f"payment refused as {'unknown' if refusal is None else refusal.value}",
            ),
        )


def idempotency_key(checkout_id: uuid.UUID) -> str:
    """The identity this executor pays one quote under.

    Derived from the quote rather than chosen, and that is what makes a repeat safe. An
    idempotency key is scoped to its checkout, so a second payment request for the same quote
    resolves to the first attempt instead of writing a second one, and a mission retried against
    a quote it already created cannot charge twice.

    Deterministic and server derived. There is no clock in it, no counter and no caller supplied
    string, so the same quote always produces the same identity and two quotes never produce one.
    """
    return f"{IDEMPOTENCY_PREFIX}-{checkout_id.hex}"


def _abstention(rejections: set[Rejection]) -> AbstentionCode:
    """Which abstention to report, given everything that was wrong with everything seen.

    Nothing seen at all is `NO_CANDIDATE_FOUND`, which is a statement about the catalog surface
    rather than about the buyer's requirements, and it is deliberately not the same finding as
    having seen things that did not qualify.

    The rest follow `ABSTENTION_FOR` in order. This is diagnostic only: the evaluator never reads
    it, because an incorrect abstention is a discovery failure whichever code was claimed.
    """
    if not rejections:
        return AbstentionCode.NO_CANDIDATE_FOUND
    for rejection, code in ABSTENTION_FOR:
        if rejection in rejections:
            return code
    return AbstentionCode.NO_COMPLIANT_CANDIDATE


def _candidate(category: str | None, variant: VariantView) -> Candidate:
    return Candidate(
        variant_id=variant.id,
        sku=variant.sku,
        unit_price_amount_minor=variant.price_amount_minor,
        currency=variant.currency,
        inventory_quantity=variant.inventory_quantity,
        product_category=category,
        attributes=dict(variant.attributes),
    )


def _in_catalog_order(results: list[ProductSearchResult]) -> list[ProductSearchResult]:
    """The merchant's hits in an order this executor chose rather than one it was handed.

    The search orders by title and then identifier, which is stable and is still the merchant's
    choice. Sorting by the merchant's own external identifier here costs nothing and removes the
    dependency: what a buyer opens first must not change because somebody renamed a product.
    Selection does not depend on it either, because `select` is total, and having both is
    deliberate rather than redundant.
    """
    return sorted(results, key=lambda hit: hit.external_id)


def _quoted(checkout: CheckoutView) -> ObservedCheckout:
    """One quote as the observation model records it."""
    return ObservedCheckout(
        created=True,
        checkout_id=checkout.id,
        total_amount_minor=checkout.total_amount_minor,
        currency=checkout.currency,
    )


def _authorization(decision: ExecutionAuthorizationView) -> ObservedAuthorization:
    """What both gates said, as one observation.

    `allowed` is the composed answer and nothing else is read from it by the evaluator. The
    violation codes from all three levels are carried for diagnostics, because a denial from the
    money, a denial from the purchase and there being no semantic authorization at all are three
    different problems and a report that could not tell them apart would be worth little.
    """
    violations = [violation.value for violation in decision.violations]
    violations.extend(violation.value for violation in decision.financial_authorization.violations)
    if decision.intent_authorization is not None:
        violations.extend(
            violation.code.value for violation in decision.intent_authorization.violations
        )
    return ObservedAuthorization(allowed=decision.authorized, violations=tuple(violations))


def _constraint_input(constraint: HardConstraint) -> HardConstraintInput:
    """One stated requirement in the shape the commerce request models take.

    Written out per kind rather than inferred, so a constraint kind added later is a compile time
    question here rather than a requirement silently dropped from an authorization.
    """
    match constraint:
        case MaxTotalAmount():
            return MaxTotalAmountInput(
                kind=ConstraintKind.MAX_TOTAL_AMOUNT,
                amount_minor=constraint.amount_minor,
                currency=constraint.currency,
            )
        case MaxQuantity():
            return MaxQuantityInput(kind=ConstraintKind.MAX_QUANTITY, quantity=constraint.quantity)
        case AllowedCategory():
            return AllowedCategoryInput(
                kind=ConstraintKind.ALLOWED_CATEGORY, category=constraint.category
            )
        case RequiredAttribute():
            return RequiredAttributeInput(
                kind=ConstraintKind.REQUIRED_ATTRIBUTE,
                name=constraint.name,
                operator=constraint.operator,
                value=list(constraint.value)
                if isinstance(constraint.value, tuple)
                else constraint.value,
            )


def _detail(error: AgentRankError) -> str:
    """What to record about a refusal, without inventing a vocabulary for it.

    The stable reason code when there is one, because a report a script may read should not
    depend on English. Prose otherwise.
    """
    if isinstance(error, ConflictError):
        return error.reason
    return str(error)
