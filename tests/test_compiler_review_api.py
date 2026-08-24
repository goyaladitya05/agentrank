"""Merchant compiler HTTP review commands preserve the domain's immutable evidence."""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import CredentialIssuer, bearer
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentrank_api.commerce.models import Merchant
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.compiler.definitions import CandidateProposal
from agentrank_api.compiler.models import CompilerCandidate, CompilerRun, ReviewDecision
from agentrank_api.compiler.service import MerchantCompilerService
from agentrank_api.compiler.views import MerchantCompilerReviewService
from agentrank_api.config import Settings
from agentrank_api.errors import ConflictError
from agentrank_api.main import create_app
from agentrank_api.payments.fake import FakePaymentProvider
from agentrank_api.representation.definitions import (
    AttributeKind,
    FactAuthority,
    FactConfidence,
    ReviewState,
    SemanticFact,
    SourceReference,
)
from agentrank_api.representation.fixtures import read_source
from agentrank_api.representation.service import MerchantRepresentationService

pytestmark = pytest.mark.anyio


def client(settings: Settings, sessions: async_sessionmaker[AsyncSession]) -> TestClient:
    app = create_app(settings, payment_provider=FakePaymentProvider())
    app.state.session_factory = sessions
    return TestClient(app)


async def reviewable_run(
    session: AsyncSession,
) -> tuple[Merchant, CompilerRun, list[CompilerCandidate]]:
    source = read_source(Path("benchmarks/voltedge/source.json"))
    merchant = await MerchantRepository(session).create(slug="review-shop", name="Review Shop")
    await session.commit()
    changed = replace(
        source,
        merchant_slug=merchant.slug,
        version=2,
        products=(
            replace(
                source.products[0], description="Explicitly supports 65W, unlike its 100W title."
            ),
            *source.products[1:],
        ),
    )
    snapshot = await MerchantRepresentationService(session).publish_source(changed)
    compiler = MerchantCompilerService(session)
    run = await compiler.run(merchant.id, snapshot.id)
    return merchant, run, await compiler.candidates(merchant.id, run.id)


