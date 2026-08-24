#!/usr/bin/env python3
"""Create one disposable merchant compiler review workflow for local browser tests."""

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
from agentrank_api.representation.fixtures import read_source
from agentrank_api.representation.service import MerchantRepresentationService


async def main() -> None:
    settings = get_settings()
    engine = create_engine(settings)
    try:
        async with create_session_factory(engine)() as session:
            slug = f"compiler-e2e-{int(time.time())}"
            merchant = await MerchantRepository(session).create(slug=slug, name="Compiler E2E")
            await session.commit()
            source = read_source(Path("benchmarks/voltedge/source.json"))
            fixture = replace(
                source,
                merchant_slug=slug,
                version=1,
                products=(
                    replace(
                        source.products[0],
                        description="Explicitly supports 65W, unlike its 100W title.",
                    ),
                    *source.products[1:],
                ),
            )
            snapshot = await MerchantRepresentationService(session).publish_source(fixture)
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
