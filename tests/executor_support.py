"""Executors a test can drive, which really shop and then say whatever they are told to say.

Two things live here and the second is the reason the first exists.

`ScriptedBuyer` carries out a mission through the real buyer surface: a real mandate, a real
constraint set, a real quote, a real hold and a real payment through the real kernel. What it
buys is chosen by the test rather than by a selection rule, so a test can produce an over budget
purchase, a purchase of the wrong thing, or a mission that stops halfway, without waiting for a
catalog that would make a well behaved executor do it.

`lie` is what makes it adversarial. The honest report is produced first, from what actually
happened, and then rewritten into whatever the test wants claimed. That is the shape a dishonest
model takes: it acts, and then it describes what it did in flattering terms. Every test that
asserts a lie does not work uses this, so what is being asserted is that trusted state wins over
a claim rather than that a claim was refused at construction.

Nothing here inserts a row directly. Every piece of commerce comes out of the service that owns
it, so a benchmark result built on this is a result about the application.
"""

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentrank_api.benchmark.buyer import BuyerCommerceSurface, MerchantBuyerSurface
from agentrank_api.benchmark.definitions import AgentMissionBrief
from agentrank_api.benchmark.execution import ExecutorIdentity
from agentrank_api.benchmark.report import (
    AbstentionCode,
    CheckoutRefusal,
    ExecutorReport,
    ReportedAbstention,
    ReportedCheckout,
    ReportedPayment,
    ReportedSelection,
)
from agentrank_api.benchmark.tools import MeasuredBuyerSurface, ToolLedger
from agentrank_api.checkout.schemas import CheckoutItemInput, CreateCheckoutRequest
from agentrank_api.constraints.rules import ConstraintOperator
from agentrank_api.constraints.schemas import CreateIntentConstraintsRequest
from agentrank_api.mandates.intent import (
    AllowedCategory,
    ConstraintKind,
    HardConstraint,
    RequiredAttribute,
)
from agentrank_api.mandates.schemas import (
    AllowedCategoryInput,
    BuyerIntentInput,
    CreateMandateRequest,
    HardConstraintInput,
    MaxTotalAmountInput,
    RequiredAttributeInput,
)
from agentrank_api.payments.fake import FakePaymentProvider
from agentrank_api.payments.provider import PaymentProvider
from agentrank_api.payments.schemas import CreatePaymentRequest

MANDATE_WINDOW = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class Buy:
    """Buy this variant, whatever a sensible buyer would have done.

    `mandate_amount_minor` is what the buyer authorizes itself to spend, which is deliberately
    separate from the mission's budget: a purchase over the mission's ceiling is exactly what a
    test needs to produce, and it can only happen if the mandate allows it.

    `require` is the semantic constraint set the purchase is made under. It defaults to the
    mission's own semantic requirements, because a constraint set that the selection does not
    satisfy is denied by the execution gate and no purchase happens at all. A set is required:
    the system treats an absent one as the absence of an authorization rather than a permissive
    one, which is a rule this helper deliberately does not work around.

    `stop_after` ends the mission early, so a test can produce a quote nobody paid for or a
    preparation nobody acted on.
    """

    variant_id: uuid.UUID
    quantity: int = 1
    mandate_amount_minor: int | None = None
    require: tuple[HardConstraint, ...] | None = None
    stop_after: str | None = None


@dataclass(frozen=True, slots=True)
class Decline:
    """Do nothing and say so."""

    code: AbstentionCode = AbstentionCode.NO_COMPLIANT_CANDIDATE


Action = Buy | Decline

# Where a scripted buy may be told to stop, in the order the steps happen.
QUOTE = "quote"
PREPARE = "prepare"


