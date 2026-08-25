"""Which snapshot and which representation are current, decided by PostgreSQL rather than luck.

The reads that answer "current" used to order by the primary key, which is a version 7 UUID
generated in Python. That is monotonic inside one process and a coin flip between two, and the
answer is not cosmetic: `current()` is what the next submission compares its evidence against,
and the current published Commerce IR is what a re-evaluation measures.

These tests are about the ordering authority itself, so they are written against the two things
that could distort it rather than against the console flow. Every one of them uses independent
PostgreSQL sessions from `factory`, because two coroutines sharing one connection take turns
because they must, not because the database made them.
"""

import asyncio
import uuid

import pytest
from launch_support import build_launch_world, without_providers
from source_support import voltedge_document
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentrank_api.benchmark.launch import MerchantEvaluationLaunchService
from agentrank_api.commerce.models import Merchant
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.config import Settings
from agentrank_api.diagnostics.service import DiagnosticsService
from agentrank_api.representation.definitions import MerchantSourceDefinition
from agentrank_api.representation.fixtures import parse_source
from agentrank_api.representation.intake import MerchantSourceIntakeService
from agentrank_api.representation.models import CommerceRepresentation, MerchantSourceSnapshot
from agentrank_api.representation.repository import MerchantSourceRepository
from agentrank_api.representation.schemas import SourceDocumentInput

pytestmark = pytest.mark.anyio


async def _merchant(session: AsyncSession, slug: str) -> Merchant:
    merchant = await MerchantRepository(session).create(slug=slug, name="Ordering Shop")
    await session.commit()
    return merchant


def _definition(slug: str, version: int, *, marker: str) -> MerchantSourceDefinition:
    """One source definition whose content differs from every other version by `marker`."""
    body = voltedge_document()
    products = [{**body["products"][0], "description": f"ordering probe {marker}"}]
    return parse_source(
        {
            "key": "merchant-source",
            "version": version,
            "merchant_slug": slug,
            "products": [*products, *body["products"][1:]],
            "policy_text": body["policy_text"],
        }
    )


async def _insert(
    session: AsyncSession, merchant: Merchant, definition: MerchantSourceDefinition
) -> MerchantSourceSnapshot:
    snapshot = await MerchantSourceRepository(session).create(merchant, definition)
    await session.commit()
    return snapshot


