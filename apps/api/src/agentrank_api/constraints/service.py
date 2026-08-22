"""Intent constraint application service.

One workflow, and it is the one that turns a request into authorization data: take the
hard constraints a buyer stated, validate them, write the enforceable ones to their own
tables, and record that it happened. The service owns the transaction. Routes call one
method and serialize the result.

Three rules shape this module:

- a constraint set and the audit event recording it commit together or not at all
- there is no update and no delete. Changing what a buyer requires means a new mandate
  with a new constraint set, which leaves the original intact and auditable
- financial constraints are validated against the mandate and never stored. A ceiling with
  two homes is a ceiling that can disagree with itself, and a stated limit that is looser
  in one place than the other is exactly the silent widening this phase exists to prevent
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.audit.models import ActorType
from agentrank_api.audit.repository import AuditRepository
from agentrank_api.benchmark.execution import BenchmarkRunCapability
from agentrank_api.benchmark.mutation import BenchmarkMutationGuard
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.conflicts import translated_conflicts
from agentrank_api.constraints.models import IntentConstraintSet
from agentrank_api.constraints.repository import IntentConstraintRepository
from agentrank_api.constraints.rules import (
    IntentConstraintSpec,
    normalize_text,
)
from agentrank_api.errors import ConflictError, NotFoundError
from agentrank_api.mandates.intent import (
    MAX_HARD_CONSTRAINTS,
    AllowedCategory,
    HardConstraint,
    MaxQuantity,
    MaxTotalAmount,
    RequiredAttribute,
)
from agentrank_api.mandates.models import MandateStatus, SpendingMandate
from agentrank_api.mandates.repository import MandateRepository

CONSTRAINTS_RESOURCE = "intent_constraint_set"
CONSTRAINTS_CREATED = "intent_constraints.created"

# Stating what must be bought is the buyer's act, exactly as granting spending authority is.
# This names a role and not a person, and it still does now that requests are authenticated: a
# credential proves which merchant integration asked, not who was holding the key. The
# credential is recorded beside the role rather than instead of it.
CONSTRAINTS_ACTOR = ActorType.BUYER


@dataclass(frozen=True, slots=True)
class NewIntentConstraints:
    """A request to qualify one mandate, refused before it reaches the database if wrong.

    The input is the same `HardConstraint` union a `BuyerIntent` carries, so there is one
    vocabulary for stating a requirement rather than a buyer facing one and an
    authorization one that drift apart.

    Every rule that can be decided without the mandate is decided here, which is what turns
    a refusal into a 422 naming the problem rather than an integrity error from inside a
    route. The rules that need the mandate, which are the financial ones, are the service's.
    """

    merchant_id: uuid.UUID
    mandate_id: uuid.UUID
    hard_constraints: tuple[HardConstraint, ...]

    def __post_init__(self) -> None:
        if not self.hard_constraints:
            raise ValueError("at least one hard constraint is required")
        if len(self.hard_constraints) > MAX_HARD_CONSTRAINTS:
            raise ValueError(f"at most {MAX_HARD_CONSTRAINTS} hard constraints are allowed")

        specs = self.semantic_specs()
        if not specs:
            # A request carrying only financial constraints asks for a semantic
            # authorization with nothing in it. That is the absence of an authorization
            # rather than a permissive one, and storing it would let a checkout report as
            # satisfied because nothing was ever required.
            raise ValueError(
                "at least one allowed_category or required_attribute constraint is required"
            )

        targets = [(spec.kind, _target_key(spec), spec.operator) for spec in specs]
        if len(set(targets)) != len(targets):
            # Two EQ rules for one attribute are a contradiction rather than a tighter
            # bound. The database refuses this too; saying so here names the problem.
            raise ValueError("two hard constraints state the same rule for the same attribute")

    def semantic_specs(self) -> tuple[IntentConstraintSpec, ...]:
        """The enforceable constraints, in the order the buyer stated them.

        Several `AllowedCategory` constraints mean any one of them rather than all of
        them, so they fold into a single membership rule. It takes the position of the
        first category the buyer named, which keeps the stored order recognisable as the
        order it was asked in. Categories repeated in different capitalisations collapse,
        because that is the same comparison the evaluator will make.
        """
        specs: list[IntentConstraintSpec | None] = []
        categories: list[str] = []
        seen: set[str] = set()
        category_slot: int | None = None

        for constraint in self.hard_constraints:
            match constraint:
                case AllowedCategory():
                    if normalize_text(constraint.category) not in seen:
                        seen.add(normalize_text(constraint.category))
                        categories.append(constraint.category)
                    if category_slot is None:
                        category_slot = len(specs)
                        specs.append(None)
                case RequiredAttribute():
                    specs.append(
                        IntentConstraintSpec.required_attribute(
                            constraint.name, constraint.operator, constraint.value
                        )
                    )
                case MaxTotalAmount() | MaxQuantity():
                    # Validated against the mandate by the service, never stored.
                    pass

        if category_slot is not None:
            specs[category_slot] = IntentConstraintSpec.allowed_categories(tuple(categories))
        return tuple(spec for spec in specs if spec is not None)

    def financial_constraints(self) -> tuple[MaxTotalAmount | MaxQuantity, ...]:
        return tuple(
            constraint
            for constraint in self.hard_constraints
            if isinstance(constraint, MaxTotalAmount | MaxQuantity)
        )


class IntentConstraintService:
    def __init__(
        self, session: AsyncSession, *, benchmark_capability: BenchmarkRunCapability | None = None
    ) -> None:
        self._session = session
        self._benchmark_capability = benchmark_capability
        self._mutation = BenchmarkMutationGuard(session)
        self._merchants = MerchantRepository(session)
        self._mandates = MandateRepository(session)
        self._constraints = IntentConstraintRepository(session)
        self._audit = AuditRepository(session)

    async def create_constraints(
        self, request: NewIntentConstraints, *, credential_id: uuid.UUID | None = None
    ) -> IntentConstraintSet:
        """Qualify a mandate with the constraints a purchase must satisfy, in one
        transaction.

        Everything is looked up before anything is written, so an unknown merchant or
        mandate is a 404 naming the resource rather than a foreign key violation surfacing
        as a server error.

        Both writes happen in one transaction and one commit. If the audit append fails,
        neither the constraint set nor its constraints are persisted: authorization data
        with no record of being granted is exactly what the audit trail exists to prevent.

        A mandate is qualified once, and the read that enforces that can be true for two
        callers at the same time. The unique constraint on `mandate_id` is what actually
        prevents the second set, and it is translated into the same refusal the read would
        have given, so a caller cannot tell whether it lost a race.
        """
        await self._mutation.require_allowed(
            request.merchant_id, capability=self._benchmark_capability
        )
        merchant = await self._merchants.get_by_id(request.merchant_id)
        if merchant is None:
            raise NotFoundError("merchant", str(request.merchant_id))

        # Another merchant's mandate does not exist as far as this merchant is concerned.
        # That is both the isolation rule and the honest answer: a caller scoped to one
        # merchant must not learn what another merchant has authorized. The merchant is in the
        # query rather than compared afterwards, so there is nothing to forget.
        mandate = await self._mandates.get(request.mandate_id, merchant_id=request.merchant_id)
        if mandate is None:
            raise NotFoundError("mandate", str(request.mandate_id))

        if mandate.status is not MandateStatus.ACTIVE:
            raise ConflictError(
                "mandate_revoked",
                f"mandate {mandate.id} has been revoked and authorizes nothing",
                resource="mandate",
                identifier=str(mandate.id),
            )

        if (
            await self._constraints.get_for_mandate(mandate.id, merchant_id=mandate.merchant_id)
            is not None
        ):
            # Terminal, like every other authorization transition here. A second set would
            # mean the terms of an authorization could be chosen after the fact.
            raise ConflictError(
                "constraints_already_exist",
                f"mandate {mandate.id} is already qualified by a constraint set",
                resource="mandate",
                identifier=str(mandate.id),
            )

        for constraint in request.financial_constraints():
            _check_against_mandate(constraint, mandate)

        specs = request.semantic_specs()
        async with translated_conflicts(self._session, identifier=str(mandate.id)):
            # The read above answers first and answers better, naming the mandate. This
            # answers when two creations pass that read at once, which the unique constraint
            # on `mandate_id` is what actually prevents. Both produce the same refusal, so a
            # caller cannot tell whether it lost a race.
            constraint_set = await self._constraints.create(
                merchant_id=request.merchant_id,
                mandate_id=request.mandate_id,
                specs=specs,
            )
        await self._audit.append(
            merchant_id=constraint_set.merchant_id,
            actor_type=CONSTRAINTS_ACTOR,
            credential_id=credential_id,
            event_type=CONSTRAINTS_CREATED,
            resource_type=CONSTRAINTS_RESOURCE,
            resource_id=constraint_set.id,
            payload=_created_payload(constraint_set, specs, request),
        )
        await self._session.commit()
        return constraint_set

    async def get_constraints(
        self, mandate_id: uuid.UUID, *, merchant_id: uuid.UUID
    ) -> IntentConstraintSet:
        """Fetch one merchant's constraint set for a mandate, raising rather than returning
        None.

        Absence is not satisfaction. A mandate with no constraint set has no semantic
        authorization at all, and answering "nothing was required, so everything passes"
        would be the single most dangerous default in this system.

        Another merchant's constraint set is absent for the same reason a missing one is, and
        produces the same error. What a buyer required of a purchase is as private as the
        mandate it qualifies.
        """
        constraint_set = await self._constraints.get_for_mandate(
            mandate_id, merchant_id=merchant_id
        )
        if constraint_set is None:
            raise NotFoundError("intent_constraints", str(mandate_id))
        return constraint_set


def _check_against_mandate(
    constraint: MaxTotalAmount | MaxQuantity, mandate: SpendingMandate
) -> None:
    """Refuse a mandate that permits more than the buyer said they wanted to spend.

    The stated financial limit is not stored, so the mandate has to be at least as strict
    as it. If the buyer said 5000 rupees and the mandate permits 10000, enforcing only the
    mandate would silently authorize twice what was asked for, and this is the one place
    that can be noticed.

    The opposite case is fine and is not refused. A mandate stricter than the stated limit
    denies more, never less.
    """
    if isinstance(constraint, MaxTotalAmount):
        if constraint.currency != mandate.currency:
            raise ConflictError(
                "mandate_currency_mismatch",
                f"the stated limit is in {constraint.currency}"
                f" and the mandate authorizes {mandate.currency}",
                resource="mandate",
                identifier=str(mandate.id),
            )
        if mandate.max_total_amount_minor > constraint.amount_minor:
            raise ConflictError(
                "mandate_exceeds_intent_limit",
                f"the mandate authorizes {mandate.max_total_amount_minor}"
                f" {mandate.currency} and the buyer stated a limit of"
                f" {constraint.amount_minor} {constraint.currency}",
                resource="mandate",
                identifier=str(mandate.id),
            )
        return

    # A mandate with no quantity ceiling places no limit at all, which is looser than any
    # stated one rather than equal to it.
    if mandate.max_quantity is None or mandate.max_quantity > constraint.quantity:
        raise ConflictError(
            "mandate_exceeds_intent_limit",
            f"the mandate authorizes {mandate.max_quantity} units"
            f" and the buyer stated a limit of {constraint.quantity}",
            resource="mandate",
            identifier=str(mandate.id),
        )


def _target_key(spec: IntentConstraintSpec) -> str | None:
    """What two constraints have to share to be rules about the same thing.

    Normalized, because the evaluator will normalize too: `Color` and `color` are one
    attribute, and letting both through would produce two rules the database then refuses
    with an integrity error instead of a message.
    """
    return None if spec.attribute_key is None else normalize_text(spec.attribute_key)


def _created_payload(
    constraint_set: IntentConstraintSet,
    specs: Sequence[IntentConstraintSpec],
    request: NewIntentConstraints,
) -> dict[str, Any]:
    """What was required, in the words of the constraints themselves.

    The stored rules are summarized so that the trail answers what a purchase had to
    satisfy without joining to the tables. The financial constraints are counted rather
    than copied: they were checked against the mandate and deliberately not stored, and
    writing their values here would put a number in the log that looks like a ceiling and
    is not one.
    """
    return {
        "mandate_id": str(constraint_set.mandate_id),
        "constraint_count": len(specs),
        "constraints": [spec.to_summary() for spec in specs],
        "financial_constraints_checked": len(request.financial_constraints()),
    }
