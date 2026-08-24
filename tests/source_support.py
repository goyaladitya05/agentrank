"""Merchant source documents as a browser would submit them.

Everything here is derived from the authored VoltEdge source rather than invented, so a document
a test submits is the same shape a merchant's current snapshot already holds and a compiler run
over it produces the candidates the rest of the suite already knows.

The submittable half only. A document a browser sends carries no key, no version and no merchant
slug, because none of the three is the browser's to choose, and a helper that added them would be
a helper testing a request nobody can make.
"""

import uuid
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.commerce.models import Merchant
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.representation.definitions import MerchantSourceDefinition
from agentrank_api.representation.fixtures import read_source
from agentrank_api.representation.models import MerchantSourceSnapshot
from agentrank_api.representation.service import MerchantRepresentationService

SOURCE_PATH = Path("benchmarks/voltedge/source.json")

# A key is eight to sixty four of these characters. Written out rather than generated so that a
# test asserting "the same key twice" is asserting about a value the test itself controls.
FIRST_KEY = "first-submission-key"
SECOND_KEY = "second-submission-key"


def document(definition: MerchantSourceDefinition) -> dict[str, Any]:
    """The submittable half of one source definition: what it says, without its identity."""
    payload = definition.payload()
    return {"products": payload["products"], "policy_text": payload["policy_text"]}


def voltedge_document() -> dict[str, Any]:
    """The authored VoltEdge source, as a submission body."""
    return document(read_source(SOURCE_PATH))


def contradicted_document() -> dict[str, Any]:
    """VoltEdge with a charger that states two wattages, so one fact needs a correction."""
    body = voltedge_document()
    body["products"][0]["description"] = "Explicitly supports 65W, unlike its 100W title."
    return body


def submission(body: dict[str, Any], request_key: str) -> dict[str, Any]:
    """One submission command body: the evidence plus the key that makes a retry the same one."""
    return {**body, "request_key": request_key}


async def merchant_with_source(
    session: AsyncSession, slug: str, *, name: str = "Source Shop"
) -> tuple[Merchant, MerchantSourceSnapshot]:
    """A merchant whose first source snapshot was published by the operator command line."""
    merchant = await MerchantRepository(session).create(slug=slug, name=name)
    await session.commit()
    definition = read_source(SOURCE_PATH)
    snapshot = await MerchantRepresentationService(session).publish_source(
        MerchantSourceDefinition(
            key=definition.key,
            version=1,
            merchant_slug=slug,
            products=definition.products,
            policy_text=definition.policy_text,
        )
    )
    return merchant, snapshot


async def bare_merchant(session: AsyncSession, slug: str) -> Merchant:
    """A merchant that has never published any source at all."""
    merchant = await MerchantRepository(session).create(slug=slug, name="Bare Shop")
    await session.commit()
    return merchant


def table_rows(payload: Any) -> list[dict[str, Any]]:
    """Every snapshot summary in a source overview response, as plain dictionaries."""
    snapshots = payload["snapshots"]
    assert isinstance(snapshots, list)
    return [dict(entry) for entry in snapshots]


def snapshot_ids(payload: Any) -> list[uuid.UUID]:
    return [uuid.UUID(entry["source_snapshot_id"]) for entry in table_rows(payload)]
