"""Compiler review worlds built from the real VoltEdge source through the real services.

Two worlds, because the compiler produces two genuinely different review-required candidates and
a test that only ever sees one of them proves half the workflow.

A conflicting wattage is a measurement the compiler refuses to guess: it carries
`requires_correction`, so accept is refused and only a typed correction resolves it. An explicit
USB-PD claim is a compatibility fact the compiler can read but not confirm: it requires review
without requiring correction, so accept and reject are both ordinary answers.
"""

import uuid
from dataclasses import replace
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.commerce.models import Merchant
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.compiler.definitions import CandidateProposal
from agentrank_api.compiler.models import (
    CandidateState,
    CompilerCandidate,
    CompilerRun,
    ReviewDecision,
)
from agentrank_api.compiler.service import MerchantCompilerService
from agentrank_api.representation.definitions import (
    AttributeKind,
    FactAuthority,
    FactConfidence,
    MerchantSourceDefinition,
    ReviewState,
    SemanticFact,
    SourceReference,
)
from agentrank_api.representation.fixtures import read_source
from agentrank_api.representation.service import MerchantRepresentationService

SOURCE_PATH = Path("benchmarks/voltedge/source.json")

# The field and excerpt the corrected wattage cites. Both exist in the conflicting source below,
# which is what the domain checks before it accepts a merchant's number.
WATTAGE_FIELD = "products[VE-CHG-100].description"
WATTAGE_EXCERPT = "65W"


def conflicting_wattage_source(merchant_slug: str, version: int = 2) -> MerchantSourceDefinition:
    """A source whose charger states two different wattages, so neither can be chosen."""
    source = read_source(SOURCE_PATH)
    return replace(
        source,
        merchant_slug=merchant_slug,
        version=version,
        products=(
            replace(
                source.products[0], description="Explicitly supports 65W, unlike its 100W title."
            ),
            *source.products[1:],
        ),
    )


def unconfirmed_compatibility_source(
    merchant_slug: str, version: int = 3
) -> MerchantSourceDefinition:
    """A source whose cable claims USB-PD, which the compiler reads but will not confirm."""
    source = read_source(SOURCE_PATH)
    cable = source.products[1]
    return replace(
        source,
        merchant_slug=merchant_slug,
        version=version,
        products=(
            source.products[0],
            replace(cable, description=f"{cable.description} Supports USB-PD."),
            *source.products[2:],
        ),
    )


async def compile_source(
    session: AsyncSession, definition: MerchantSourceDefinition
) -> tuple[CompilerRun, list[CompilerCandidate]]:
    """Snapshot and compile one source, returning the run and its candidates."""
    merchant = await MerchantRepository(session).get_by_slug(definition.merchant_slug)
    assert merchant is not None
    snapshot = await MerchantRepresentationService(session).publish_source(definition)
    compiler = MerchantCompilerService(session)
    run = await compiler.run(merchant.id, snapshot.id)
    return run, await compiler.candidates(merchant.id, run.id)


async def reviewable_run(
    session: AsyncSession, slug: str = "review-shop"
) -> tuple[Merchant, CompilerRun, list[CompilerCandidate]]:
    """A merchant whose compiler run needs two typed wattage corrections."""
    merchant = await MerchantRepository(session).create(slug=slug, name="Review Shop")
    await session.commit()
    run, candidates = await compile_source(session, conflicting_wattage_source(slug))
    return merchant, run, candidates


async def acceptable_run(
    session: AsyncSession, slug: str = "compatibility-shop"
) -> tuple[Merchant, CompilerRun, list[CompilerCandidate]]:
    """A merchant whose compiler run needs two compatibility decisions and no correction."""
    merchant = await MerchantRepository(session).create(slug=slug, name="Compatibility Shop")
    await session.commit()
    run, candidates = await compile_source(session, unconfirmed_compatibility_source(slug))
    return merchant, run, candidates


def pending(candidates: list[CompilerCandidate]) -> list[CompilerCandidate]:
    """Only the candidates a merchant still has to answer for."""
    return [
        candidate for candidate in candidates if candidate.state is CandidateState.REVIEW_REQUIRED
    ]


def wattage_correction(
    target: str, value: int = 65, excerpt: str = WATTAGE_EXCERPT, unit: str = "W"
) -> CandidateProposal:
    """The correction a merchant makes to a conflicting wattage, cited to the source."""
    return CandidateProposal(
        target=target,
        fact=SemanticFact(
            value=value,
            authority=FactAuthority.DERIVED,
            confidence=FactConfidence.HIGH,
            review_state=ReviewState.CONFIRMED,
            provenance=(SourceReference(WATTAGE_FIELD, excerpt),),
        ),
        attribute_kind=AttributeKind.MEASUREMENT,
        unit=unit,
    )


async def resolve_every_correction(
    session: AsyncSession, merchant_id: uuid.UUID, candidates: list[CompilerCandidate]
) -> None:
    """Correct every wattage a run still needs, so the run becomes publishable."""
    service = MerchantCompilerService(session)
    for candidate in pending(candidates):
        await service.review(
            merchant_id,
            candidate.id,
            ReviewDecision.CORRECT,
            correction=wattage_correction(candidate.target),
        )
