#!/usr/bin/env python3
"""Create one merchant whose entire history is a source snapshot, for local browser tests.

This is the state Phase 5C exists for, and the script is deliberately the smallest one in
`scripts/`. There is no authored world directory, no catalog fixture, no benchmark suite, no
compiler run, no published representation and no benchmark run. The merchant has published their
own merchant information and nothing else, which until this phase meant they could not be
evaluated at all without a developer writing two JSON documents for them.

Every other browser seed writes an authored world to a temporary directory and publishes it. The
absence of that here is the point: the workflow under test is that a merchant builds their own
evaluation setup from the console, so anything this script prepared for them would be preparing
away the thing being tested.

The source document is written here rather than adapted from `benchmarks/`, and it is small on
purpose: a browser test that waits for a dozen real missions is a browser test nobody runs. Two
categories, a price spread, one line out of stock and one structured colour, which between them
support a purchase family, a budget abstention and a specification mission.

Two lines on standard output: the merchant API key, then the merchant slug the operator
dispatcher is to be pointed at.
"""

import asyncio
import time

from agentrank_api.auth.service import MerchantCredentialService
from agentrank_api.auth.tokens import TokenMarker
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.config import get_settings
from agentrank_api.database import create_engine, create_session_factory
from agentrank_api.registry import Base  # noqa: F401  registers every table
from agentrank_api.representation.definitions import (
    MerchantSourceDefinition,
    SourceProduct,
    SourceVariant,
)
from agentrank_api.representation.service import MerchantRepresentationService

CURRENCY = "INR"


def variant(
    sku: str, *, label: str, price: int, stock: int, finish: str | None = None
) -> SourceVariant:
    return SourceVariant(
        sku=sku,
        label=label,
        price_amount_minor=price,
        currency=CURRENCY,
        inventory_quantity=stock,
        merchant_metadata={} if finish is None else {"finish": finish},
    )


def document(slug: str) -> MerchantSourceDefinition:
    """One merchant's own words about themselves, with no benchmark field anywhere in it."""
    return MerchantSourceDefinition(
        key="merchant-source",
        version=1,
        merchant_slug=slug,
        products=(
            SourceProduct(
                external_id="CHG-65",
                title="65W Travel Charger",
                description="A two-port 65W USB-C charger for phones and tablets.",
                category="chargers",
                variants=(
                    variant("CHG-65-BLK", label="Black", price=329900, stock=12, finish="black"),
                    variant("CHG-65-WHT", label="White", price=339900, stock=0, finish="white"),
                ),
                merchant_metadata={},
            ),
            SourceProduct(
                external_id="CBL-USBC",
                title="USB-C to USB-C Cable",
                description="A braided USB-C charging cable.",
                category="cables",
                variants=(
                    variant("CBL-USBC-1M", label="1 m", price=69900, stock=40),
                    variant("CBL-USBC-2M", label="2 m", price=89900, stock=25),
                ),
                merchant_metadata={},
            ),
        ),
        policy_text={
            "returns": "Returns are accepted within 30 days of delivery in original packaging."
        },
    )


async def main() -> None:
    settings = get_settings()
    engine = create_engine(settings)
    slug = f"setup-e2e-{int(time.time())}"
    try:
        async with create_session_factory(engine)() as session:
            merchant = await MerchantRepository(session).create(slug=slug, name="Evaluation setup")
            await session.commit()
            await MerchantRepresentationService(session).publish_source(document(slug))
            issued = await MerchantCredentialService(session).issue(
                merchant_id=merchant.id,
                label="playwright evaluation setup",
                marker=TokenMarker.of(settings.environment),
            )
            print(issued.token)
            print(slug)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
