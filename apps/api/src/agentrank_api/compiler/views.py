"""Merchant-scoped compiler review read models and browser command adaptation."""

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.compiler.definitions import CandidateProposal
from agentrank_api.compiler.models import (
    CandidateState,
    CompilerCandidate,
    CompilerReview,
    CompilerRun,
    CompilerRunStatus,
    ReviewDecision,
)
from agentrank_api.compiler.schemas import (
    CompilerCandidateView,
    CompilerEvidenceView,
    CompilerOverviewView,
    CompilerReviewHistoryView,
    CompilerRunReviewView,
    CompilerRunSummaryView,
    PublishReadinessView,
)
from agentrank_api.compiler.service import MerchantCompilerService, _proposal
from agentrank_api.errors import NotFoundError
from agentrank_api.representation.definitions import (
    FactAuthority,
    FactConfidence,
    ReviewState,
    SemanticFact,
    SourceReference,
)
from agentrank_api.representation.models import MerchantSourceSnapshot


class MerchantCompilerReviewService:
    """Bounded merchant review views, never raw compiler ORM records."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._compiler = MerchantCompilerService(session)

    async def overview(self, merchant_id: uuid.UUID, *, limit: int = 20) -> CompilerOverviewView:
        runs = list(
            (
                await self._session.execute(
                    select(CompilerRun)
                    .where(CompilerRun.merchant_id == merchant_id)
                    .order_by(CompilerRun.created_at.desc())
                    .limit(limit)
                )
            ).scalars()
        )
        source_labels = await self._source_labels(
            merchant_id, (run.source_snapshot_id for run in runs)
        )
        reviews = await self._reviews_for_runs(merchant_id, (run.id for run in runs))
        candidates = await self._candidates_for_runs(merchant_id, (run.id for run in runs))
        summaries = [
            self._summary(
                run, source_labels[run.source_snapshot_id], candidates.get(run.id, []), reviews
            )
            for run in runs
        ]
        current = next(
            (run.published_representation_id for run in runs if run.published_representation_id),
            None,
        )
        return CompilerOverviewView(
            current_representation_id=current,
            review_required_count=sum(
                summary.review_required_count - summary.reviewed_count for summary in summaries
            ),
            runs=summaries,
        )

    async def run_view(self, merchant_id: uuid.UUID, run_id: uuid.UUID) -> CompilerRunReviewView:
        run = await self._compiler.get_run(merchant_id, run_id)
        source = await self._source(merchant_id, run.source_snapshot_id)
        candidates = await self._compiler.candidates(merchant_id, run.id)
        reviews = await self._reviews_for_runs(merchant_id, [run.id])
        candidate_views = [
            self._candidate_view(candidate, reviews.get(candidate.id)) for candidate in candidates
        ]
        return CompilerRunReviewView(
            run_id=run.id,
            source_snapshot_id=source.id,
            source_label=source.label,
            configuration_digest=run.configuration_digest,
            status=run.status.value,
            created_at=run.created_at,
            completed_at=run.completed_at,
            candidates=candidate_views,
            readiness=self._readiness(run, candidates, reviews),
        )

    async def review(
        self,
        merchant_id: uuid.UUID,
        candidate_id: uuid.UUID,
        decision: ReviewDecision,
        *,
        value: str | int | bool | None = None,
        provenance_field: str | None = None,
        provenance_excerpt: str | None = None,
        reviewer: str = "MERCHANT_CREDENTIAL",
    ) -> CompilerReview:
        correction = None
        if decision is ReviewDecision.CORRECT:
            if value is None or provenance_field is None:
                raise ValueError("a correction needs a value and source citation")
            candidate = await self._candidate(merchant_id, candidate_id)
            original = _proposal(candidate.proposal)
            correction = CandidateProposal(
                target=original.target,
                fact=SemanticFact(
                    value=value,
                    authority=FactAuthority.DERIVED,
                    confidence=FactConfidence.HIGH,
                    review_state=ReviewState.CONFIRMED,
                    provenance=(SourceReference(provenance_field, provenance_excerpt),),
                ),
                attribute_kind=original.attribute_kind,
                unit=original.unit,
            )
        return await self._compiler.review(
            merchant_id, candidate_id, decision, correction=correction, reviewer=reviewer
        )

    async def _candidate(
        self, merchant_id: uuid.UUID, candidate_id: uuid.UUID
    ) -> CompilerCandidate:
        candidate = (
            await self._session.execute(
                select(CompilerCandidate).where(
                    CompilerCandidate.id == candidate_id,
                    CompilerCandidate.merchant_id == merchant_id,
                )
            )
        ).scalar_one_or_none()
        if candidate is None:
            raise NotFoundError("compiler_candidate", str(candidate_id))
        return candidate

    async def _source(self, merchant_id: uuid.UUID, source_id: uuid.UUID) -> MerchantSourceSnapshot:
        source = (
            await self._session.execute(
                select(MerchantSourceSnapshot).where(
                    MerchantSourceSnapshot.id == source_id,
                    MerchantSourceSnapshot.merchant_id == merchant_id,
                )
            )
        ).scalar_one_or_none()
        if source is None:
            raise NotFoundError("merchant_source_snapshot", str(source_id))
        return source

    async def _source_labels(
        self, merchant_id: uuid.UUID, source_ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        ids = list(source_ids)
        if not ids:
            return {}
        sources = (
            await self._session.execute(
                select(MerchantSourceSnapshot).where(
                    MerchantSourceSnapshot.merchant_id == merchant_id,
                    MerchantSourceSnapshot.id.in_(ids),
                )
            )
        ).scalars()
        return {source.id: source.label for source in sources}

    async def _candidates_for_runs(
        self, merchant_id: uuid.UUID, run_ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, list[CompilerCandidate]]:
        ids = list(run_ids)
        result: dict[uuid.UUID, list[CompilerCandidate]] = {run_id: [] for run_id in ids}
        if not ids:
            return result
        candidates = (
            await self._session.execute(
                select(CompilerCandidate).where(
                    CompilerCandidate.merchant_id == merchant_id, CompilerCandidate.run_id.in_(ids)
                )
            )
        ).scalars()
        for candidate in candidates:
            result[candidate.run_id].append(candidate)
        return result

    async def _reviews_for_runs(
        self, merchant_id: uuid.UUID, run_ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, CompilerReview]:
        ids = list(run_ids)
        if not ids:
            return {}
        reviews = (
            await self._session.execute(
                select(CompilerReview).where(
                    CompilerReview.merchant_id == merchant_id, CompilerReview.run_id.in_(ids)
                )
            )
        ).scalars()
        return {review.candidate_id: review for review in reviews}

    def _summary(
        self,
        run: CompilerRun,
        source_label: str,
        candidates: list[CompilerCandidate],
        reviews: dict[uuid.UUID, CompilerReview],
    ) -> CompilerRunSummaryView:
        required = [
            candidate
            for candidate in candidates
            if candidate.state is CandidateState.REVIEW_REQUIRED
        ]
        return CompilerRunSummaryView(
            run_id=run.id,
            source_snapshot_id=run.source_snapshot_id,
            source_label=source_label,
            status=run.status.value,
            created_at=run.created_at,
            review_required_count=len(required),
            reviewed_count=sum(candidate.id in reviews for candidate in required),
            published_representation_id=run.published_representation_id,
        )

    def _readiness(
        self,
        run: CompilerRun,
        candidates: list[CompilerCandidate],
        reviews: dict[uuid.UUID, CompilerReview],
    ) -> PublishReadinessView:
        blockers: list[str] = []
        if run.status is not CompilerRunStatus.COMPLETED:
            blockers.append("The compiler run did not complete.")
        unresolved = [
            candidate.target
            for candidate in candidates
            if candidate.state is CandidateState.REVIEW_REQUIRED and candidate.id not in reviews
        ]
        if unresolved:
            blockers.append(f"{len(unresolved)} fact(s) still require review.")
        return PublishReadinessView(
            publishable=not blockers and run.published_representation_id is None,
            blockers=blockers,
            published_representation_id=run.published_representation_id,
        )

    def _candidate_view(
        self, candidate: CompilerCandidate, review: CompilerReview | None
    ) -> CompilerCandidateView:
        proposal = _proposal(candidate.proposal)
        fact = proposal.fact
        target_parts = candidate.target.split(".")
        product_or_variant = (
            ".".join(target_parts[:2]) if len(target_parts) > 1 else candidate.target
        )
        attribute = target_parts[-1]
        return CompilerCandidateView(
            candidate_id=candidate.id,
            target=candidate.target,
            product_or_variant=product_or_variant,
            attribute=attribute,
            proposal=candidate.proposal,
            attribute_kind=None
            if proposal.attribute_kind is None
            else proposal.attribute_kind.value,
            unit=proposal.unit,
            state=(review.decision.value if review is not None else candidate.state.value),
            requires_correction=proposal.requires_correction,
            evidence=[
                CompilerEvidenceView(field=reference.field, excerpt=reference.excerpt)
                for reference in fact.provenance
            ],
            review=(
                None
                if review is None
                else CompilerReviewHistoryView(
                    review_id=review.id,
                    decision=review.decision.value,
                    correction=review.correction,
                    reviewer=review.reviewer,
                    created_at=review.created_at,
                )
            ),
        )
