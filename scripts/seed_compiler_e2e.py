#!/usr/bin/env python3
"""Create one disposable merchant compiler review workflow for local browser tests.

The seeded source is chosen so that one compiler run contains every decision the merchant
workflow supports. Its charger states two different wattages, which the compiler refuses to
guess and marks as needing a correction; its cable claims USB-PD, which the compiler can read
but cannot confirm, so that fact needs an accept or a reject. One run, four pending facts, and
the browser test exercises correct, accept and reject against the same publication.

A new merchant per invocation, because the workflow ends in an immutable publication and a
second run against a published one would have nothing left to do.
"""

import asyncio
import time
from dataclasses import replace
from pathlib import Path

from agentrank_api.auth.service import MerchantCredentialService
from agentrank_api.auth.tokens import TokenMarker
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.compiler.service import MerchantCompilerService
from agentrank_api.config import get_settings
from agentrank_api.database import create_engine, create_session_factory
from agentrank_api.registry import Base  # noqa: F401  registers every table
from agentrank_api.representation.definitions import MerchantSourceDefinition
from agentrank_api.representation.fixtures import read_source
from agentrank_api.representation.service import MerchantRepresentationService

SOURCE_PATH = Path("benchmarks/voltedge/source.json")


def reviewable_source(merchant_slug: str) -> MerchantSourceDefinition:
    """VoltEdge with one contradicted measurement and one unconfirmed compatibility claim."""
    source = read_source(SOURCE_PATH)
    charger, cable = source.products[0], source.products[1]
    return replace(
        source,
        merchant_slug=merchant_slug,
        version=1,
        products=(
            replace(charger, description="Explicitly supports 65W, unlike its 100W title."),
            replace(cable, description=f"{cable.description} Supports USB-PD."),
            *source.products[2:],
        ),
    )


async def main() -> None:
    settings = get_settings()
    engine = create_engine(settings)
    try:
        async with create_session_factory(engine)() as session:
            slug = f"compiler-e2e-{int(time.time())}"
            merchant = await MerchantRepository(session).create(slug=slug, name="Compiler E2E")
            await session.commit()
            snapshot = await MerchantRepresentationService(session).publish_source(
                reviewable_source(slug)
            )
            await MerchantCompilerService(session).run(merchant.id, snapshot.id)
            issued = await MerchantCredentialService(session).issue(
                merchant_id=merchant.id,
                label="playwright",
                marker=TokenMarker.of(settings.environment),
            )
            print(issued.token)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
