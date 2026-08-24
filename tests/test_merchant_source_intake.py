"""What supplying merchant source evidence does, and what it deliberately does not do.

The service, not the HTTP surface. What is asserted here is the part a route cannot decide: which
version a submission becomes, when a submission writes nothing at all, what a repeat means, and
what two of them arriving at once produce against a real PostgreSQL.
"""

import asyncio
from typing import Any

import pytest
from source_support import (
    FIRST_KEY,
    SECOND_KEY,
    bare_merchant,
    contradicted_document,
    merchant_with_source,
    voltedge_document,
)
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentrank_api.errors import NotFoundError
from agentrank_api.representation.intake import (
    DEFAULT_SOURCE_KEY,
    OPERATOR_ORIGIN,
    MerchantSourceIntakeService,
)
from agentrank_api.representation.models import (
    MerchantSourceSnapshot,
    MerchantSourceSubmission,
    SourceOrigin,
)
from agentrank_api.representation.schemas import SourceSubmissionRequest

pytestmark = pytest.mark.anyio


def request(body: dict[str, Any], request_key: str) -> SourceSubmissionRequest:
    return SourceSubmissionRequest.model_validate({**body, "request_key": request_key})


async def test_a_submission_continues_the_current_source_line_at_the_next_version(
    session: AsyncSession,
) -> None:
    merchant, first = await merchant_with_source(session, "intake-shop")
    service = MerchantSourceIntakeService(session)

    outcome = await service.submit(
        merchant.id, request_key=FIRST_KEY, document=request(contradicted_document(), FIRST_KEY)
    )

    assert outcome.submission.created_snapshot is True
    assert outcome.submission.origin is SourceOrigin.MERCHANT_CONSOLE
    assert outcome.snapshot.source_key == first.source_key
    assert outcome.snapshot.source_version == first.source_version + 1
    assert outcome.snapshot.merchant_id == merchant.id
    assert outcome.snapshot.id != first.id
    # The identity the browser could not name is the merchant its credential authenticated.
    assert outcome.snapshot.payload["merchant_slug"] == merchant.slug
    assert outcome.snapshot.payload["version"] == first.source_version + 1


async def test_a_merchant_with_no_source_starts_one_at_version_one(
    session: AsyncSession,
) -> None:
    merchant = await bare_merchant(session, "empty-shop")

    outcome = await MerchantSourceIntakeService(session).submit(
        merchant.id, request_key=FIRST_KEY, document=request(voltedge_document(), FIRST_KEY)
    )

    assert outcome.snapshot.source_key == DEFAULT_SOURCE_KEY
    assert outcome.snapshot.source_version == 1
    assert outcome.snapshot.label == f"{DEFAULT_SOURCE_KEY}@1"


async def test_the_same_request_key_twice_is_one_submission_and_one_snapshot(
    session: AsyncSession,
) -> None:
    merchant, _ = await merchant_with_source(session, "retry-shop")
    service = MerchantSourceIntakeService(session)
    body = contradicted_document()

    first = await service.submit(
        merchant.id, request_key=FIRST_KEY, document=request(body, FIRST_KEY)
    )
    again = await service.submit(
        merchant.id, request_key=FIRST_KEY, document=request(body, FIRST_KEY)
    )

    assert again.snapshot.id == first.snapshot.id
    assert again.submission.id == first.submission.id
    assert await _snapshot_count(session, merchant.id) == 2


async def test_a_retry_after_a_lost_response_answers_with_what_it_already_did(
    session: AsyncSession,
) -> None:
    """A repeat carrying a different document under the same key still answers the first one.

    A key names a command rather than a payload. What a lost response needs is the outcome of the
    command that key already ran, and re-reading the body would let a stale tab overwrite it.
    """
    merchant, _ = await merchant_with_source(session, "lost-response-shop")
    service = MerchantSourceIntakeService(session)

    first = await service.submit(
        merchant.id, request_key=FIRST_KEY, document=request(contradicted_document(), FIRST_KEY)
    )
    repeat = await service.submit(
        merchant.id, request_key=FIRST_KEY, document=request(voltedge_document(), FIRST_KEY)
    )

    assert repeat.snapshot.id == first.snapshot.id
    assert repeat.snapshot.payload == first.snapshot.payload
    assert await _snapshot_count(session, merchant.id) == 2


async def test_evidence_identical_to_the_current_snapshot_writes_no_second_copy(
    session: AsyncSession,
) -> None:
    merchant, first = await merchant_with_source(session, "unchanged-shop")

    outcome = await MerchantSourceIntakeService(session).submit(
        merchant.id, request_key=FIRST_KEY, document=request(voltedge_document(), FIRST_KEY)
    )

    assert outcome.submission.created_snapshot is False
    assert outcome.snapshot.id == first.id
    assert await _snapshot_count(session, merchant.id) == 1


