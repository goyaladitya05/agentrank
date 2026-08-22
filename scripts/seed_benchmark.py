#!/usr/bin/env python3
"""Create or refresh the VoltEdge catalog and publish the benchmark suite authored against it.

Run through `make seed-benchmark`. Convergent: running it twice changes nothing, and running it
after editing the catalog updates the rows. Running it after editing the *suite* without bumping
the version is refused, which is the point of the version.
"""

import asyncio

from agentrank_api.benchmark.voltedge import seed_voltedge
from agentrank_api.config import get_settings
from agentrank_api.database import create_engine, create_session_factory
from agentrank_api.registry import Base  # noqa: F401  registers every table


async def main() -> None:
    settings = get_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            summary, suite = await seed_voltedge(session)
    finally:
        await engine.dispose()

    print(
        f"seeded {summary.products} products and {summary.variants} variants"
        f" for merchant {summary.merchant_id} ({summary.created} rows created)"
    )
    print(f"published benchmark suite {suite.label} as {suite.definition_hash}")


if __name__ == "__main__":
    asyncio.run(main())
