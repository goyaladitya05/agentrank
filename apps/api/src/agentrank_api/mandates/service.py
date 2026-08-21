"""Mandate application service.

Workflows live here: create, retrieve, validate and revoke. The service coordinates the
repositories, the domain rules and the audit trail, and it owns the transaction. Routes
call one method and serialize the result.

Two rules shape everything in this module:

- a state change and the audit event that records it commit together or not at all
- there is no update. Changing an authorization means creating a new mandate
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.audit.models import ActorType
from agentrank_api.audit.repository import AuditRepository
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.errors import NotFoundError
from agentrank_api.mandates.intent import BuyerIntent
from agentrank_api.mandates.models import MandateStatus, SpendingMandate
from agentrank_api.mandates.repository import MandateRepository
from agentrank_api.mandates.validation import (
    MandateValidationResult,
    validate_mandate,
    validate_validity_window,
)
from agentrank_api.money import validate_amount_minor, validate_currency

MANDATE_RESOURCE = "spending_mandate"
MANDATE_CREATED = "mandate.created"
MANDATE_REVOKED = "mandate.revoked"

# Every mandate event is attributed to the buyer, because granting and withdrawing
# spending authority is the buyer's act. This names a role, not a verified identity:
# nothing authenticates a caller yet, so an audit event is not evidence of who acted. An
# actor identifier belongs with authentication, and arrives with it.
MANDATE_ACTOR = ActorType.BUYER


@dataclass(frozen=True, slots=True)
class NewMandate:
    """A request to create a mandate, refused before it reaches the database if wrong.

    Every rule here is also a database constraint. Stating them once more in the domain
    is what turns a would be integrity error into a typed refusal that names the field,
    for HTTP callers and service callers alike.

    `intent` is optional context. It is recorded in the creation event and is never read
    when deciding anything.
    """

    merchant_id: uuid.UUID
    max_total_amount_minor: int
    currency: str
    valid_from: datetime
    valid_until: datetime
    max_quantity: int | None = None
    intent: BuyerIntent | None = None

    def __post_init__(self) -> None:
        validate_amount_minor(self.max_total_amount_minor)
        validate_currency(self.currency)
        validate_validity_window(self.valid_from, self.valid_until)
        if self.max_quantity is not None and self.max_quantity <= 0:
            raise ValueError("max_quantity must be positive when it is set")
        if self.intent is not None and self.intent.merchant_id != self.merchant_id:
            raise ValueError("intent and mandate must name the same merchant")


class MandateService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._merchants = MerchantRepository(session)
        self._mandates = MandateRepository(session)
        self._audit = AuditRepository(session)

    async def create_mandate(self, request: NewMandate) -> SpendingMandate:
        """Create a mandate and record that it was created, in one transaction.

        The merchant is looked up first so that an unknown one is a 404 naming the
        merchant, rather than a foreign key violation surfacing as a server error.

        Both writes happen in one transaction and one commit. If the audit append fails
        for any reason, the mandate is not persisted either: an authorization with no
        record of being granted is exactly what the audit trail exists to prevent.
        """
        merchant = await self._merchants.get_by_id(request.merchant_id)
        if merchant is None:
            raise NotFoundError("merchant", str(request.merchant_id))

        mandate = await self._mandates.create(
            merchant_id=request.merchant_id,
            max_total_amount_minor=request.max_total_amount_minor,
            currency=request.currency,
            max_quantity=request.max_quantity,
            valid_from=request.valid_from,
            valid_until=request.valid_until,
        )
        await self._append(mandate, MANDATE_CREATED, _created_payload(mandate, request.intent))
        await self._session.commit()
        return mandate

    async def get_mandate(self, mandate_id: uuid.UUID) -> SpendingMandate:
        """Fetch a mandate, raising rather than returning None.

        A caller naming a mandate has already decided it should exist, and every caller
        turning None into the same error is worse than raising it once.
        """
        mandate = await self._mandates.get(mandate_id)
        if mandate is None:
            raise NotFoundError("mandate", str(mandate_id))
        return mandate

    async def validate_mandate(
        self, mandate_id: uuid.UUID, *, at: datetime | None = None
    ) -> MandateValidationResult:
        """Report whether a mandate is usable, at `at` or at the current time.

        This is the layer allowed to read the clock. The rule underneath it takes the
        instant as an argument and reads nothing, which is what keeps it deterministic.

        Nothing is written. Checking an authorization is a read, and an audit event per
        read would turn the trail into a request log. The event worth recording is a
        refusal at execution time, which is Phase 1C.
        """
        mandate = await self.get_mandate(mandate_id)
        return validate_mandate(mandate, at=at or datetime.now(UTC))

    async def revoke_mandate(self, mandate_id: uuid.UUID) -> SpendingMandate:
        """Revoke a mandate and record it, once.

        Idempotent. Revoking an already revoked mandate returns it unchanged and appends
        nothing, so a retried request cannot produce a second revocation event or move
        the original timestamp. Revocation is terminal; there is no reactivation, and the
        database enforces that too.

        The mandate is read under a row lock rather than plainly, which is what makes that
        idempotence survive two revocations arriving at once: without it both would read an
        active mandate, both would take the transition, and the second would move
        `revoked_at` and append a second event.

        The same lock is what execution preparation takes before treating this mandate as
        authoritative, so the two serialize. Either this finishes first and preparation
        observes a revoked mandate and refuses, or preparation finishes first and this
        waits for it. There is no schedule where a revocation commits and a preparation
        then succeeds on an active reading taken before it.
        """
        mandate = await self._locked(mandate_id)
        if await self._mandates.revoke(mandate):
            await self._append(mandate, MANDATE_REVOKED, {"status": MandateStatus.REVOKED.value})
        # Committed either way. When nothing changed this just closes the read.
        await self._session.commit()
        return mandate

    async def _locked(self, mandate_id: uuid.UUID) -> SpendingMandate:
        """Fetch a mandate held against other transactions, raising rather than returning
        None."""
        mandate = await self._mandates.get_for_update(mandate_id)
        if mandate is None:
            raise NotFoundError("mandate", str(mandate_id))
        return mandate

    async def _append(
        self, mandate: SpendingMandate, event_type: str, payload: dict[str, Any]
    ) -> None:
        await self._audit.append(
            merchant_id=mandate.merchant_id,
            actor_type=MANDATE_ACTOR,
            event_type=event_type,
            resource_type=MANDATE_RESOURCE,
            resource_id=mandate.id,
            payload=payload,
        )


def _created_payload(mandate: SpendingMandate, intent: BuyerIntent | None) -> dict[str, Any]:
    """What was authorized, in the words of the mandate itself.

    The buyer's intent is included when one was supplied, so the trail answers why an
    authorization exists as well as what it permits. It is context, never a rule: nothing
    reads it back to decide anything.
    """
    payload: dict[str, Any] = {
        "max_total_amount_minor": mandate.max_total_amount_minor,
        "currency": mandate.currency,
        "max_quantity": mandate.max_quantity,
        "valid_from": mandate.valid_from.isoformat(),
        "valid_until": mandate.valid_until.isoformat(),
        "status": mandate.status.value,
    }
    if intent is not None:
        payload["intent"] = intent.to_payload()
    return payload
