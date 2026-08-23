"""Publishing representation artifacts, with identity conflicts normalized for callers."""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.errors import ConflictError, NotFoundError
from agentrank_api.representation.definitions import CommerceIRDefinition, MerchantSourceDefinition
from agentrank_api.representation.models import CommerceRepresentation, MerchantSourceSnapshot
from agentrank_api.representation.repository import (
    CommerceRepresentationRepository,
    MerchantSourceRepository,
)


class MerchantRepresentationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._merchants = MerchantRepository(session)
        self._sources = MerchantSourceRepository(session)
        self._representations = CommerceRepresentationRepository(session)

    async def publish_source(self, definition: MerchantSourceDefinition) -> MerchantSourceSnapshot:
        merchant = await self._merchants.get_by_slug(definition.merchant_slug)
        if merchant is None:
            raise NotFoundError("merchant", definition.merchant_slug)
        existing = await self._sources.get(merchant.id, definition.key, definition.version)
        if existing is not None:
            if existing.content_hash != definition.content_hash:
                raise ConflictError("source_definition_changed", definition.label)
            return existing
        try:
            row = await self._sources.create(merchant, definition)
            await self._session.commit()
            return row
        except IntegrityError as error:
            await self._session.rollback()
            existing = await self._sources.get(merchant.id, definition.key, definition.version)
            if existing is not None and existing.content_hash == definition.content_hash:
                return existing
            raise ConflictError("source_definition_changed", definition.label) from error

    async def publish_ir(self, definition: CommerceIRDefinition) -> CommerceRepresentation:
        source = await self._source(definition)
        try:
            row = await self._representations.create(source=source, definition=definition)
            await self._session.commit()
            return row
        except IntegrityError as error:
            await self._session.rollback()
            raise ConflictError(
                "representation_definition_changed", definition.producer_version
            ) from error

    async def _source(self, definition: CommerceIRDefinition) -> MerchantSourceSnapshot:
        source = (
            await self._session.execute(
                select(MerchantSourceSnapshot).where(
                    MerchantSourceSnapshot.source_key == definition.source_key,
                    MerchantSourceSnapshot.source_version == definition.source_version,
                    MerchantSourceSnapshot.content_hash == definition.source_hash,
                )
            )
        ).scalar_one_or_none()
        if source is None:
            raise NotFoundError(
                "merchant_source_snapshot", f"{definition.source_key}@{definition.source_version}"
            )
        return source