async def test_compiler_review_api_scopes_reads_reviews_and_publication(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    merchant, run, candidates = await reviewable_run(session)
    token = await issue_credential(merchant.id)
    other = await MerchantRepository(session).create(slug="foreign-review-shop", name="Foreign")
    await session.commit()
    foreign = await issue_credential(other.id)
    http = client(settings, factory)

    overview = http.get("/api/v1/compiler/overview", headers=bearer(token))
    assert overview.status_code == 200
    assert overview.json()["review_required_count"] == 2
    assert http.get(f"/api/v1/compiler/runs/{run.id}", headers=bearer(foreign)).status_code == 404

    watts = [
        candidate
        for candidate in candidates
        if candidate.target.endswith(".attribute.wattage")
        and candidate.state.value == "REVIEW_REQUIRED"
    ]
    assert len(watts) == 2
    foreign_write = http.post(
        f"/api/v1/compiler/candidates/{watts[0].id}/correct",
        headers=bearer(foreign),
        json={
            "value": 65,
            "provenance_field": "products[VE-CHG-100].description",
            "provenance_excerpt": "65W",
        },
    )
    assert foreign_write.status_code == 404

    for candidate in watts:
        response = http.post(
            f"/api/v1/compiler/candidates/{candidate.id}/correct",
            headers=bearer(token),
            json={
                "value": 65,
                "provenance_field": "products[VE-CHG-100].description",
                "provenance_excerpt": "65W",
            },
        )
        assert response.status_code == 200
        assert (
            next(
                item
                for item in response.json()["candidates"]
                if item["candidate_id"] == str(candidate.id)
            )["review"]["decision"]
            == "CORRECT"
        )

    duplicate = http.post(
        f"/api/v1/compiler/candidates/{watts[0].id}/correct",
        headers=bearer(token),
        json={
            "value": 65,
            "provenance_field": "products[VE-CHG-100].description",
            "provenance_excerpt": "65W",
        },
    )
    assert duplicate.status_code == 200

    invalid = http.post(
        f"/api/v1/compiler/candidates/{watts[0].id}/correct",
        headers=bearer(token),
        json={
            "value": "wrong",
            "provenance_field": "products[VE-CHG-100].description",
            "provenance_excerpt": "65W",
        },
    )
    assert invalid.status_code == 422

    published = http.post(f"/api/v1/compiler/runs/{run.id}/publish", headers=bearer(token))
    assert published.status_code == 200
    representation_id = published.json()["readiness"]["published_representation_id"]
    assert representation_id is not None
    repeated = http.post(f"/api/v1/compiler/runs/{run.id}/publish", headers=bearer(token))
    assert repeated.status_code == 200
    assert repeated.json()["readiness"]["published_representation_id"] == representation_id


async def test_compiler_review_api_rejects_bad_corrections_and_unresolved_publish(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    merchant, run, candidates = await reviewable_run(session)
    token = await issue_credential(merchant.id)
    candidate = next(item for item in candidates if item.target.endswith(".attribute.wattage"))
    http = client(settings, factory)

    unresolved = http.post(f"/api/v1/compiler/runs/{run.id}/publish", headers=bearer(token))
    assert unresolved.status_code == 409
    malformed = http.post(
        f"/api/v1/compiler/candidates/{candidate.id}/correct",
        headers=bearer(token),
        json={
            "value": 65,
            "provenance_field": "products[VE-CHG-100].description",
            "provenance_excerpt": "not in source",
            "authority": "AUTHORITATIVE",
        },
    )
    assert malformed.status_code == 422


async def test_competing_corrections_are_serialized_by_postgresql(
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant, _, candidates = await reviewable_run(session)
    candidate = next(
        item
        for item in candidates
        if item.target.endswith(".attribute.wattage") and item.state.value == "REVIEW_REQUIRED"
    )

    def correction(value: int, excerpt: str) -> CandidateProposal:
        return CandidateProposal(
            target=candidate.target,
            fact=SemanticFact(
                value=value,
                authority=FactAuthority.DERIVED,
                confidence=FactConfidence.HIGH,
                review_state=ReviewState.CONFIRMED,
                provenance=(SourceReference("products[VE-CHG-100].description", excerpt),),
            ),
            attribute_kind=AttributeKind.MEASUREMENT,
            unit="W",
        )

    async def submit(proposal: CandidateProposal) -> str:
        async with factory() as competing_session:
            try:
                review = await MerchantCompilerService(competing_session).review(
                    merchant.id, candidate.id, ReviewDecision.CORRECT, correction=proposal
                )
            except ConflictError:
                return "conflict"
            return str(review.id)

    first, second = await asyncio.gather(
        submit(correction(65, "65W")), submit(correction(100, "100W"))
    )
    assert sorted([first == "conflict", second == "conflict"]) == [False, True]
    async with factory() as verify_session:
        reviews = await MerchantCompilerReviewService(verify_session)._reviews_for_runs(
            merchant.id, [candidate.run_id]
        )
    assert candidate.id in reviews


async def test_competing_publications_are_idempotent_in_postgresql(
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant, run, candidates = await reviewable_run(session)
    compiler = MerchantCompilerService(session)
    for candidate in candidates:
        if candidate.state.value != "REVIEW_REQUIRED":
            continue
        correction = CandidateProposal(
            target=candidate.target,
            fact=SemanticFact(
                value=65,
                authority=FactAuthority.DERIVED,
                confidence=FactConfidence.HIGH,
                review_state=ReviewState.CONFIRMED,
                provenance=(SourceReference("products[VE-CHG-100].description", "65W"),),
            ),
            attribute_kind=AttributeKind.MEASUREMENT,
            unit="W",
        )
        await compiler.review(
            merchant.id, candidate.id, ReviewDecision.CORRECT, correction=correction
        )

    async def publish() -> str:
        async with factory() as competing_session:
            representation = await MerchantCompilerService(competing_session).publish(
                merchant.id, run.id
            )
            return str(representation.id)

    first, second = await asyncio.gather(publish(), publish())
    assert first == second


async def test_compiler_review_and_publication_leave_financial_state_unchanged(
    session: AsyncSession,
) -> None:
    merchant, run, candidates = await reviewable_run(session)
    statements = {
        "checkout_session": "SELECT count(*) FROM checkout_session",
        "checkout_line": "SELECT count(*) FROM checkout_line",
        "payment_attempt": "SELECT count(*) FROM payment_attempt",
        "inventory_reservation": "SELECT count(*) FROM inventory_reservation",
        "inventory_reservation_line": "SELECT count(*) FROM inventory_reservation_line",
        "spending_mandate": "SELECT count(*) FROM spending_mandate",
        "razorpay_checkout": "SELECT count(*) FROM razorpay_checkout",
    }

    async def counts() -> dict[str, int]:
        return {
            table: int((await session.execute(text(statement))).scalar_one())
            for table, statement in statements.items()
        }

    before = await counts()
    compiler = MerchantCompilerService(session)
    for candidate in candidates:
        if candidate.state.value != "REVIEW_REQUIRED":
            continue
        await compiler.review(
            merchant.id,
            candidate.id,
            ReviewDecision.CORRECT,
            correction=CandidateProposal(
                target=candidate.target,
                fact=SemanticFact(
                    value=65,
                    authority=FactAuthority.DERIVED,
                    confidence=FactConfidence.HIGH,
                    review_state=ReviewState.CONFIRMED,
                    provenance=(SourceReference("products[VE-CHG-100].description", "65W"),),
                ),
                attribute_kind=AttributeKind.MEASUREMENT,
                unit="W",
            ),
        )
    await compiler.publish(merchant.id, run.id)
    assert await counts() == before
