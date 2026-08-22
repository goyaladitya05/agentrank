#!/usr/bin/env python3
"""Create or refresh the VoltEdge catalog and publish the benchmark suite authored against it.

Run through `make seed-benchmark`. Convergent: running it twice changes nothing, and it puts the
catalog back to exactly what the fixture describes, which is what makes a benchmark run
reproducible rather than a measurement of whatever the database happened to contain.

Running it after editing the suite or the catalog fixture without bumping that definition's
version is refused, which is the point of the versions.

The world is read from `benchmarks/voltedge` rather than imported. The authored files are
operator side and are deliberately outside the package a benchmark worker runs from, because a
mission's expected outcome is the answer key. See `agentrank_api.benchmark.authored`.
"""

import asyncio
from pathlib import Path

from agentrank_api.benchmark.authored import publish_world, read_world
from agentrank_api.config import get_settings
from agentrank_api.database import create_engine, create_session_factory
from agentrank_api.registry import Base  # noqa: F401  registers every table

WORLD = Path(__file__).resolve().parent.parent / "benchmarks" / "voltedge"


async def main() -> None:
    settings = get_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            prepared, suite = await publish_world(session, read_world(WORLD))
    finally:
        await engine.dispose()

    environment, summary = prepared.environment, prepared.catalog
    print(
        f"prepared benchmark world {environment.label} as {environment.fixture_hash}"
        f" for merchant {summary.merchant_id}"
    )
    print(
        f"seeded {summary.products} products and {summary.variants} variants"
        f" ({summary.created} rows created, {prepared.released_holds} holds released)"
    )
    print(f"published benchmark suite {suite.label} as {suite.definition_hash}")


if __name__ == "__main__":
    asyncio.run(main())
