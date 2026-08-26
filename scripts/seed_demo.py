#!/usr/bin/env python3
"""Reset the Nordwatt demo to the state a Buildathon recording starts from.

Nordwatt is a fictional charger merchant whose shop genuinely stocks a 100 W wall charger and a
3 m cable. Their published store information does not say so: the wattage and the length appear
on no product page. That single gap is the whole demo. A shopping agent reading what Nordwatt
publishes cannot tell which charger is the laptop-class one or which cable is long enough, so it
cannot buy either, and two sales are lost to missing information rather than to anything the
agent did wrong.

What this leaves behind, all of it written through the ordinary services:

```text
world              4 shopping scenarios over a catalog that really does stock these products
source@1           what Nordwatt published: no wattage, no length
representation@1   compiled and published from source@1, carrying the same gap
source@2           Nordwatt's corrected pages, stating the wattage and the length in prose
compiler run 2     the recovered facts, waiting for the merchant's review
```

The recording then signs in, reads the result of the first run, reviews the recovered facts,
publishes them as representation@2 and measures again. Only that final run executes live.

Three properties make the before and after honest rather than staged, and the first two are
enforced by the comparison engine rather than by this script:

- both runs measure a published representation, with the same buyer, against the same world and
  the same suite. A first evaluation against the ordinary storefront compared with a
  re-evaluation would trip `REPRESENTATION_DELIVERY_DIFFERS` and be refused outright
- the world never changes between them. Nordwatt's shop stocks exactly what it stocked before,
  so a difference cannot be the shelf moving
- the buyer is a model buyer, and that is a requirement rather than a preference. AgentRank's
  deterministic reference buyer is given no discovery surface at all and reads structured
  commerce fields directly, so a published representation cannot change its behaviour by design.
  A representation is an artifact for agents that read what a merchant publishes, and measuring
  one needs such an agent

A fresh synthetic merchant each time, which is what makes this reproducible. Compiler evidence,
source snapshots and published representations are immutable in this system, enforced by a
database trigger as well as by the services, so a reset cannot overwrite the previous recording's
artifacts and does not try to.

Prints the merchant credential and the world path as JSON. Nothing here contacts a model
provider; the runs are executed separately by the operator dispatcher.
"""

import argparse
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from agentrank_api.auth.service import MerchantCredentialService
from agentrank_api.auth.tokens import TokenMarker
from agentrank_api.benchmark.authored import publish_world, read_world
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.compiler.service import MerchantCompilerService
from agentrank_api.config import get_settings
from agentrank_api.database import create_engine, create_session_factory
from agentrank_api.registry import Base  # noqa: F401  registers every table
from agentrank_api.representation.fixtures import read_source
from agentrank_api.representation.service import MerchantRepresentationService

WORLD = Path("benchmarks/nordwatt")

# The world is what the shop holds. The two source documents are what the merchant published
# about it, before and after they close the gap; neither is ever the answer key.
CATALOG = "catalog.json"
SUITE = "suite.json"
INCOMPLETE = "source-incomplete.json"
CORRECTED = "source-corrected.json"


def _document(name: str, slug: str, key_suffix: str) -> dict[str, Any]:
    """One authored document, rebound to this reset's merchant slug."""
    with (WORLD / name).open(encoding="utf-8") as handle:
        loaded: dict[str, Any] = json.load(handle)
    loaded["merchant_slug"] = slug
    loaded["key"] = f"{slug}-{key_suffix}"
    return loaded


def _write_world(slug: str) -> Path:
    """The authored world for this reset, in a directory of its own.

    `read_world` expects a source document beside the catalog and the suite. The incomplete one
    is the world's provenance record, which is correct: the world is registered before Nordwatt
    has fixed anything.
    """
    directory = Path(tempfile.mkdtemp(prefix=f"{slug}-"))
    for name, suffix in ((CATALOG, "catalog"), (SUITE, "core"), (INCOMPLETE, "source")):
        target = "source.json" if name == INCOMPLETE else name
        with (directory / target).open("w", encoding="utf-8") as handle:
            json.dump(_document(name, slug, suffix), handle)
    return directory


def _write_source(slug: str, name: str, version: int, directory: Path) -> Path:
    """One of Nordwatt's own source documents, at the version this reset publishes it as."""
    document = _document(name, slug, "source")
    document["version"] = version
    path = directory / f"source-v{version}.json"
    with path.open("w", encoding="utf-8") as handle:
        json.dump(document, handle)
    return path


async def main() -> int:
    argparse.ArgumentParser(description="Reset the Nordwatt Buildathon demo.").parse_args()

    settings = get_settings()
    engine = create_engine(settings)
    slug = f"nordwatt-{int(time.time())}"
    directory = _write_world(slug)
    try:
        async with create_session_factory(engine)() as session:
            # The world, and the merchant it belongs to. Publishing the world creates the row.
            await publish_world(session, read_world(directory))
            merchant = await MerchantRepository(session).get_by_slug(slug)
            assert merchant is not None  # publishing the world creates it

            representations = MerchantRepresentationService(session)
            compiler = MerchantCompilerService(session)

            # What Nordwatt published first, and the representation compiled from it. The gap in
            # the source is carried into the representation, which is the whole point.
            incomplete = await representations.publish_source(
                read_source(_write_source(slug, INCOMPLETE, 1, directory))
            )
            first = await compiler.run(merchant.id, incomplete.id)
            published = await compiler.publish(merchant.id, first.id)

            # Nordwatt's corrected pages, compiled and left waiting for the merchant's review.
            corrected = await representations.publish_source(
                read_source(_write_source(slug, CORRECTED, 2, directory))
            )
            second = await compiler.run(merchant.id, corrected.id)

            issued = await MerchantCredentialService(session).issue(
                merchant_id=merchant.id,
                label="buildathon demo",
                marker=TokenMarker.of(settings.environment),
            )

            print(
                json.dumps(
                    {
                        "merchant_slug": slug,
                        "credential": issued.token,
                        "world": str(directory),
                        "published_representation": str(published.id),
                        "review_compiler_run": str(second.id),
                    },
                    indent=2,
                )
            )
            return 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
