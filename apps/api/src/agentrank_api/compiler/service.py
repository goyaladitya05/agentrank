"""Compiler lifecycle, source-evidence validation, review, and publication."""

import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import func, select, text
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
from agentrank_api.compiler.targets import (
    POLICY_PREFIX,
    product_prefix,
    variant_attribute_prefix,
    variant_availability_target,
    variant_compatibility_prefix,
    variant_price_target,
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
from agentrank_api.representation.fields import MAX_EXCERPT_LENGTH, source_fields
from agentrank_api.representation.fixtures import RepresentationFixtureError, parse_source
from agentrank_api.representation.models import CommerceRepresentation, MerchantSourceSnapshot
from agentrank_api.representation.repository import CommerceRepresentationRepository

# What a run records when the document it was given could not be read as a source document or
# produced a candidate this build refuses. One code, because the distinction between the two is a
# detail of this module and neither is something a merchant acts on differently.
INVALID_SOURCE = "invalid_source_or_candidate"


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
        digest = configuration.configuration_digest
        # Read out as plain values before anything can roll back. A rollback expires every loaded
        # instance, and the recovery below rolls back on purpose: reading `source.id` afterwards
        # would be a lazy load, which is database IO in a place with no greenlet to run it. The
        # snapshot is immutable, so a copy of it taken now is the same snapshot later.
        source_id, content_hash, payload = source.id, source.content_hash, dict(source.payload)
        existing = await self._run_for_input(source_id, digest)
        if existing is not None:
            return existing
        # Read the document before anything is written. Compilation is pure CPU over a document
        # already in memory: no network, no model, no second query, and milliseconds.
        #
        # That ordering is the whole of the crash recovery. This used to commit PENDING, commit
        # RUNNING, and only then extract, which meant the unique key on the snapshot and the
        # configuration was consumed by the first commit. A process killed between commits left a
        # run stuck PENDING or RUNNING that nothing recovers, publishing refused it forever, and
        # the merchant could not escape by resubmitting their source either: identical evidence
        # deduplicates back onto the same snapshot, so the same dead run came back. That was an
        # unescapable dead end reachable by an ordinary restart.
        #
        # Now nothing is written until the outcome is known, so a process that dies leaves no row
        # and the merchant simply compiles again.
        # Nothing is held open across the compile. Reading the snapshot opened a transaction, and
        # `_compile` is synchronous CPU work over a document already in memory, so leaving it
        # open would pin a snapshot and an idle connection for the length of that work.
        #
        # A commit rather than a rollback, and the difference matters: this session belongs to
        # the caller, `expire_on_commit` is off, and a rollback would expire every instance the
        # caller loaded before it ever called this. Nothing was written, so committing a read is
        # exactly ending it.
        await self._session.commit()
        outcome, candidates = self._compile(payload, content_hash)
        settled = await self._session.scalar(select(func.now()))
        assert settled is not None  # `now()` always answers
        run = CompilerRun(
            merchant_id=merchant_id,
            source_snapshot_id=source_id,
            configuration_digest=digest,
            configuration=configuration.payload(),
            status=outcome,
            error_code=None if outcome is CompilerRunStatus.COMPLETED else INVALID_SOURCE,
            # The database's clock, which is the one `created_at` defaults from. Two clocks would
            # let host skew render a run as completed before it was created.
            completed_at=settled,
        )
        try:
            self._session.add(run)
            # Flushed rather than committed, so the run has an identity for its candidates to
            # name and the whole thing is still one transaction. A process that dies anywhere
            # between here and the commit leaves nothing behind.
            await self._session.flush()
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
            await self._session.commit()
        except IntegrityError:
            # Two launches of one snapshot under one configuration. The unique key is what makes
            # them one run rather than two readings of one document, and the loser answers with
            # the run the winner wrote rather than with an error nothing went wrong for.
            await self._session.rollback()
            existing = await self._run_for_input(source_id, digest)
            if existing is not None:
                return existing
            raise
        return run

    def _compile(
        self, payload: dict[str, Any], content_hash: str
    ) -> tuple[CompilerRunStatus, list[tuple[CandidateProposal, CandidateState]]]:
        """Read one stored source document and propose what it states, or say it could not be.

        Pure. No session, no clock and no IO of any kind, which is what lets the whole run be one
        transaction: the work happens first and the row that records it is written once, settled.

        A document this build cannot read is a FAILED run with no candidates rather than an
        exception, because a snapshot that was valid when it was written and is not readable now
        is a fact about this merchant's evidence that they should be able to see.
        """
        try:
            definition = parse_source(payload)
            if definition.content_hash != content_hash:
                raise ValueError("persisted source content hash does not match its payload")
            candidates = extract(definition)
            self._validate_candidates(candidates, definition)
        except RepresentationFixtureError, ValueError:
            return CompilerRunStatus.FAILED, []
        return CompilerRunStatus.COMPLETED, candidates

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

    async def _claim_publication(self, merchant_id: uuid.UUID) -> None:
        """Hold this merchant's publication line against every other publish, until commit."""
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"commerce_representation:{merchant_id}"},
        )

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
        # Review and publication serialize on the run first, then a candidate.  A published
        # representation must be the exact reviewed state it names, never a stale snapshot that
        # a concurrent correction can overtake.
        candidate = (
            await self._session.execute(
                select(CompilerCandidate)
                .join(CompilerRun, CompilerCandidate.run_id == CompilerRun.id)
                .where(
                    CompilerCandidate.id == candidate_id,
                    CompilerCandidate.merchant_id == merchant_id,
                    CompilerRun.merchant_id == merchant_id,
                )
                .with_for_update(of=CompilerRun)
            )
        ).scalar_one_or_none()
        if candidate is None:
            raise NotFoundError("compiler_candidate", str(candidate_id))
        run = await self._run_for_update(merchant_id, candidate.run_id)
        if run.published_representation_id is not None:
            raise ConflictError("compiler_run_already_published", str(run.id))
        candidate = (
            await self._session.execute(
                select(CompilerCandidate)
                .where(
                    CompilerCandidate.id == candidate_id,
                    CompilerCandidate.merchant_id == merchant_id,
                )
                .with_for_update()
            )
        ).scalar_one()
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
            if (
                correction.fact.authority is not FactAuthority.DERIVED
                or correction.fact.confidence is not FactConfidence.HIGH
                or correction.fact.review_state is not ReviewState.CONFIRMED
            ):
                raise ValueError("a correction must be derived, high confidence, and confirmed")
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
        """Publish one completed compiler run's representation, once, for one merchant.

        Two locks and they do different jobs. The per-merchant advisory lock serializes every
        publication this merchant makes; the row lock on the run makes publishing that particular
        run a one-way transition and prevents two requests producing equivalent representations
        or losing the run link.

        The advisory lock is what makes `write_order` mean what the reads that use it assume.
        `write_order` is allocated at INSERT, so it equals commit order only while something
        serializes the whole read-decide-insert-commit section. Two tabs publishing two different
        runs lock two different rows and would otherwise interleave: the one that inserted first
        can commit second, and every later re-evaluation would then measure the representation the
        merchant published second-to-last. The source intake takes the same lock for the same
        reason and says so.

        Transaction scoped and taken before any row lock, which is the order
        `agentrank_api.locking` writes down, so it cannot join a cycle with the commerce locks.
        """
        await self._claim_publication(merchant_id)
        run = await self._run_for_update(merchant_id, run_id)
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

    async def _run_for_update(self, merchant_id: uuid.UUID, run_id: uuid.UUID) -> CompilerRun:
        run = (
            await self._session.execute(
                select(CompilerRun)
                .where(CompilerRun.id == run_id, CompilerRun.merchant_id == merchant_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if run is None:
            raise NotFoundError("compiler_run", str(run_id))
        return run

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
        fields = source_fields(source)
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
                    len(reference.excerpt) > MAX_EXCERPT_LENGTH or reference.excerpt not in text
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
                attribute_prefix = variant_attribute_prefix(variant.sku)
                compatibility_prefix = variant_compatibility_prefix(variant.sku)
                attributes = tuple(
                    CommerceAttribute(
                        target.removeprefix(attribute_prefix),
                        proposal.attribute_kind,
                        proposal.fact,
                        proposal.unit,
                    )
                    for target, proposal in sorted(effective.items())
                    if target.startswith(attribute_prefix) and proposal.attribute_kind is not None
                )
                compatibility = {
                    target.removeprefix(compatibility_prefix): proposal.fact
                    for target, proposal in effective.items()
                    if target.startswith(compatibility_prefix)
                }
                variants.append(
                    CommerceVariant(
                        sku=variant.sku,
                        label=variant.label,
                        price=effective[variant_price_target(variant.sku)].fact,
                        availability=effective[variant_availability_target(variant.sku)].fact,
                        attributes=attributes,
                        compatibility=compatibility,
                    )
                )
            fields = product_prefix(product.external_id)
            products.append(
                CommerceProduct(
                    external_id=product.external_id,
                    title=effective[fields + "title"].fact,
                    category=(
                        effective[fields + "category"].fact
                        if fields + "category" in effective
                        else None
                    ),
                    variants=tuple(variants),
                    policy_facts={
                        target.removeprefix(POLICY_PREFIX): proposal.fact
                        for target, proposal in effective.items()
                        if product_index == 0 and target.startswith(POLICY_PREFIX)
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
    fields = source_fields(source)
    expected = f"{proposal.fact.value}{proposal.unit}"
    if not any(
        expected.lower() in fields[reference.field].replace(" ", "").lower()
        for reference in proposal.fact.provenance
    ):
        raise ValueError("measurement correction value is not supported by cited source evidence")
