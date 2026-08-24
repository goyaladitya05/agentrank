#!/usr/bin/env python3
"""Create one disposable merchant that can be re-evaluated, for local browser tests.

The re-evaluation workflow needs more than a compiler run. A launch freezes a representation, a
suite, a benchmark world and a buyer, and refuses if any of them is missing, so a merchant the
browser test can drive has to carry all of them.

Everything is derived from the authored VoltEdge documents rather than invented here. The two
world files and the source document are read as JSON, given this merchant's slug, and written to
a directory of their own; the suite is trimmed to two missions, one purchasable and one control,
because a browser test that waits for fourteen real missions is a browser test nobody runs.

A new merchant per invocation, because the workflow publishes an immutable representation and
executes real benchmark runs, and a second pass over an already published one would have nothing
left to do.

Two lines on standard output: the merchant API key, then the path of the authored world the
operator dispatcher is to be pointed at.
"""

import asyncio
import json
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

WORLD = Path("benchmarks/voltedge")

# One purchasable mission and one control. Two is the smallest suite that still exercises both
# rates a comparison reports, and it keeps a real browser run to a few seconds.
MISSIONS_PER_OUTCOME = 1


def _document(name: str) -> dict[str, Any]:
    with (WORLD / name).open(encoding="utf-8") as handle:
        loaded: dict[str, Any] = json.load(handle)
        return loaded


def _catalog(slug: str) -> dict[str, Any]:
    document = _document("catalog.json")
    document["key"] = f"{slug}-catalog"
    document["version"] = 1
    document["merchant_slug"] = slug
    document["merchant_name"] = "Re-evaluation E2E"
    return document


def _suite(slug: str) -> dict[str, Any]:
    document = _document("suite.json")
    document["key"] = f"{slug}-core"
    document["version"] = 1
    document["merchant_slug"] = slug
    document["name"] = "Re-evaluation E2E suite"
    chosen: list[dict[str, Any]] = []
    for outcome in ("PURCHASE_AVAILABLE", "NO_ACCEPTABLE_PURCHASE"):
        matching = [
            mission
            for mission in document["missions"]
            if mission["oracle"]["expected_outcome"] == outcome
        ]
        chosen.extend(matching[:MISSIONS_PER_OUTCOME])
    document["missions"] = chosen
    return document


def _source(slug: str) -> dict[str, Any]:
    document = _document("source.json")
    document["key"] = f"{slug}-source"
    document["version"] = 1
    document["merchant_slug"] = slug
    return document


def write_world(slug: str) -> Path:
    """The authored documents this merchant's world is read from, in a directory of their own."""
    directory = Path(tempfile.mkdtemp(prefix=f"{slug}-"))
    for name, document in (
        ("catalog.json", _catalog(slug)),
        ("suite.json", _suite(slug)),
        ("source.json", _source(slug)),
    ):
        with (directory / name).open("w", encoding="utf-8") as handle:
            json.dump(document, handle)
    return directory


async def main() -> None:
    settings = get_settings()
    engine = create_engine(settings)
    slug = f"reeval-e2e-{int(time.time())}"
    directory = write_world(slug)
    try:
        async with create_session_factory(engine)() as session:
            await publish_world(session, read_world(directory))
            merchant = await MerchantRepository(session).get_by_slug(slug)
            assert merchant is not None  # publishing the world creates it
            snapshot = await MerchantRepresentationService(session).publish_source(
                read_source(directory / "source.json")
            )
            await MerchantCompilerService(session).run(merchant.id, snapshot.id)
            issued = await MerchantCredentialService(session).issue(
                merchant_id=merchant.id,
                label="playwright re-evaluation",
                marker=TokenMarker.of(settings.environment),
            )
            print(issued.token)
            print(directory)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