async def test_the_row_written_second_is_current_even_when_its_transaction_began_first(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Transaction start time cannot invert history.

    `created_at` is `transaction_timestamp()`, so the snapshot written second here carries the
    earlier timestamp. This is not a contrived shape: it is exactly what two submissions
    serializing on the per-merchant advisory lock produce, because the waiter's transaction
    began before the holder's row was written.
    """
    merchant = await _merchant(session, "ordering-transaction-time")

    async with factory() as early, factory() as late:
        # `early` opens its transaction first, which is what fixes its `transaction_timestamp`.
        await early.execute(text("SELECT 1"))
        await asyncio.sleep(0.05)
        second = await _insert(late, merchant, _definition(merchant.slug, 1, marker="written-1st"))
        first = await _insert(early, merchant, _definition(merchant.slug, 2, marker="written-2nd"))

    current = await MerchantSourceIntakeService(session).current(merchant.id)
    assert current is not None
    assert current.id == first.id, "the snapshot written last must be current"
    assert first.created_at < second.created_at, "the shape under test is a timestamp inversion"
    assert first.write_order > second.write_order


async def test_a_smaller_identifier_written_later_is_still_current(session: AsyncSession) -> None:
    """UUID generation order is not semantic history.

    The identifiers are chosen so the row written second sorts first by primary key. Under the
    ordering this replaces, the older snapshot would have been reported as current and the next
    submission would have compared its evidence against it.
    """
    merchant = await _merchant(session, "ordering-identifier")

    older = MerchantSourceSnapshot(
        id=uuid.UUID("ffffffff-ffff-7fff-8fff-ffffffffffff"),
        merchant_id=merchant.id,
        source_key="merchant-source",
        source_version=1,
        content_hash="sha256:" + "a" * 64,
        payload={"products": [], "policy_text": {}},
    )
    session.add(older)
    await session.commit()

    newer = MerchantSourceSnapshot(
        id=uuid.UUID("00000000-0000-7000-8000-000000000000"),
        merchant_id=merchant.id,
        source_key="merchant-source",
        source_version=2,
        content_hash="sha256:" + "b" * 64,
        payload={"products": [], "policy_text": {}},
    )
    session.add(newer)
    await session.commit()

    current = await MerchantSourceIntakeService(session).current(merchant.id)
    assert current is not None
    assert current.id == newer.id
    assert newer.id < older.id, "the shape under test is an identifier inversion"


async def test_write_order_cannot_be_supplied_by_a_writer(session: AsyncSession) -> None:
    """`GENERATED ALWAYS` means no INSERT anywhere decides where a row sits in history."""
    merchant = await _merchant(session, "ordering-always-generated")
    snapshot = await _insert(session, merchant, _definition(merchant.slug, 1, marker="only"))
    # Plain values: the rollback below expires every loaded instance, and reading an attribute
    # off one afterwards is database IO in a place with no greenlet for it.
    snapshot_id, written = snapshot.id, snapshot.write_order

    with pytest.raises(Exception) as refused:
        await session.execute(
            text(
                "INSERT INTO merchant_source_snapshot"
                " (id, write_order, merchant_id, source_key, source_version, content_hash,"
                " payload)"
                " VALUES (:id, 1, :merchant, 'merchant-source', 2, :hash, '{}'::jsonb)"
            ),
            {"id": uuid.uuid7(), "merchant": merchant.id, "hash": "sha256:" + "c" * 64},
        )
    await session.rollback()
    assert "write_order" in str(refused.value)

    reread = (
        await session.execute(
            select(MerchantSourceSnapshot).where(MerchantSourceSnapshot.id == snapshot_id)
        )
    ).scalar_one()
    assert reread.write_order == written


async def test_concurrent_submissions_from_independent_sessions_produce_one_history(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Two API processes submitting at once leave two versions and one deterministic current.

    Both submissions carry different evidence and different request keys, so both are genuine
    new snapshots rather than either idempotency rule collapsing them. What must hold is that
    the two agree on an order: consecutive versions, distinct `write_order`, and a `current()`
    that names the higher of the two.
    """
    merchant = await _merchant(session, "ordering-concurrent")
    body = voltedge_document()

    async def submit(marker: str) -> uuid.UUID:
        async with factory() as own:
            document = {
                **body,
                "products": [
                    {**body["products"][0], "description": f"concurrent probe {marker}"},
                    *body["products"][1:],
                ],
            }
            outcome = await MerchantSourceIntakeService(own).submit(
                merchant.id,
                request_key=f"concurrent-{marker}",
                document=SourceDocumentInput.model_validate(document),
            )
            return outcome.snapshot.id

    first, second = await asyncio.gather(submit("alpha"), submit("beta"))
    assert first != second

    rows = list(
        (
            await session.execute(
                select(MerchantSourceSnapshot)
                .where(MerchantSourceSnapshot.merchant_id == merchant.id)
                .order_by(MerchantSourceSnapshot.write_order)
            )
        )
        .scalars()
        .all()
    )
    assert [row.source_version for row in rows] == [1, 2]
    assert rows[0].write_order < rows[1].write_order

    current = await MerchantSourceIntakeService(session).current(merchant.id)
    assert current is not None
    assert current.id == rows[1].id
    # Read again through an independent session: the answer is a database fact, not a fact about
    # whichever process happened to write it.
    async with factory() as elsewhere:
        from_elsewhere = await MerchantSourceIntakeService(elsewhere).current(merchant.id)
    assert from_elsewhere is not None
    assert from_elsewhere.id == current.id


async def test_repeated_submissions_still_collapse_to_one_snapshot(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Both idempotency rules survive the ordering change.

    The same request key twice is one submission and one snapshot. Identical evidence under a
    second key resolves to the current snapshot rather than writing a copy of it. Neither of
    those is what this change touched, and both are what would break first if `current()` had
    started answering with the wrong row.
    """
    merchant = await _merchant(session, "ordering-idempotent")
    document = SourceDocumentInput.model_validate(voltedge_document())

    async def submit(request_key: str) -> tuple[uuid.UUID, bool]:
        async with factory() as own:
            outcome = await MerchantSourceIntakeService(own).submit(
                merchant.id, request_key=request_key, document=document
            )
            return outcome.snapshot.id, outcome.submission.created_snapshot

    created_id, created = await submit("repeat-key")
    assert created is True

    same_key_id, same_key_created = await submit("repeat-key")
    assert same_key_id == created_id
    assert same_key_created is True, "the original outcome is replayed, not re-decided"

    same_content_id, same_content_created = await submit("different-key")
    assert same_content_id == created_id
    assert same_content_created is False

    count = (
        await session.execute(
            select(MerchantSourceSnapshot).where(MerchantSourceSnapshot.merchant_id == merchant.id)
        )
    ).scalars()
    assert len(list(count)) == 1


async def test_the_published_representation_a_launch_measures_is_the_one_written_last(
    session: AsyncSession, settings: Settings
) -> None:
    """The current Commerce IR is decided the same way, and it decides what gets measured.

    A merchant who publishes twice has two compiler representations, and a re-evaluation must
    measure the second. The identifiers here are forced so that the newer row sorts first by
    primary key, which is what two console processes publishing in the same millisecond can
    produce on their own.
    """
    world = await build_launch_world(session, "ordering-representation")
    published = (
        await session.execute(
            select(CommerceRepresentation).where(
                CommerceRepresentation.id == world.representation_id
            )
        )
    ).scalar_one()

    superseding = CommerceRepresentation(
        id=uuid.UUID("00000000-0000-7000-8000-000000000000"),
        merchant_id=published.merchant_id,
        source_snapshot_id=published.source_snapshot_id,
        compiler_run_id=published.compiler_run_id,
        producer=published.producer,
        producer_version=published.producer_version + "-later",
        content_hash=published.content_hash,
        payload=published.payload,
    )
    session.add(superseding)
    await session.commit()
    assert superseding.id < world.representation_id, (
        "the shape under test is an identifier inversion"
    )

    plan = await MerchantEvaluationLaunchService(session, without_providers(settings)).plan(
        world.merchant_id
    )
    assert plan.representation_id == superseding.id

    overview = await DiagnosticsService(session).merchant_overview(world.merchant_id)
    assert overview.representation_state.compiled_representation_id == superseding.id
