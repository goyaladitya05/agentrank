#!/usr/bin/env python3
"""Create one disposable merchant for the Buildathon browser workflow.

The workflow this seeds is the whole merchant story on one screen after another: an
evaluation whose failures include one a merchant can actually fix, an issues page that says
so, a batch of proposed fixes to review, a publication, and a re-evaluation read against the
first run. Deterministic reference execution, no model provider anywhere.

Three authored deviations from the plain VoltEdge world make that story true:

- the world catalog omits the wattage attribute on both 100W charger variants, so the one
  mission that requires wattage at least 100 fails with the merchant's own data as the
  evidence. That is the merchant-fixable failure the issues page exists for
- the suite is trimmed to three missions: the wattage specification purchase that fails, one
  ordinary purchase that succeeds, and one control that correctly declines
- the source document carries the compiler seed's two ambiguities, a contradicted wattage
  and an unconfirmed USB-PD claim, so the fixes page has real decisions waiting

A new merchant per invocation, because the workflow publishes an immutable representation
and executes real benchmark runs.

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

# The mission that must fail for a merchant-fixable reason, and the attribute it requires.
SPECIFICATION_MISSION = "black-100w-charger"
OMITTED_ATTRIBUTE = "wattage"
OMITTED_FROM_PRODUCT = "VE-CHG-100"


def _document(name: str) -> dict[str, Any]:
    with (WORLD / name).open(encoding="utf-8") as handle:
        loaded: dict[str, Any] = json.load(handle)
        return loaded


def _catalog(slug: str) -> dict[str, Any]:
    """VoltEdge, with the wattage the specification mission needs left unpublished."""
    document = _document("catalog.json")
    document["key"] = f"{slug}-catalog"
    document["version"] = 1
    document["merchant_slug"] = slug
    document["merchant_name"] = "Buildathon E2E"
    for product in document["products"]:
        if product["external_id"] != OMITTED_FROM_PRODUCT:
            continue
        for variant in product["variants"]:
            variant["attributes"].pop(OMITTED_ATTRIBUTE, None)
    return document


def _suite(slug: str) -> dict[str, Any]:
    """Three missions: the merchant-fixable failure, one success, one correct decline."""
    document = _document("suite.json")
    document["key"] = f"{slug}-core"
    document["version"] = 1
    document["merchant_slug"] = slug
    document["name"] = "Buildathon E2E suite"
    missions = document["missions"]
    specification = next(
        mission for mission in missions if mission["brief"]["key"] == SPECIFICATION_MISSION
    )
    plain_purchase = next(
        mission
        for mission in missions
        if mission["oracle"]["expected_outcome"] == "PURCHASE_AVAILABLE"
        and not any(
            constraint.get("name") == OMITTED_ATTRIBUTE
            for constraint in mission["brief"]["hard_constraints"]
        )
    )
    control = next(
        mission
        for mission in missions
        if mission["oracle"]["expected_outcome"] == "NO_ACCEPTABLE_PURCHASE"
    )
    document["missions"] = [specification, plain_purchase, control]
    return document


def _source(slug: str) -> dict[str, Any]:
    """VoltEdge source with the compiler seed's two reviewable ambiguities."""
    document = _document("source.json")
    document["key"] = f"{slug}-source"
    document["version"] = 1
    document["merchant_slug"] = slug
    charger, cable = document["products"][0], document["products"][1]
    charger["description"] = "Explicitly supports 65W, unlike its 100W title."
    cable["description"] = f"{cable['description']} Supports USB-PD."
    return document


def write_world(slug: str) -> Path:
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
    slug = f"buildathon-e2e-{int(time.time())}"
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
                label="playwright buildathon",
                marker=TokenMarker.of(settings.environment),
            )
            print(issued.token)
            print(directory)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
