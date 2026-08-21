"""Persistence access for authoritative intent constraints.

Create and read. There is deliberately no update and no delete: an authorization that can
be edited is not an authorization, and the database refuses both through a trigger, so
this is a contract rather than a convention.

The repository owns SQLAlchemy and does not commit. The caller sets the transaction
boundary, which is what lets a constraint set, its constraints and its audit event be one
unit of work.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from agentrank_api.constraints.models import IntentConstraint, IntentConstraintSet
from agentrank_api.constraints.rules import IntentConstraintSpec


class IntentConstraintRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        merchant_id: uuid.UUID,
        mandate_id: uuid.UUID,
        specs: Sequence[IntentConstraintSpec],
    ) -> IntentConstraintSet:
        """Write a constraint set and its constraints, and flush.

        A set is never written empty. An authorization that requires nothing is not a
        weaker authorization, it is the absence of one, and storing it would let a
        checkout report as semantically satisfied because nothing was ever asked.
        """
        if not specs:
            raise ValueError("a constraint set must hold at least one constraint")

        constraint_set = IntentConstraintSet(merchant_id=merchant_id, mandate_id=mandate_id)
        constraint_set.constraints = [
            IntentConstraint(
                merchant_id=merchant_id,
                kind=spec.kind,
                attribute_key=spec.attribute_key,
                operator=spec.operator,
                value=spec.to_stored_value(),
            )
            for spec in specs
        ]
        self._session.add(constraint_set)
        await self._session.flush()
        return constraint_set

    async def get_for_mandate(
        self, mandate_id: uuid.UUID, *, merchant_id: uuid.UUID
    ) -> IntentConstraintSet | None:
        """Fetch one merchant's constraint set for one mandate, with every constraint loaded.

        Loaded eagerly and completely. Evaluation checks every constraint, and a
        collection that was loaded lazily or partially would make a decision depend on how
        the object happened to be fetched. `lazy="raise_on_sql"` means an unloaded
        collection raises rather than quietly evaluating against nothing.

        The lookup is by mandate rather than by identifier, because that is the only
        binding that exists: a caller cannot choose which constraints apply to a mandate.

        Merchant scoped, and the merchant is required rather than optional even though the
        composite foreign key already ties a set to its mandate's merchant. Two kinds of caller
        reach this: an authenticated request, where the merchant is the credential's and the
        scope is doing real work, and the evaluation paths, where it comes from a checkout that
        was itself found by a merchant scoped read. Requiring it in both is what makes an
        unscoped read impossible to write rather than merely unusual.
        """
        statement = (
            select(IntentConstraintSet)
            .options(selectinload(IntentConstraintSet.constraints))
            .where(
                IntentConstraintSet.mandate_id == mandate_id,
                IntentConstraintSet.merchant_id == merchant_id,
            )
        )
        return (await self._session.execute(statement)).scalar_one_or_none()
