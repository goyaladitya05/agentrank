#!/usr/bin/env python3
"""Create one merchant with nothing at all, for the browser import workflow.

The smallest seed in `scripts/`, and smaller than `seed_workspace_e2e.py` by exactly one thing:
that one publishes a source document, and this one does not. A merchant arriving at a private beta
has a store and a credential and nothing inside AgentRank, and the workflow under test is that
their own public pages become their first source snapshot with no operator writing a document for
them.

Anything this script prepared would be preparing away the thing being tested.

Two lines on standard output: the merchant API key, then the merchant slug.
"""

import asyncio
import time

from agentrank_api.auth.service import MerchantCredentialService
from agentrank_api.auth.tokens import TokenMarker
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.config import get_settings
from agentrank_api.database import create_engine, create_session_factory
from agentrank_api.registry import Base  # noqa: F401  registers every table


async def main() -> None:
    settings = get_settings()
    engine = create_engine(settings)
    slug = f"import-e2e-{int(time.time())}"
    try:
        async with create_session_factory(engine)() as session:
            merchant = await MerchantRepository(session).create(slug=slug, name="Import merchant")
            await session.commit()
            issued = await MerchantCredentialService(session).issue(
                merchant_id=merchant.id,
                label="playwright merchant import",
                marker=TokenMarker.of(settings.environment),
            )
            print(issued.token)
            print(slug)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
