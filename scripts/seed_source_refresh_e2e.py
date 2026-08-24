#!/usr/bin/env python3
"""Create one disposable merchant whose compiler work is already settled, for browser tests.

The workflow Phase 4E exists for only starts from a dead end. A merchant who has reviewed and
published has no way to change what AgentRank publishes about their catalog except by supplying
newer source evidence, so a browser test of that path has to begin from a merchant who is
genuinely finished: a source snapshot, a completed compiler run over it with nothing left to
answer, and one immutable published representation.

The authored VoltEdge source is used as it is, because it compiles with no review-required fact
and can therefore be published without a browser touching it. The contradiction the browser test
needs is introduced by the browser, in the document the merchant edits, which is the point.

Everything here is synthetic. The merchant is created for this run, the credential is generated
for it, and no real merchant data and no provider secret is written into anything a test retains.

Two lines on standard output: the merchant API key, then the identifier of the representation
that must still be exactly what it was when the workflow finishes.
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
from agentrank_api.representation.fixtures import read_source
from agentrank_api.representation.service import MerchantRepresentationService

SOURCE_PATH = Path("benchmarks/voltedge/source.json")


async def main() -> None:
    settings = get_settings()
    engine = create_engine(settings)
    try:
        async with create_session_factory(engine)() as session:
            slug = f"source-e2e-{int(time.time())}"
            merchant = await MerchantRepository(session).create(slug=slug, name="Source E2E")
            await session.commit()
            snapshot = await MerchantRepresentationService(session).publish_source(
                replace(read_source(SOURCE_PATH), merchant_slug=slug, version=1)
            )
            compiler = MerchantCompilerService(session)
            run = await compiler.run(merchant.id, snapshot.id)
            representation = await compiler.publish(merchant.id, run.id)
            issued = await MerchantCredentialService(session).issue(
                merchant_id=merchant.id,
                label="playwright source refresh",
                marker=TokenMarker.of(settings.environment),
            )
            print(issued.token)
            print(representation.id)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
