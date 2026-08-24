"""Merchant compiler HTTP review commands preserve the domain's immutable evidence."""

import asyncio
from typing import Any

import pytest
from compiler_support import (
    WATTAGE_EXCERPT,
    WATTAGE_FIELD,
    acceptable_run,
    pending,
    resolve_every_correction,
    reviewable_run,
    wattage_correction,
)
from conftest import CredentialIssuer, bearer
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.compiler.models import ReviewDecision
from agentrank_api.compiler.service import MerchantCompilerService
from agentrank_api.compiler.views import MerchantCompilerReviewService
from agentrank_api.config import Settings
from agentrank_api.errors import ConflictError
from agentrank_api.main import create_app
from agentrank_api.payments.fake import FakePaymentProvider

pytestmark = pytest.mark.anyio

CORRECTION = {
    "value": 65,
    "provenance_field": WATTAGE_FIELD,
    "provenance_excerpt": WATTAGE_EXCERPT,
}


def client(settings: Settings, sessions: async_sessionmaker[AsyncSession]) -> TestClient:
    app = create_app(settings, payment_provider=FakePaymentProvider())
    app.state.session_factory = sessions
    return TestClient(app)


def candidate_view(payload: dict[str, Any], candidate_id: object) -> dict[str, Any]:
    candidates = payload["candidates"]
    assert isinstance(candidates, list)
    return next(item for item in candidates if item["candidate_id"] == str(candidate_id))


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

    watts = pending(candidates)
    assert len(watts) == 2
    foreign_write = http.post(
        f"/api/v1/compiler/candidates/{watts[0].id}/correct",
        headers=bearer(foreign),
        json=CORRECTION,
    )
    assert foreign_write.status_code == 404

    for candidate in watts:
        response = http.post(
            f"/api/v1/compiler/candidates/{candidate.id}/correct",
            headers=bearer(token),
            json=CORRECTION,
        )
        assert response.status_code == 200
        assert candidate_view(response.json(), candidate.id)["review"]["decision"] == "CORRECT"

    duplicate = http.post(
        f"/api/v1/compiler/candidates/{watts[0].id}/correct",
        headers=bearer(token),
        json=CORRECTION,
    )
    assert duplicate.status_code == 200

    invalid = http.post(
        f"/api/v1/compiler/candidates/{watts[0].id}/correct",
        headers=bearer(token),
        json={**CORRECTION, "value": "wrong"},
    )
    assert invalid.status_code == 422

    conflicting = http.post(
        f"/api/v1/compiler/candidates/{watts[0].id}/correct",
        headers=bearer(token),
        json={**CORRECTION, "value": 100, "provenance_excerpt": "100W"},
    )
    assert conflicting.status_code == 409
    assert conflicting.json()["error"] == "candidate_already_reviewed"

    published = http.post(f"/api/v1/compiler/runs/{run.id}/publish", headers=bearer(token))
    assert published.status_code == 200
    representation_id = published.json()["readiness"]["published_representation_id"]
    assert representation_id is not None
    repeated = http.post(f"/api/v1/compiler/runs/{run.id}/publish", headers=bearer(token))
    assert repeated.status_code == 200
    assert repeated.json()["readiness"]["published_representation_id"] == representation_id

    after_publication = http.post(
        f"/api/v1/compiler/candidates/{watts[0].id}/correct",
        headers=bearer(token),
        json=CORRECTION,
    )
    assert after_publication.status_code == 409
    assert after_publication.json()["error"] == "compiler_run_already_published"