async def test_two_tabs_submitting_the_same_evidence_produce_one_snapshot(
    session: AsyncSession,
) -> None:
    """Different keys, identical content. The second is a new command that writes nothing."""
    merchant, _ = await merchant_with_source(session, "two-tab-shop")
    service = MerchantSourceIntakeService(session)
    body = contradicted_document()

    first = await service.submit(
        merchant.id, request_key=FIRST_KEY, document=request(body, FIRST_KEY)
    )
    second = await service.submit(
        merchant.id, request_key=SECOND_KEY, document=request(body, SECOND_KEY)
    )

    assert second.snapshot.id == first.snapshot.id
    assert first.submission.created_snapshot is True
    assert second.submission.created_snapshot is False
    assert second.submission.id != first.submission.id
    assert await _snapshot_count(session, merchant.id) == 2


async def test_reverting_to_older_evidence_is_a_new_snapshot_rather_than_a_no_op(
    session: AsyncSession,
) -> None:
    """Only the current snapshot is compared against. Going back is a real change of evidence."""
    merchant, first = await merchant_with_source(session, "revert-shop")
    service = MerchantSourceIntakeService(session)

    changed = await service.submit(
        merchant.id, request_key=FIRST_KEY, document=request(contradicted_document(), FIRST_KEY)
    )
    reverted = await service.submit(
        merchant.id, request_key=SECOND_KEY, document=request(voltedge_document(), SECOND_KEY)
    )

    assert reverted.submission.created_snapshot is True
    assert reverted.snapshot.id not in {first.id, changed.snapshot.id}
    assert reverted.snapshot.source_version == 3


