"""Compiler lifecycle, source-evidence validation, review, and publication."""

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.compiler.definitions import CandidateProposal, CompilerConfiguration
from agentrank_api.compiler.extraction import extract
from agentrank_api.compiler.models import (
    CandidateState,
    CompilerCandidate,
    CompilerReview,
    CompilerRun,
    CompilerRunStatus,
    ReviewDecision,
)
from agentrank_api.errors import ConflictError, NotFoundError
from agentrank_api.representation.definitions import (
    AttributeKind,
    CommerceAttribute,
    CommerceIRDefinition,
    CommerceProduct,
    CommerceVariant,
    FactAuthority,
    FactConfidence,
    RepresentationProducer,
    ReviewState,
    SemanticFact,
    SourceReference,
    ValueState,
)
from agentrank_api.representation.fixtures import RepresentationFixtureError, parse_source
from agentrank_api.representation.models import CommerceRepresentation, MerchantSourceSnapshot
from agentrank_api.representation.repository import CommerceRepresentationRepository


class MerchantCompilerService:
    """A whole-snapshot compiler.  It has no benchmark or buyer dependencies by design."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def run(
        self,
        merchant_id: uuid.UUID,
        source_snapshot_id: uuid.UUID,
        *,
        configuration: CompilerConfiguration | None = None,
    ) -> CompilerRun:
        configuration = CompilerConfiguration() if configuration is None else configuration
        source = await self._source(merchant_id, source_snapshot_id)
        existing = await self._run_for_input(source.id, configuration.configuration_digest)
        if existing is not None:
            return existing
        run = CompilerRun(
            merchant_id=merchant_id,
            source_snapshot_id=source.id,
            configuration_digest=configuration.configuration_digest,
            configuration=configuration.payload(),
            status=CompilerRunStatus.PENDING,
        )
        self._session.add(run)
        try:
            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            existing = await self._run_for_input(source.id, configuration.configuration_digest)
            if existing is not None:
                return existing
            raise
        run.status = CompilerRunStatus.RUNNING
        await self._session.commit()
        try:
            definition = parse_source(source.payload)
            if definition.content_hash != source.content_hash:
                raise ValueError("persisted source content hash does not match its payload")
            candidates = extract(definition)
            self._validate_candidates(candidates, definition)
            self._session.add_all(
                CompilerCandidate(
                    merchant_id=merchant_id,
                    run_id=run.id,
                    target=proposal.target,
                    proposal=proposal.payload(),
                    state=state,
                )
                for proposal, state in candidates
            )
            run.status = CompilerRunStatus.COMPLETED
            run.completed_at = datetime.now(UTC)
            await self._session.commit()
            return run
        except RepresentationFixtureError, ValueError:
            run.status = CompilerRunStatus.FAILED
            run.error_code = "invalid_source_or_candidate"
            run.completed_at = datetime.now(UTC)
            await self._session.commit()
            return run

    async def get_run(self, merchant_id: uuid.UUID, run_id: uuid.UUID) -> CompilerRun:
        run = (
            await self._session.execute(
                select(CompilerRun).where(
                    CompilerRun.id == run_id, CompilerRun.merchant_id == merchant_id
                )
            )
        ).scalar_one_or_none()
        if run is None:
            raise NotFoundError("compiler_run", str(run_id))
        return run

    async def candidates(
        self, merchant_id: uuid.UUID, run_id: uuid.UUID
    ) -> list[CompilerCandidate]:
        await self.get_run(merchant_id, run_id)
        return list(
            (
                await self._session.execute(
                    select(CompilerCandidate)
                    .where(
                        CompilerCandidate.run_id == run_id,
                        CompilerCandidate.merchant_id == merchant_id,
                    )
                    .order_by(CompilerCandidate.target)
                )
            ).scalars()
        )

    async def review(
        self,
        merchant_id: uuid.UUID,
        candidate_id: uuid.UUID,
        decision: ReviewDecision,
        *,
        correction: CandidateProposal | None = None,
        reviewer: str = "SYSTEM",
    ) -> CompilerReview:
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
        if candidate.state is not CandidateState.REVIEW_REQUIRED:
            raise ConflictError("candidate_does_not_require_review", candidate.target)
        original = _proposal(candidate.proposal)
        if decision is ReviewDecision.ACCEPT and original.requires_correction:
            raise ConflictError("candidate_requires_correction", candidate.target)
        if decision is ReviewDecision.CORRECT:
            if correction is None or correction.target != candidate.target:
                raise ValueError("a correction must replace the same candidate target")
            if (
                correction.attribute_kind is not original.attribute_kind
                or correction.unit != original.unit
                or correction.requires_correction
            ):
                raise ValueError("a correction must preserve the candidate type and unit")
            _validate_target_value(correction)
            source = await self._source_for_run(candidate.run_id, merchant_id)
            self._validate_candidates(
                [(correction, CandidateState.ACCEPTED)], parse_source(source.payload)
            )
            _validate_correction_evidence(correction, parse_source(source.payload))
        elif correction is not None:
            raise ValueError("only a correction review may carry corrected fact data")
        existing = (
            await self._session.execute(
                select(CompilerReview).where(CompilerReview.candidate_id == candidate.id)
            )
        ).scalar_one_or_none()
        proposed = None if correction is None else correction.payload()
        if existing is not None:
            if existing.decision is decision and existing.correction == proposed:
                return existing
            raise ConflictError("candidate_already_reviewed", candidate.target)
        row = CompilerReview(
            merchant_id=merchant_id,
            run_id=candidate.run_id,
            candidate_id=candidate.id,
            decision=decision,
            correction=proposed,
            reviewer=reviewer,
        )
        self._session.add(row)
        await self._session.commit()
        return row

    async def publish(self, merchant_id: uuid.UUID, run_id: uuid.UUID) -> CommerceRepresentation:
        run = await self.get_run(merchant_id, run_id)
        if run.status is not CompilerRunStatus.COMPLETED:
            raise ConflictError("compiler_run_not_completed", str(run_id))
        if run.published_representation_id is not None:
            representation = (
                await self._session.execute(
                    select(CommerceRepresentation).where(
                        CommerceRepresentation.id == run.published_representation_id,
                        CommerceRepresentation.merchant_id == merchant_id,
                    )
                )
            ).scalar_one_or_none()
            if representation is not None:
                return representation
            raise RuntimeError("compiler run names a missing published representation")
        source = await self._source(merchant_id, run.source_snapshot_id)
        source_definition = parse_source(source.payload)
        candidates = await self.candidates(merchant_id, run.id)
        reviews = {
            review.candidate_id: review
            for review in (
                await self._session.execute(
                    select(CompilerReview).where(CompilerReview.run_id == run.id)
                )
            ).scalars()
        }
        unresolved = [
            candidate.target
            for candidate in candidates
            if candidate.state is CandidateState.REVIEW_REQUIRED and candidate.id not in reviews
        ]
        if unresolved:
            raise ConflictError("compiler_review_required", ", ".join(unresolved))
        definition = self._ir(source_definition, source.content_hash, run, candidates, reviews)
        try:
            representation = await CommerceRepresentationRepository(self._session).create(
                source=source, definition=definition, compiler_run=run
            )
            run.published_representation_id = representation.id
            await self._session.commit()
            return representation
        except IntegrityError as error:
            await self._session.rollback()
            existing = (
                await self._session.execute(
                    select(CommerceRepresentation).where(
                        CommerceRepresentation.merchant_id == merchant_id,
                        CommerceRepresentation.source_snapshot_id == source.id,
                        CommerceRepresentation.producer == RepresentationProducer.COMPILER,
                        CommerceRepresentation.producer_version == run.configuration_digest,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing
            raise ConflictError("compiler_representation_conflict", str(run_id)) from error

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

    async def _source_for_run(
        self, run_id: uuid.UUID, merchant_id: uuid.UUID
    ) -> MerchantSourceSnapshot:
        run = await self.get_run(merchant_id, run_id)
        return await self._source(merchant_id, run.source_snapshot_id)

    async def _run_for_input(
        self, source_id: uuid.UUID, configuration_digest: str
    ) -> CompilerRun | None:
        return (
            await self._session.execute(
                select(CompilerRun).where(
                    CompilerRun.source_snapshot_id == source_id,
                    CompilerRun.configuration_digest == configuration_digest,
                )
            )
        ).scalar_one_or_none()

    def _validate_candidates(
        self,
        candidates: Iterable[tuple[CandidateProposal, CandidateState]],
        source: Any,
    ) -> None:
        fields = _source_fields(source)
        for proposal, state in candidates:
            if (
                state is CandidateState.REVIEW_REQUIRED
                and proposal.fact.review_state is not ReviewState.REVIEW_REQUIRED
            ):
                raise ValueError("review-required candidate must retain review-required fact state")
            if (
                state is CandidateState.ACCEPTED
                and proposal.fact.review_state is ReviewState.REVIEW_REQUIRED
            ):
                raise ValueError("review-required fact cannot be automatically accepted")
            for reference in proposal.fact.provenance:
                text = fields.get(reference.field)
                if text is None:
                    raise ValueError("candidate provenance does not name a source field")
                if reference.excerpt is not None and (
                    len(reference.excerpt) > 500 or reference.excerpt not in text
                ):
                    raise ValueError("candidate provenance excerpt is not present in the source")

    def _ir(
        self,
        source: Any,
        source_hash: str,
        run: CompilerRun,
        candidates: list[CompilerCandidate],
        reviews: dict[uuid.UUID, CompilerReview],
    ) -> CommerceIRDefinition:
        effective: dict[str, CandidateProposal] = {}
        for candidate in candidates:
            review = reviews.get(candidate.id)
            if candidate.state is CandidateState.REJECTED or (
                review and review.decision is ReviewDecision.REJECT
            ):
                continue
            payload = (
                candidate.proposal
                if review is None or review.correction is None
                else review.correction
            )
            proposal = _proposal(payload)
            if review is not None and review.decision is ReviewDecision.ACCEPT:
                proposal = CandidateProposal(
                    target=proposal.target,
                    fact=SemanticFact(
                        value=proposal.fact.value,
                        authority=proposal.fact.authority,
                        confidence=FactConfidence.HIGH,
                        review_state=ReviewState.CONFIRMED,
                        provenance=proposal.fact.provenance,
                    ),
                    attribute_kind=proposal.attribute_kind,
                    unit=proposal.unit,
                )
            effective[candidate.target] = proposal
        products: list[CommerceProduct] = []
        for product_index, product in enumerate(source.products):
            variants: list[CommerceVariant] = []
            for variant in product.variants:
                prefix = f"variant.{variant.sku}."
                attributes = tuple(
                    CommerceAttribute(
                        target.removeprefix(prefix + "attribute."),
                        proposal.attribute_kind,
                        proposal.fact,
                        proposal.unit,
                    )
                    for target, proposal in sorted(effective.items())
                    if target.startswith(prefix + "attribute.")
                    and proposal.attribute_kind is not None
                )
                compatibility = {
                    target.removeprefix(prefix + "compatibility."): proposal.fact
                    for target, proposal in effective.items()
                    if target.startswith(prefix + "compatibility.")
                }
                variants.append(
                    CommerceVariant(
                        sku=variant.sku,
                        label=variant.label,
                        price=effective[prefix + "price"].fact,
                        availability=effective[prefix + "availability"].fact,
                        attributes=attributes,
                        compatibility=compatibility,
                    )
                )
            product_prefix = f"product.{product.external_id}."
            products.append(
                CommerceProduct(
                    external_id=product.external_id,
                    title=effective[product_prefix + "title"].fact,
                    category=(
                        effective[product_prefix + "category"].fact
                        if product_prefix + "category" in effective
                        else None
                    ),
                    variants=tuple(variants),
                    policy_facts={
                        target.removeprefix("policy."): proposal.fact
                        for target, proposal in effective.items()
                        if product_index == 0 and target.startswith("policy.")
                    },
                )
            )
        return CommerceIRDefinition(
            source_key=source.key,
            source_version=source.version,
            source_hash=source_hash,
            producer=RepresentationProducer.COMPILER,
            producer_version=run.configuration_digest,
            products=tuple(products),
        )


def _source_fields(source: Any) -> dict[str, str]:
    fields: dict[str, str] = {}
    for product in source.products:
        prefix = f"products[{product.external_id}]"
        fields[f"{prefix}.title"] = product.title
        if product.description is not None:
            fields[f"{prefix}.description"] = product.description
        if product.category is not None:
            fields[f"{prefix}.category"] = product.category
        for variant in product.variants:
            variant_prefix = f"{prefix}.variants[{variant.sku}]"
            fields[f"{variant_prefix}.price_amount_minor"] = str(variant.price_amount_minor)
            fields[f"{variant_prefix}.inventory_quantity"] = str(variant.inventory_quantity)
            for key, value in variant.merchant_metadata.items():
                if isinstance(value, str):
                    fields[f"{variant_prefix}.merchant_metadata.{key}"] = value
            if variant.label is not None:
                fields[f"{variant_prefix}.label"] = variant.label
    for key, value in source.policy_text.items():
        fields[f"policy_text.{key}"] = value
    return fields


def _proposal(payload: dict[str, Any]) -> CandidateProposal:
    fact = payload.get("fact")
    if not isinstance(fact, dict):
        raise ValueError("candidate proposal has no fact")
    references = fact.get("provenance")
    if not isinstance(references, list):
        raise ValueError("candidate proposal has no provenance")
    return CandidateProposal(
        target=payload["target"],
        fact=SemanticFact(
            value=fact["value"],
            authority=FactAuthority(fact["authority"]),
            confidence=FactConfidence(fact["confidence"]),
            review_state=ReviewState(fact["review_state"]),
            provenance=tuple(
                SourceReference(reference["field"], reference.get("excerpt"))
                for reference in references
            ),
        ),
        attribute_kind=(
            None
            if payload.get("attribute_kind") is None
            else AttributeKind(payload["attribute_kind"])
        ),
        unit=payload.get("unit"),
        requires_correction=payload.get("requires_correction", False),
    )


def _validate_target_value(proposal: CandidateProposal) -> None:
    if proposal.attribute_kind is not None:
        CommerceAttribute(
            proposal.target.rsplit(".", maxsplit=1)[-1],
            proposal.attribute_kind,
            proposal.fact,
            proposal.unit,
        )
    elif ".compatibility." in proposal.target and proposal.fact.value not in {
        item.value for item in ValueState
    }:
        raise ValueError("compatibility corrections need a four-state value")


def _validate_correction_evidence(proposal: CandidateProposal, source: Any) -> None:
    if proposal.attribute_kind is not AttributeKind.MEASUREMENT:
        return
    fields = _source_fields(source)
    expected = f"{proposal.fact.value}{proposal.unit}"
    if not any(
        expected.lower() in fields[reference.field].replace(" ", "").lower()
        for reference in proposal.fact.provenance
    ):
        raise ValueError("measurement correction value is not supported by cited source evidence")
