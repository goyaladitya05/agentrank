"""Merchant-originated source evidence: taking it in, and reading back what is held.

Phase 4C made a review decision permanent and Phase 4D made a measurement explicit. Both are
right, and together they left the merchant with no way forward from a settled run: a published
representation cannot be edited, and a rejected fact cannot be un-rejected. The recovery was
always meant to be historical rather than corrective. Newer evidence becomes a new immutable
snapshot, the compiler reads it into new candidates, and those get their own review history and
their own representation. Nothing older changes.

This module is the merchant-facing half of that. It takes evidence in and it reads history back.
It does not compile anything: starting a compiler run is a separate command, because a snapshot
is a statement about the merchant's catalog and a compiler run is a reading of that statement,
and a workflow that produced both from one click would leave the merchant unable to say which of
the two they meant.

Three things are decided here and none of them by the browser.

**Which merchant.** From the credential that authenticated the request, never from the document.
A source document used to name its merchant by slug, because the only thing that wrote one was
the operator command line reading an authored file. A browser that could name a merchant slug
could write into another merchant's source history, so the submitted document has no slug field
at all and the slug is looked up from the authenticated merchant identifier.

**Which source line and which version.** The merchant continues their current source line at the
next version. A browser that could name a version could target one an existing compiler run and
published representation were derived from, and while the snapshot table would refuse to
overwrite it, a merchant should not be able to address it in the first place.

**Whether anything new is written at all.** Submitting evidence identical to the current snapshot
resolves to that snapshot. That is not a convenience: a second snapshot with identical content
would compile to identical candidates and produce an identical representation, so it would be a
second copy of one truth and a second review queue for one decision. What it means is stated
back to the merchant rather than hidden, through `created_snapshot`.

Retry semantics are two mechanisms because there are two situations. A request key covers the
same command arriving twice: a double submit, or a retry after a response nobody saw. Content
identity covers two different commands saying the same thing, which is what two browser tabs
rendered from the same snapshot produce. Both converge on one snapshot, and a partial unique
index makes the second impossible to get wrong under concurrency.
"""

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement, Select, func, literal_column, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from agentrank_api.benchmark.identity import canonical_json
from agentrank_api.commerce.models import Merchant
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.compiler.definitions import CompilerConfiguration
from agentrank_api.compiler.models import (
    CandidateState,
    CompilerCandidate,
    CompilerReview,
    CompilerRun,
)
from agentrank_api.errors import ConflictError, NotFoundError
from agentrank_api.representation.fields import MAX_EXCERPT_LENGTH, excerpt, source_fields
from agentrank_api.representation.fixtures import parse_source
from agentrank_api.representation.models import (
    CommerceRepresentation,
    MerchantSourceSnapshot,
    MerchantSourceSubmission,
    SourceOrigin,
)
from agentrank_api.representation.repository import MerchantSourceRepository
from agentrank_api.representation.schemas import (
    SourceCompilerRunView,
    SourceDocumentInput,
    SourceFieldView,
    SourceOverviewView,
    SourceSnapshotSummaryView,
    SourceSnapshotView,
    SourceSubmissionView,
)

# The source line a merchant's first console submission starts. A constant rather than something
# derived from the merchant, because a source key is unique per merchant already and a key built
# out of a slug would be a second, longer spelling of an identity the row beside it holds.
DEFAULT_SOURCE_KEY = "merchant-source"

# What a snapshot published before this table existed is reported as. Every one of them was
# written by the operator command line reading an authored file, and none of them has a
# submission row, so the origin is read from that absence rather than from a backfilled column
# that would state something nobody recorded.
OPERATOR_ORIGIN = "OPERATOR_FIXTURE"

SOURCE_RESOURCE = "merchant_source_snapshot"

# How many times one command will resolve a version before giving up. More than one because the
# operator command line publishes snapshots without taking the advisory lock, and bounded because
# a caller that keeps losing is a caller that should be told rather than retried forever.
_VERSION_ATTEMPTS = 3