class ScriptedBuyer:
    """An executor that does what a test says and reports what a test says.

    It declares its own identity, so a run driven by it is never mistaken for one produced by
    the reference executor.
    """

    identity = ExecutorIdentity(kind="scripted", version=1)

    def __init__(
        self,
        surface: BuyerCommerceSurface,
        script: Mapping[str, Action],
        *,
        lie: Callable[[str, ExecutorReport], ExecutorReport] | None = None,
    ) -> None:
        self._surface = surface
        self._script = script
        self._lie = lie
        self.honest: dict[str, ExecutorReport] = {}

    async def __call__(self, brief: AgentMissionBrief, *, merchant_id: uuid.UUID) -> ExecutorReport:
        action = self._script[brief.key]
        report = (
            await self._buy(brief, action, merchant_id=merchant_id)
            if isinstance(action, Buy)
            else ExecutorReport(
                merchant_id=merchant_id,
                abstention=ReportedAbstention(code=action.code),
            )
        )
        self.honest[brief.key] = report
        return report if self._lie is None else self._lie(brief.key, report)

    async def _buy(
        self, brief: AgentMissionBrief, action: Buy, *, merchant_id: uuid.UUID
    ) -> ExecutorReport:
        mandate_id = await self._authorize(brief, action)
        checkout = await self._surface.create_checkout(
            CreateCheckoutRequest(
                mandate_id=mandate_id,
                items=[CheckoutItemInput(variant_id=action.variant_id, quantity=action.quantity)],
            )
        )
        selection = ReportedSelection(variant_id=action.variant_id, quantity=action.quantity)
        if action.stop_after == QUOTE:
            return ExecutorReport(
                merchant_id=merchant_id,
                selection=selection,
                checkout=ReportedCheckout(checkout_id=checkout.id),
            )

        preparation = await self._surface.prepare_checkout(checkout.id)
        if not preparation.ready or action.stop_after == PREPARE:
            # A denial and an empty shelf are different findings, exactly as they are for the
            # reference executor. A denied purchase names the quote and says nothing about why,
            # because what the authorization layer decided is recorded at the tool boundary.
            refused = (
                CheckoutRefusal.OUT_OF_STOCK
                if not preparation.ready and preparation.authorization.authorized
                else None
            )
            return ExecutorReport(
                merchant_id=merchant_id,
                selection=selection,
                checkout=ReportedCheckout(checkout_id=checkout.id, refusal=refused),
            )

        paid = await self._surface.complete_checkout(
            checkout.id, CreatePaymentRequest(idempotency_key=f"scripted-{checkout.id.hex}")
        )
        return ExecutorReport(
            merchant_id=merchant_id,
            selection=selection,
            checkout=ReportedCheckout(checkout_id=checkout.id),
            payment=None if paid.attempt is None else ReportedPayment(attempt_id=paid.attempt.id),
        )

    async def _authorize(self, brief: AgentMissionBrief, action: Buy) -> uuid.UUID:
        amount = (
            brief.budget.amount_minor
            if action.mandate_amount_minor is None
            else action.mandate_amount_minor
        )
        stated = action.require if action.require is not None else _semantic(brief)
        now = datetime.now(UTC)
        mandate = await self._surface.authorize_spending(
            CreateMandateRequest(
                max_total_amount_minor=amount,
                currency=brief.currency,
                valid_from=now,
                valid_until=now + MANDATE_WINDOW,
                intent=BuyerIntentInput(
                    description=brief.objective,
                    hard_constraints=[
                        MaxTotalAmountInput(
                            kind=ConstraintKind.MAX_TOTAL_AMOUNT,
                            amount_minor=amount,
                            currency=brief.currency,
                        )
                    ],
                    preferences=[],
                ),
            )
        )
        if stated:
            await self._surface.state_requirements(
                mandate.id,
                CreateIntentConstraintsRequest(
                    constraints=[_input(constraint) for constraint in stated]
                ),
            )
        identifier: uuid.UUID = mandate.id
        return identifier


def _semantic(brief: AgentMissionBrief) -> tuple[HardConstraint, ...]:
    """The mission's own semantic requirements, which a compliant purchase satisfies."""
    return tuple(
        constraint
        for constraint in brief.hard_constraints
        if isinstance(constraint, AllowedCategory | RequiredAttribute)
    )


def _input(constraint: HardConstraint) -> HardConstraintInput:
    """One stated requirement in the shape the commerce request models take."""
    if isinstance(constraint, AllowedCategory):
        return AllowedCategoryInput(
            kind=ConstraintKind.ALLOWED_CATEGORY, category=constraint.category
        )
    if isinstance(constraint, RequiredAttribute):
        value = list(constraint.value) if isinstance(constraint.value, tuple) else constraint.value
        return RequiredAttributeInput(
            kind=ConstraintKind.REQUIRED_ATTRIBUTE,
            name=constraint.name,
            operator=ConstraintOperator(constraint.operator),
            value=value,
        )
    raise TypeError(f"a scripted buyer states no {type(constraint).__name__}")


def scripted(
    sessions: async_sessionmaker[AsyncSession],
    merchant_id: uuid.UUID,
    script: Mapping[str, Action],
    *,
    lie: Callable[[str, ExecutorReport], ExecutorReport] | None = None,
    provider: PaymentProvider | None = None,
) -> tuple[ScriptedBuyer, ToolLedger]:
    """A scripted buyer behind the same trusted boundary a run puts an executor behind.

    The ledger comes back beside it because the runner needs it as the witness: it is what sees
    the merchant's authorization answers, and a run without it is a run that attributes no
    authorization at all.
    """
    ledger = ToolLedger()
    surface = MeasuredBuyerSurface(
        MerchantBuyerSurface(
            sessions, merchant_id=merchant_id, provider=provider or FakePaymentProvider()
        ),
        ledger,
    )
    return ScriptedBuyer(surface, script, lie=lie), ledger
