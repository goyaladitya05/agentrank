"""Trusted authorization provisioning for an LLM benchmark mission.

The model receives a pre-created mandate only through the checkout tool implementation.  It has
no tool for creating, widening, qualifying, or revoking authorization.
"""

import uuid
from datetime import UTC, datetime, timedelta

from agentrank_api.benchmark.buyer import BuyerCommerceSurface
from agentrank_api.benchmark.definitions import AgentMissionBrief
from agentrank_api.constraints.schemas import CreateIntentConstraintsRequest
from agentrank_api.mandates.intent import (
    AllowedCategory,
    ConstraintKind,
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

MANDATE_WINDOW = timedelta(hours=1)


def _input(
    constraint: AllowedCategory | MaxQuantity | MaxTotalAmount | RequiredAttribute,
) -> HardConstraintInput:
    match constraint:
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
        case MaxTotalAmount():
            return MaxTotalAmountInput(
                kind=ConstraintKind.MAX_TOTAL_AMOUNT,
                amount_minor=constraint.amount_minor,
                currency=constraint.currency,
            )
        case MaxQuantity():
            return MaxQuantityInput(kind=ConstraintKind.MAX_QUANTITY, quantity=constraint.quantity)
        case _:
            raise TypeError(f"unknown hard constraint {type(constraint).__name__}")


async def provision(surface: BuyerCommerceSurface, brief: AgentMissionBrief) -> uuid.UUID:
    """Create exactly the authorization stated by the brief, before model execution."""
    stated = [_input(constraint) for constraint in brief.hard_constraints]
    semantic = [
        entry
        for entry, constraint in zip(stated, brief.hard_constraints, strict=True)
        if isinstance(constraint, (AllowedCategory, RequiredAttribute))
    ]
    now = datetime.now(UTC)
    mandate = await surface.authorize_spending(
        CreateMandateRequest(
            max_total_amount_minor=brief.budget.amount_minor,
            currency=brief.currency,
            max_quantity=brief.max_quantity,
            valid_from=now,
            valid_until=now + MANDATE_WINDOW,
            intent=BuyerIntentInput(
                description=brief.objective,
                hard_constraints=[_input(brief.budget), *stated],
                preferences=[item.statement for item in brief.preferences],
            ),
        )
    )
    if semantic:
        await surface.state_requirements(
            mandate.id, CreateIntentConstraintsRequest(constraints=semantic)
        )
    return mandate.id
