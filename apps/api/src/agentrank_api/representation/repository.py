"""Repository access for immutable merchant representations."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.commerce.models import Merchant
from agentrank_api.representation.definitions import CommerceIRDefinition, MerchantSourceDefinition
from agentrank_api.representation.models import CommerceRepresentation, MerchantSourceSnapshot


class MerchantSourceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self, merchant: Merchant, definition: MerchantSourceDefinition
    ) -> MerchantSourceSnapshot:
        if merchant.slug != definition.merchant_slug:
            raise ValueError("merchant source cannot be published for another merchant")
        row = MerchantSourceSnapshot(
            merchant_id=merchant.id,
            source_key=definition.key,
            source_version=definition.version,
            content_hash=definition.content_hash,
            payload=definition.payload(),
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get(
        self, merchant_id: uuid.UUID, key: str, version: int
    ) -> MerchantSourceSnapshot | None:
        return (
            await self._session.execute(
                select(MerchantSourceSnapshot).where(
                    MerchantSourceSnapshot.merchant_id == merchant_id,
                    MerchantSourceSnapshot.source_key == key,
                    MerchantSourceSnapshot.source_version == version,
                )
            )
        ).scalar_one_or_none()


class CommerceRepresentationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self, *, source: MerchantSourceSnapshot, definition: CommerceIRDefinition
    ) -> CommerceRepresentation:
        if (source.source_key, source.source_version, source.content_hash) != (
            definition.source_key,
            definition.source_version,
            definition.source_hash,
        ):
            raise ValueError("Commerce IR must name the exact source snapshot it was derived from")
        row = CommerceRepresentation(
            merchant_id=source.merchant_id,
            source_snapshot_id=source.id,
            producer=definition.producer,
            producer_version=definition.producer_version,
            content_hash=definition.content_hash,
            payload=definition.payload(),
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get(
        self, merchant_id: uuid.UUID, representation_id: uuid.UUID
    ) -> CommerceRepresentation | None:
        return (
            await self._session.execute(
                select(CommerceRepresentation).where(
                    CommerceRepresentation.id == representation_id,
                    CommerceRepresentation.merchant_id == merchant_id,
                )
            )
        ).scalar_one_or_none()