# Counts computed by PostgreSQL rather than by loading every historical document into Python.
# A history page renders identity and size, and a merchant with a long history and large
# documents would otherwise pay for all of them to draw a table of numbers.
_VARIANT_COUNT: ColumnElement[int] = literal_column(
    "(SELECT coalesce(sum(jsonb_array_length(entry -> 'variants')), 0)"
    " FROM jsonb_array_elements(merchant_source_snapshot.payload -> 'products') AS entry)"
)
_POLICY_COUNT: ColumnElement[int] = literal_column(
    "(SELECT count(*) FROM jsonb_object_keys(merchant_source_snapshot.payload -> 'policy_text'))"
)


@dataclass(frozen=True, slots=True)
class SubmissionOutcome:
    """What one submission command did, before it is rendered for anybody."""

    submission: MerchantSourceSubmission
    snapshot: MerchantSourceSnapshot


class MerchantSourceIntakeService:
    """The merchant's own source evidence: submitted, listed and read back."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def submit(
        self,
        merchant_id: uuid.UUID,
        *,
        request_key: str,
        document: SourceDocumentInput,
    ) -> SubmissionOutcome:
        """Take one piece of merchant source evidence in, and say what became of it.

        Everything after the first read happens under a per-merchant advisory lock. Version
        allocation and the comparison against the current snapshot are both read-then-write, and
        two submissions racing without the lock would both read the same current version: one
        would lose the snapshot table's unique constraint and the other would have compared
        against a snapshot that is no longer current.

        Transaction scoped, so the lock is released by the commit or the rollback that ends this
        command. Taken before any row lock, which this command has none of, so it cannot join a
        cycle with the order `agentrank_api.locking` writes down.

        Nothing external is called and nothing is compiled, so this is one short transaction.
        """
        merchant = await self._merchant(merchant_id)
        # Plain values, because a retry below rolls back and a rollback expires every loaded
        # instance. Reading one afterwards is database IO in a place with no greenlet for it.
        merchant_slug = merchant.slug
        submitted = canonical_json(document.evidence())

        settled = await self._by_request_key(merchant_id, request_key)
        if settled is not None:
            return _same_request(settled, submitted, request_key)

        for attempt in range(_VERSION_ATTEMPTS):
            await self._claim(merchant_id)
            # Read again under the lock: an identical submit may have committed between the read
            # above and the lock, and answering with its outcome is what a request key is for.
            settled = await self._by_request_key(merchant_id, request_key)
            if settled is not None:
                return _same_request(settled, submitted, request_key)

            current = await self.current(merchant_id)
            key = DEFAULT_SOURCE_KEY if current is None else current.source_key
            definition = document.definition(
                key=key,
                version=await self._next_version(merchant_id, key),
                merchant_slug=merchant_slug,
            )
            if current is not None and _evidence(current.payload) == submitted:
                return await self._record(merchant_id, request_key, current, created=False)
            try:
                snapshot = await MerchantSourceRepository(self._session).create(
                    merchant, definition
                )
                return await self._record(merchant_id, request_key, snapshot, created=True)
            except IntegrityError:
                # A version this command had already allocated. Unreachable between two callers
                # holding the lock, and reachable from the operator command line, which publishes
                # a snapshot without taking it. Rolling back and resolving again is the answer;
                # exhausting the attempts is a caller that will not settle and is refused by name
                # rather than surfacing as a database error nobody can act on.
                await self._session.rollback()
                merchant = await self._merchant(merchant_id)
                if attempt == _VERSION_ATTEMPTS - 1:
                    raise ConflictError(
                        "source_version_conflict",
                        "another writer is publishing source snapshots for this merchant",
                        resource=SOURCE_RESOURCE,
                        identifier=str(merchant_id),
                    ) from None
        raise AssertionError("unreachable")  # pragma: no cover

    async def current(self, merchant_id: uuid.UUID) -> MerchantSourceSnapshot | None:
        """The merchant's newest source snapshot, or None if they have never published one.

        Ordered by identifier rather than by `created_at`, and that is load bearing rather than
        arbitrary. `created_at` defaults to `now()`, which PostgreSQL evaluates as
        `transaction_timestamp()`, so a transaction that began earlier and committed later
        carries an earlier timestamp than a snapshot it was written after. Two submissions
        serializing on the advisory lock are exactly that shape: the waiter's transaction began
        first and its row is written second, so ordering by timestamp would report the older
        version as current, and the next submission would compare its evidence against the wrong
        snapshot and write a duplicate.

        The identifier is `uuid7`, generated in Python at insert. It is time ordered by when the
        row was actually written, which is the order this question is asking about.
        """
        return (
            await self._session.execute(
                select(MerchantSourceSnapshot)
                .where(MerchantSourceSnapshot.merchant_id == merchant_id)
                .order_by(MerchantSourceSnapshot.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def _current_id(self, merchant_id: uuid.UUID) -> uuid.UUID | None:
        """Which snapshot is current, without loading the document inside it.

        The read views need the identifier and nothing else, and a source document is up to a
        hundred and twenty eight kilobytes of JSONB. Loading one to compare a UUID is the exact
        cost the summary columns are computed in PostgreSQL to avoid.
        """
        return (
            await self._session.execute(
                select(MerchantSourceSnapshot.id)
                .where(MerchantSourceSnapshot.merchant_id == merchant_id)
                .order_by(MerchantSourceSnapshot.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def snapshot(
        self, merchant_id: uuid.UUID, source_snapshot_id: uuid.UUID
    ) -> MerchantSourceSnapshot:
        """One snapshot this merchant owns. Somebody else's identifier is an unknown one."""
        found = (
            await self._session.execute(
                select(MerchantSourceSnapshot).where(
                    MerchantSourceSnapshot.id == source_snapshot_id,
                    MerchantSourceSnapshot.merchant_id == merchant_id,
                )
            )
        ).scalar_one_or_none()
        if found is None:
            raise NotFoundError(SOURCE_RESOURCE, str(source_snapshot_id))
        return found

    async def overview(self, merchant_id: uuid.UUID, *, limit: int = 20) -> SourceOverviewView:
        """This merchant's source history as identity and size, newest first."""
        current = await self._current_id(merchant_id)
        rows = list(
            (
                await self._session.execute(
                    self._summary_columns()
                    .where(MerchantSourceSnapshot.merchant_id == merchant_id)
                    .order_by(MerchantSourceSnapshot.id.desc())
                    .limit(limit)
                )
            ).all()
        )
        ids = [row.id for row in rows]
        summaries = await self._summaries(merchant_id, rows, ids, current)
        return SourceOverviewView(current_source_snapshot_id=current, snapshots=summaries)

    async def snapshot_view(
        self, merchant_id: uuid.UUID, source_snapshot_id: uuid.UUID
    ) -> SourceSnapshotView:
        """One snapshot, what it says, and every compiler run that has read it."""
        found = await self.snapshot(merchant_id, source_snapshot_id)
        current = await self._current_id(merchant_id)
        row = (
            await self._session.execute(
                self._summary_columns().where(MerchantSourceSnapshot.id == found.id)
            )
        ).one()
        summaries = await self._summaries(merchant_id, [row], [found.id], current)
        definition = parse_source(found.payload)
        runs = await self._runs(merchant_id, found.id)
        digest = CompilerConfiguration().configuration_digest
        existing = next((run for run in runs if run.configuration_digest == digest), None)
        return SourceSnapshotView(
            summary=summaries[0],
            document=_document(found.payload),
            fields=[
                SourceFieldView(
                    field=address,
                    excerpt=excerpt(value),
                    truncated=len(value) > MAX_EXCERPT_LENGTH,
                )
                for address, value in source_fields(definition).items()
            ],
            compiler_runs=runs,
            compilable=existing is None,
            existing_run_id=None if existing is None else existing.run_id,
        )

    async def submission_view(self, outcome: SubmissionOutcome) -> SourceSubmissionView:
        """One submission command's answer, carrying the snapshot it resolved to."""
        current = await self._current_id(outcome.submission.merchant_id)
        row = (
            await self._session.execute(
                self._summary_columns().where(MerchantSourceSnapshot.id == outcome.snapshot.id)
            )
        ).one()
        summaries = await self._summaries(
            outcome.submission.merchant_id, [row], [outcome.snapshot.id], current
        )
        return SourceSubmissionView(
            submission_id=outcome.submission.id,
            request_key=outcome.submission.request_key,
            created_snapshot=outcome.submission.created_snapshot,
            snapshot=summaries[0],
        )

    async def _record(
        self,
        merchant_id: uuid.UUID,
        request_key: str,
        snapshot: MerchantSourceSnapshot,
        *,
        created: bool,
    ) -> SubmissionOutcome:
        """Write the submission beside whatever snapshot it resolved to, in one transaction."""
        submission = MerchantSourceSubmission(
            merchant_id=merchant_id,
            request_key=request_key,
            source_snapshot_id=snapshot.id,
            origin=SourceOrigin.MERCHANT_CONSOLE,
            created_snapshot=created,
        )
        self._session.add(submission)
        try:
            await self._session.commit()
        except IntegrityError:
            # The advisory lock makes this unreachable between two callers holding it, and the
            # unique constraint is what refuses a caller that somehow did not hold it. Re-reading
            # the request key is the deterministic answer either way, and the rollback takes the
            # snapshot insert with it so no orphan version is left behind.
            await self._session.rollback()
            duplicate = await self._by_request_key(merchant_id, request_key)
            if duplicate is not None:
                return duplicate
            raise
        return SubmissionOutcome(submission=submission, snapshot=snapshot)

    async def _by_request_key(
        self, merchant_id: uuid.UUID, request_key: str
    ) -> SubmissionOutcome | None:
        """The submission this key already produced, with the snapshot it resolved to."""
        found = (
            await self._session.execute(
                select(MerchantSourceSubmission, MerchantSourceSnapshot)
                .join(
                    MerchantSourceSnapshot,
                    MerchantSourceSubmission.source_snapshot_id == MerchantSourceSnapshot.id,
                )
                .where(
                    MerchantSourceSubmission.merchant_id == merchant_id,
                    MerchantSourceSubmission.request_key == request_key,
                )
            )
        ).first()
        if found is None:
            return None
        submission, snapshot = found
        return SubmissionOutcome(submission=submission, snapshot=snapshot)

    async def _claim(self, merchant_id: uuid.UUID) -> None:
        """Hold this merchant's source line against every other submission, until commit."""
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"{SOURCE_RESOURCE}:{merchant_id}"},
        )

    async def _next_version(self, merchant_id: uuid.UUID, key: str) -> int:
        highest = (
            await self._session.execute(
                select(func.max(MerchantSourceSnapshot.source_version)).where(
                    MerchantSourceSnapshot.merchant_id == merchant_id,
                    MerchantSourceSnapshot.source_key == key,
                )
            )
        ).scalar_one_or_none()
        return 1 if highest is None else int(highest) + 1

    async def _merchant(self, merchant_id: uuid.UUID) -> Merchant:
        merchant = await MerchantRepository(self._session).get_by_id(merchant_id)
        if merchant is None:
            raise NotFoundError("merchant", str(merchant_id))
        return merchant

    def _summary_columns(self) -> Select[Any]:
        return select(
            MerchantSourceSnapshot.id,
            MerchantSourceSnapshot.source_key,
            MerchantSourceSnapshot.source_version,
            MerchantSourceSnapshot.content_hash,
            MerchantSourceSnapshot.created_at,
            func.jsonb_array_length(MerchantSourceSnapshot.payload["products"]).label("products"),
            _VARIANT_COUNT.label("variants"),
            _POLICY_COUNT.label("policies"),
        )

    async def _summaries(
        self,
        merchant_id: uuid.UUID,
        rows: Sequence[Any],
        ids: Sequence[uuid.UUID],
        current: uuid.UUID | None,
    ) -> list[SourceSnapshotSummaryView]:
        runs = await self._counts(CompilerRun.source_snapshot_id, ids)
        published = await self._counts(CommerceRepresentation.source_snapshot_id, ids)
        origins = await self._origins(merchant_id, ids)
        return [
            SourceSnapshotSummaryView(
                source_snapshot_id=row.id,
                source_label=f"{row.source_key}@{row.source_version}",
                source_key=row.source_key,
                source_version=row.source_version,
                content_hash=row.content_hash,
                created_at=row.created_at,
                origin=origins.get(row.id, OPERATOR_ORIGIN),
                product_count=int(row.products or 0),
                variant_count=int(row.variants or 0),
                policy_count=int(row.policies or 0),
                compiler_run_count=runs.get(row.id, 0),
                published_representation_count=published.get(row.id, 0),
                is_current=current == row.id,
            )
            for row in rows
        ]

    async def _counts(
        self, column: InstrumentedAttribute[uuid.UUID], ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        """How many rows of one lineage table name each snapshot, in one grouped query."""
        if not ids:
            return {}
        result = await self._session.execute(
            select(column, func.count()).where(column.in_(ids)).group_by(column)
        )
        return {snapshot_id: int(count) for snapshot_id, count in result.all()}

    async def _origins(
        self, merchant_id: uuid.UUID, ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        """Which mechanism wrote each snapshot, for the ones a submission claims."""
        if not ids:
            return {}
        result = await self._session.execute(
            select(
                MerchantSourceSubmission.source_snapshot_id, MerchantSourceSubmission.origin
            ).where(
                MerchantSourceSubmission.merchant_id == merchant_id,
                MerchantSourceSubmission.source_snapshot_id.in_(ids),
                MerchantSourceSubmission.created_snapshot.is_(True),
            )
        )
        return {snapshot_id: origin.value for snapshot_id, origin in result.all()}

    async def _runs(
        self, merchant_id: uuid.UUID, source_snapshot_id: uuid.UUID
    ) -> list[SourceCompilerRunView]:
        """Every compiler run over one snapshot, with how much of it is still unanswered."""
        runs = list(
            (
                await self._session.execute(
                    select(CompilerRun)
                    .where(
                        CompilerRun.merchant_id == merchant_id,
                        CompilerRun.source_snapshot_id == source_snapshot_id,
                    )
                    .order_by(CompilerRun.created_at.desc())
                )
            ).scalars()
        )
        ids = [run.id for run in runs]
        required = await self._review_required(merchant_id, ids)
        reviewed = await self._reviewed(merchant_id, ids)
        return [
            SourceCompilerRunView(
                run_id=run.id,
                status=run.status.value,
                configuration_digest=run.configuration_digest,
                created_at=run.created_at,
                completed_at=run.completed_at,
                error_code=run.error_code,
                review_required_count=required.get(run.id, 0),
                reviewed_count=reviewed.get(run.id, 0),
                published_representation_id=run.published_representation_id,
            )
            for run in runs
        ]

    async def _review_required(
        self, merchant_id: uuid.UUID, run_ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        ids = list(run_ids)
        if not ids:
            return {}
        result = await self._session.execute(
            select(CompilerCandidate.run_id, func.count())
            .where(
                CompilerCandidate.merchant_id == merchant_id,
                CompilerCandidate.run_id.in_(ids),
                CompilerCandidate.state == CandidateState.REVIEW_REQUIRED,
            )
            .group_by(CompilerCandidate.run_id)
        )
        return {run_id: int(count) for run_id, count in result.all()}

    async def _reviewed(
        self, merchant_id: uuid.UUID, run_ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        ids = list(run_ids)
        if not ids:
            return {}
        result = await self._session.execute(
            select(CompilerReview.run_id, func.count())
            .where(CompilerReview.merchant_id == merchant_id, CompilerReview.run_id.in_(ids))
            .group_by(CompilerReview.run_id)
        )
        return {run_id: int(count) for run_id, count in result.all()}


def _same_request(
    settled: SubmissionOutcome, submitted: str, request_key: str
) -> SubmissionOutcome:
    """The outcome this key already produced, if this really is the same command.

    A key names a command, so a repeat carrying the same evidence is that command and answers
    with what it did. A repeat carrying different evidence is a different command wearing the
    same name, and answering with the old outcome would tell a merchant their edit was stored
    when it was not. That is the refusal the re-evaluation launch already makes for a reused
    launch key, made here for the same reason.
    """
    if _evidence(settled.snapshot.payload) != submitted:
        raise ConflictError(
            "source_request_key_reused",
            "this submission key has already stored different source evidence",
            resource=SOURCE_RESOURCE,
            identifier=request_key,
        )
    return settled


def _document(payload: dict[str, Any]) -> dict[str, Any]:
    """The submittable half of a stored snapshot: what the merchant said, without its identity."""
    return {
        "products": payload.get("products", []),
        "policy_text": payload.get("policy_text", {}),
    }


def _evidence(payload: dict[str, Any]) -> str:
    """One canonical string for the evidence in a payload, ignoring the identity around it.

    Two snapshots differ in `version` by construction, so the stored content hash can never say
    that two versions carry the same evidence. This can, and it is what makes resubmitting an
    unchanged document resolve to the snapshot that already holds it.
    """
    return canonical_json(_document(payload))