async def test_compiler_review_api_accepts_and_rejects_unconfirmed_compatibility(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    merchant, run, candidates = await acceptable_run(session)
    token = await issue_credential(merchant.id)
    other = await MerchantRepository(session).create(slug="foreign-compat-shop", name="Foreign")
    await session.commit()
    foreign = await issue_credential(other.id)
    http = client(settings, factory)

    unconfirmed = pending(candidates)
    assert len(unconfirmed) == 2
    kept, dropped = unconfirmed

    assert (
        http.post(
            f"/api/v1/compiler/candidates/{kept.id}/accept", headers=bearer(foreign)
        ).status_code
        == 404
    )
    assert (
        http.post(
            f"/api/v1/compiler/candidates/{dropped.id}/reject", headers=bearer(foreign)
        ).status_code
        == 404
    )

    accepted = http.post(f"/api/v1/compiler/candidates/{kept.id}/accept", headers=bearer(token))
    assert accepted.status_code == 200
    assert candidate_view(accepted.json(), kept.id)["review"]["decision"] == "ACCEPT"

    rejected = http.post(f"/api/v1/compiler/candidates/{dropped.id}/reject", headers=bearer(token))
    assert rejected.status_code == 200
    assert candidate_view(rejected.json(), dropped.id)["review"]["decision"] == "REJECT"
    assert rejected.json()["readiness"]["publishable"] is True

    assert (
        http.post(
            f"/api/v1/compiler/candidates/{kept.id}/accept", headers=bearer(token)
        ).status_code
        == 200
    )
    reversal = http.post(f"/api/v1/compiler/candidates/{kept.id}/reject", headers=bearer(token))
    assert reversal.status_code == 409
    assert reversal.json()["error"] == "candidate_already_reviewed"

    published = http.post(f"/api/v1/compiler/runs/{run.id}/publish", headers=bearer(token))
    assert published.status_code == 200
    assert published.json()["readiness"]["published_representation_id"] is not None


async def test_a_rejected_compatibility_fact_never_reaches_the_published_representation(
    session: AsyncSession,
) -> None:
    merchant, run, candidates = await acceptable_run(session)
    service = MerchantCompilerService(session)
    kept, dropped = pending(candidates)
    await service.review(merchant.id, kept.id, ReviewDecision.ACCEPT)
    await service.review(merchant.id, dropped.id, ReviewDecision.REJECT)
    representation = await service.publish(merchant.id, run.id)

    compatibility = {
        variant["sku"]: variant["compatibility"]
        for product in representation.payload["products"]
        for variant in product["variants"]
    }
    kept_sku = kept.target.split(".")[1]
    dropped_sku = dropped.target.split(".")[1]
    assert "usb-c-pd" in compatibility[kept_sku]
    assert "usb-c-pd" not in compatibility[dropped_sku]


async def test_a_browser_cannot_smuggle_ownership_type_or_unit_into_a_correction(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    merchant, _, candidates = await reviewable_run(session)
    token = await issue_credential(merchant.id)
    candidate = pending(candidates)[0]
    http = client(settings, factory)

    for smuggled in (
        {"merchant_id": str(merchant.id)},
        {"unit": "mW"},
        {"attribute_kind": "TEXT"},
        {"authority": "AUTHORITATIVE"},
        {"reviewer": "COMPILER"},
    ):
        response = http.post(
            f"/api/v1/compiler/candidates/{candidate.id}/correct",
            headers=bearer(token),
            json={**CORRECTION, **smuggled},
        )
        assert response.status_code == 422, smuggled

    unsupported = http.post(
        f"/api/v1/compiler/candidates/{candidate.id}/correct",
        headers=bearer(token),
        json={**CORRECTION, "value": 999},
    )
    assert unsupported.status_code == 422

    uncited = http.post(
        f"/api/v1/compiler/candidates/{candidate.id}/correct",
        headers=bearer(token),
        json={**CORRECTION, "provenance_excerpt": "not in source"},
    )
    assert uncited.status_code == 422


async def test_compiler_review_api_rejects_bad_corrections_and_unresolved_publish(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    merchant, run, candidates = await reviewable_run(session)
    token = await issue_credential(merchant.id)
    candidate = pending(candidates)[0]
    http = client(settings, factory)

    unresolved = http.post(f"/api/v1/compiler/runs/{run.id}/publish", headers=bearer(token))
    assert unresolved.status_code == 409
    assert unresolved.json()["error"] == "compiler_review_required"

    blocked = http.get(f"/api/v1/compiler/runs/{run.id}", headers=bearer(token))
    assert blocked.json()["readiness"]["publishable"] is False
    assert blocked.json()["readiness"]["blockers"] == ["2 fact(s) still require review."]

    refused = http.post(f"/api/v1/compiler/candidates/{candidate.id}/accept", headers=bearer(token))
    assert refused.status_code == 409
    assert refused.json()["error"] == "candidate_requires_correction"


async def test_competing_corrections_are_serialized_by_postgresql(
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant, _, candidates = await reviewable_run(session)
    candidate = pending(candidates)[0]

    async def submit(value: int, excerpt: str) -> str:
        async with factory() as competing_session:
            try:
                review = await MerchantCompilerService(competing_session).review(
                    merchant.id,
                    candidate.id,
                    ReviewDecision.CORRECT,
                    correction=wattage_correction(candidate.target, value=value, excerpt=excerpt),
                )
            except ConflictError:
                return "conflict"
            return str(review.id)

    first, second = await asyncio.gather(submit(65, "65W"), submit(100, "100W"))
    assert sorted([first == "conflict", second == "conflict"]) == [False, True]
    async with factory() as verify_session:
        view = await MerchantCompilerReviewService(verify_session).run_view(
            merchant.id, candidate.run_id
        )
    reviewed = candidate_view(view.model_dump(mode="json"), candidate.id)
    assert reviewed["review"] is not None


async def test_a_review_racing_its_run_publication_has_one_safe_answer(
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A retry arriving beside a publication either finds its review or is told it is too late.

    Publication needs every candidate answered, so the honest race is a retry of a decision that
    has already landed, which is exactly what a merchant's double click produces. Both orders are
    safe and neither writes a second review: the run lock is what makes that true.
    """
    merchant, run, candidates = await acceptable_run(session)
    kept, dropped = pending(candidates)
    service = MerchantCompilerService(session)
    accepted = await service.review(merchant.id, kept.id, ReviewDecision.ACCEPT)
    await service.review(merchant.id, dropped.id, ReviewDecision.REJECT)

    async def publish() -> str:
        async with factory() as publishing:
            return str((await MerchantCompilerService(publishing).publish(merchant.id, run.id)).id)

    async def retry() -> str:
        async with factory() as reviewing:
            try:
                review = await MerchantCompilerService(reviewing).review(
                    merchant.id, kept.id, ReviewDecision.ACCEPT
                )
            except ConflictError as error:
                return error.reason
            return str(review.id)

    outcome, representation = await asyncio.gather(retry(), publish())
    assert outcome in {str(accepted.id), "compiler_run_already_published"}
    async with factory() as verify_session:
        after = await MerchantCompilerReviewService(verify_session).run_view(merchant.id, run.id)
    assert str(after.readiness.published_representation_id) == representation
    still_accepted = candidate_view(after.model_dump(mode="json"), kept.id)["review"]
    assert isinstance(still_accepted, dict)
    assert still_accepted["review_id"] == str(accepted.id)
    assert still_accepted["decision"] == "ACCEPT"


async def test_competing_publications_are_idempotent_in_postgresql(
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant, run, candidates = await reviewable_run(session)
    await resolve_every_correction(session, merchant.id, candidates)

    async def publish() -> str:
        async with factory() as competing_session:
            representation = await MerchantCompilerService(competing_session).publish(
                merchant.id, run.id
            )
            return str(representation.id)

    first, second = await asyncio.gather(publish(), publish())
    assert first == second


async def test_the_overview_counts_the_facts_a_merchant_still_has_to_answer_for(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    """The console's call to action is a count of real pending work, not of published facts.

    A published representation cannot contain a review-required fact, so reading the artifact
    always answered zero and the overview never asked a merchant to do anything.
    """
    merchant, run, candidates = await reviewable_run(session, slug="overview-count-shop")
    token = await issue_credential(merchant.id)
    other = await MerchantRepository(session).create(slug="overview-other-shop", name="Other")
    await session.commit()
    foreign = await issue_credential(other.id)
    http = client(settings, factory)

    def pending_count(credential: str) -> int:
        response = http.get("/api/v1/insights/overview", headers=bearer(credential))
        assert response.status_code == 200
        return int(response.json()["representation_state"]["review_required_facts"])

    assert pending_count(token) == 2
    assert pending_count(foreign) == 0

    watts = pending(candidates)
    http.post(
        f"/api/v1/compiler/candidates/{watts[0].id}/correct", headers=bearer(token), json=CORRECTION
    )
    assert pending_count(token) == 1

    http.post(
        f"/api/v1/compiler/candidates/{watts[1].id}/correct", headers=bearer(token), json=CORRECTION
    )
    assert pending_count(token) == 0
    assert (
        http.post(f"/api/v1/compiler/runs/{run.id}/publish", headers=bearer(token)).status_code
        == 200
    )
    assert pending_count(token) == 0


async def test_the_compiler_namespace_is_authenticated_and_echoes_no_credential(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    merchant, run, candidates = await reviewable_run(session, slug="hygiene-shop")
    token = await issue_credential(merchant.id)
    candidate = pending(candidates)[0]
    http = client(settings, factory)

    unauthenticated = [
        http.get("/api/v1/compiler/overview"),
        http.get(f"/api/v1/compiler/runs/{run.id}"),
        http.post(f"/api/v1/compiler/candidates/{candidate.id}/accept"),
        http.post(f"/api/v1/compiler/candidates/{candidate.id}/reject"),
        http.post(f"/api/v1/compiler/candidates/{candidate.id}/correct", json=CORRECTION),
        http.post(f"/api/v1/compiler/runs/{run.id}/publish"),
    ]
    assert [response.status_code for response in unauthenticated] == [401] * 6

    answered = [
        http.get("/api/v1/compiler/overview", headers=bearer(token)),
        http.get(f"/api/v1/compiler/runs/{run.id}", headers=bearer(token)),
        http.post(
            f"/api/v1/compiler/candidates/{candidate.id}/correct",
            headers=bearer(token),
            json=CORRECTION,
        ),
    ]
    joined = "\n".join(response.text for response in answered)
    assert all(response.status_code == 200 for response in answered)
    assert token not in joined
    assert token.split("_")[-1] not in joined
    assert "secret_hash" not in joined
    assert "authorization" not in joined.lower()
    assert "postgres" not in joined.lower()

    document = http.get("/openapi.json").json()
    compiler_paths = [path for path in document["paths"] if path.startswith("/api/v1/compiler")]
    assert len(compiler_paths) == 6