async def test_two_concurrent_submissions_are_serialized_by_postgresql(
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two different commands saying the same thing at the same time still write one snapshot.

    Independent sessions, so the two genuinely race on the database rather than taking turns on
    one connection. The per-merchant advisory lock is what makes the loser compare against the
    snapshot the winner just wrote instead of against the one they both started from.
    """
    merchant, _ = await merchant_with_source(session, "racing-shop")
    body = contradicted_document()

    async def submit(key: str) -> tuple[str, bool]:
        async with factory() as racing:
            outcome = await MerchantSourceIntakeService(racing).submit(
                merchant.id, request_key=key, document=request(body, key)
            )
            return str(outcome.snapshot.id), outcome.submission.created_snapshot

    first, second = await asyncio.gather(submit(FIRST_KEY), submit(SECOND_KEY))

    assert first[0] == second[0]
    assert sorted([first[1], second[1]]) == [False, True]
    async with factory() as verify:
        assert await _snapshot_count(verify, merchant.id) == 2


async def test_two_concurrent_retries_of_one_command_produce_one_submission(
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant, _ = await merchant_with_source(session, "racing-retry-shop")
    body = contradicted_document()

    async def submit() -> str:
        async with factory() as racing:
            outcome = await MerchantSourceIntakeService(racing).submit(
                merchant.id, request_key=FIRST_KEY, document=request(body, FIRST_KEY)
            )
            return str(outcome.submission.id)

    first, second = await asyncio.gather(submit(), submit())

    assert first == second
    async with factory() as verify:
        assert await _snapshot_count(verify, merchant.id) == 2
        submissions = (
            await verify.execute(
                select(MerchantSourceSubmission).where(
                    MerchantSourceSubmission.merchant_id == merchant.id
                )
            )
        ).scalars()
        assert len(list(submissions)) == 1


async def test_a_submission_never_touches_an_older_snapshot(session: AsyncSession) -> None:
    merchant, first = await merchant_with_source(session, "history-shop")
    before = (first.content_hash, dict(first.payload), first.source_version, first.created_at)

    await MerchantSourceIntakeService(session).submit(
        merchant.id, request_key=FIRST_KEY, document=request(contradicted_document(), FIRST_KEY)
    )

    await session.refresh(first)
    assert (first.content_hash, dict(first.payload), first.source_version, first.created_at) == (
        before
    )


async def test_a_source_snapshot_cannot_be_edited_or_deleted_by_raw_sql(
    session: AsyncSession,
) -> None:
    """The immutability the recovery model rests on is PostgreSQL's, not the service's.

    Plain identifiers rather than ORM instances, because a rollback expires every one of them and
    reading one back afterwards is database IO in a place this test has no greenlet for.
    """
    _, snapshot = await merchant_with_source(session, "raw-sql-shop")
    snapshot_id = snapshot.id

    with pytest.raises(DBAPIError, match="immutable"):
        await session.execute(
            text("UPDATE merchant_source_snapshot SET source_version = 99 WHERE id = :id"),
            {"id": snapshot_id},
        )
    await session.rollback()

    with pytest.raises(DBAPIError, match="immutable"):
        await session.execute(
            text("DELETE FROM merchant_source_snapshot WHERE id = :id"), {"id": snapshot_id}
        )
    await session.rollback()


async def test_a_submission_row_cannot_be_edited_or_deleted_by_raw_sql(
    session: AsyncSession,
) -> None:
    merchant, _ = await merchant_with_source(session, "submission-guard-shop")
    outcome = await MerchantSourceIntakeService(session).submit(
        merchant.id, request_key=FIRST_KEY, document=request(contradicted_document(), FIRST_KEY)
    )

    submission_id = outcome.submission.id

    with pytest.raises(DBAPIError, match="immutable"):
        await session.execute(
            text("UPDATE merchant_source_submission SET created_snapshot = false WHERE id = :id"),
            {"id": submission_id},
        )
    await session.rollback()

    with pytest.raises(DBAPIError, match="immutable"):
        await session.execute(
            text("DELETE FROM merchant_source_submission WHERE id = :id"), {"id": submission_id}
        )
    await session.rollback()


async def test_only_one_submission_may_claim_to_have_created_a_snapshot(
    session: AsyncSession,
) -> None:
    """The partial unique index, exercised directly. The service is not in the path."""
    merchant, _ = await merchant_with_source(session, "one-creator-shop")
    outcome = await MerchantSourceIntakeService(session).submit(
        merchant.id, request_key=FIRST_KEY, document=request(contradicted_document(), FIRST_KEY)
    )

    session.add(
        MerchantSourceSubmission(
            merchant_id=merchant.id,
            request_key=SECOND_KEY,
            source_snapshot_id=outcome.snapshot.id,
            origin=SourceOrigin.MERCHANT_CONSOLE,
            created_snapshot=True,
        )
    )
    with pytest.raises(DBAPIError):
        await session.commit()
    await session.rollback()


async def test_a_submission_cannot_name_another_merchants_snapshot(
    session: AsyncSession,
) -> None:
    merchant, _ = await merchant_with_source(session, "owner-shop")
    _, foreign_snapshot = await merchant_with_source(session, "foreign-shop", name="Foreign")

    session.add(
        MerchantSourceSubmission(
            merchant_id=merchant.id,
            request_key=FIRST_KEY,
            source_snapshot_id=foreign_snapshot.id,
            origin=SourceOrigin.MERCHANT_CONSOLE,
            created_snapshot=False,
        )
    )
    with pytest.raises(DBAPIError):
        await session.commit()
    await session.rollback()


async def test_the_overview_reports_identity_size_and_which_mechanism_supplied_each(
    session: AsyncSession,
) -> None:
    merchant, first = await merchant_with_source(session, "overview-shop")
    service = MerchantSourceIntakeService(session)
    outcome = await service.submit(
        merchant.id, request_key=FIRST_KEY, document=request(contradicted_document(), FIRST_KEY)
    )

    overview = await service.overview(merchant.id)

    assert overview.current_source_snapshot_id == outcome.snapshot.id
    assert [entry.source_snapshot_id for entry in overview.snapshots] == [
        outcome.snapshot.id,
        first.id,
    ]
    newest, oldest = overview.snapshots
    assert newest.origin == "MERCHANT_CONSOLE"
    assert oldest.origin == OPERATOR_ORIGIN
    assert newest.is_current is True
    assert oldest.is_current is False
    assert (newest.product_count, newest.variant_count, newest.policy_count) == (2, 4, 3)
    assert newest.compiler_run_count == 0
    assert newest.published_representation_count == 0


async def test_reading_another_merchants_snapshot_is_indistinguishable_from_an_unknown_one(
    session: AsyncSession,
) -> None:
    merchant, _ = await merchant_with_source(session, "reader-shop")
    _, foreign = await merchant_with_source(session, "other-shop", name="Other")

    with pytest.raises(NotFoundError):
        await MerchantSourceIntakeService(session).snapshot_view(merchant.id, foreign.id)


async def _snapshot_count(session: AsyncSession, merchant_id: Any) -> int:
    rows = (
        await session.execute(
            select(MerchantSourceSnapshot).where(MerchantSourceSnapshot.merchant_id == merchant_id)
        )
    ).scalars()
    return len(list(rows))
