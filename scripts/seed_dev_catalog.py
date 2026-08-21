#!/usr/bin/env python3
"""Create or refresh the local development catalog.

Run with `make seed-dev`. Safe to run repeatedly: the second run reports nothing created.
This is never invoked by the application itself.
"""

import asyncio

from agentrank_api.commerce.dev_catalog import seed_dev_catalog
from agentrank_api.config import get_settings
from agentrank_api.database import create_engine, create_session_factory


async def main() -> None:
    engine = create_engine(get_settings())
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            summary = await seed_dev_catalog(session)
            await session.commit()
    finally:
        await engine.dispose()

    print(f"merchant   {summary.merchant_id}")
    print(f"products   {summary.products}")
    print(f"variants   {summary.variants}")
    print(f"created    {summary.created} rows this run")


if __name__ == "__main__":
    asyncio.run(main())
