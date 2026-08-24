"""What the database refuses, and what a compiler review provably does not touch.

Two separate claims live here and both are mechanism tests rather than behavior tests.

The first is that review and publication lineage is enforced by PostgreSQL and not only by the
service that usually writes it. Every statement here that must be refused is issued as raw SQL
against the real schema, so the service is not in the path and cannot be the reason for the no.

The second is that reviewing and publishing semantic facts changes no financial or runtime truth.
"The compiler does not import payments" is an argument about source code. Two things are asserted
instead: the actual contents of every money, stock and order table are identical before and after
a real accept, correct, reject and publish against a merchant that holds all of them, and the set
of tables the whole workflow writes to is exactly the three it is allowed to write to.
"""

import re
import uuid
from collections.abc import Iterator, Sequence

import pytest
from commerce_support import admit, build_shop, quote
from compiler_support import (
    acceptable_run,
    compile_source,
    conflicting_wattage_source,
    pending,
    resolve_every_correction,
    reviewable_run,
    unconfirmed_compatibility_source,
    wattage_correction,
)
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from agentrank_api.compiler.models import ReviewDecision
from agentrank_api.compiler.service import MerchantCompilerService
from agentrank_api.errors import NotFoundError
from agentrank_api.representation.definitions import RepresentationProducer
from agentrank_api.representation.models import CommerceRepresentation

pytestmark = pytest.mark.anyio

# Every table that holds money, an obligation to a buyer, or held stock. A compiler review is
# allowed to change none of them, and a table added to this schema without being considered here
# is the failure mode the explicit list exists to make visible.
FINANCIAL_TABLES = (
    "spending_mandate",
    "intent_constraint_set",
    "intent_constraint",
    "checkout_session",
    "checkout_line",
    "inventory_reservation",
    "inventory_reservation_line",
    "payment_attempt",
    "razorpay_checkout",
    "product",
    "variant",
    "audit_event",
)

# The only three tables the merchant review workflow may write. Everything a benchmark or an
# experiment already measured lives outside this set, which is what makes publishing a new
# representation incapable of reaching back into older evidence.
WRITABLE_TABLES = {"compiler_review", "compiler_run", "commerce_representation"}

WRITE = re.compile(r"^\s*(INSERT INTO|UPDATE|DELETE FROM)\s+\"?(\w+)", re.IGNORECASE)

INSERT_REVIEW = text(
    "INSERT INTO compiler_review "
    "(id, merchant_id, run_id, candidate_id, decision, correction, reviewer) "
    "VALUES (:id, :merchant, :run, :candidate, 'REJECT', NULL, 'SYSTEM')"
)

CLAIM_REPRESENTATION = text(
    "UPDATE compiler_run SET published_representation_id = :representation WHERE id = :run"
)


@pytest.fixture
def written_tables(catalog_engine: AsyncEngine) -> Iterator[set[str]]:
    """Every table any statement on this engine wrote to, as the table it named.

    Watching statements rather than comparing rows, because the interesting claim is about what
    the code can reach at all. A table that happens to be empty gives a row comparison nothing to
    say; a statement that never mentions it says the same thing for every possible state.
    """
    seen: set[str] = set()

    def record(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: object,
    ) -> None:
        match = WRITE.match(statement)
        if match is not None:
            seen.add(match.group(2).lower())

    event.listen(catalog_engine.sync_engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(catalog_engine.sync_engine, "before_cursor_execute", record)


async def digests(session: AsyncSession, tables: Sequence[str]) -> dict[str, str]:
    """A content digest of every row of each table, ordered so the answer is stable.

    Row counts are not enough. A review that swapped one price for another would keep the count
    and change the truth, so the whole row is hashed, and the ordering comes from the row itself
    rather than from a column each table happens to share.
    """
    result: dict[str, str] = {}
    for table in tables:
        # Table names are constants in this module, never caller supplied.
        statement = text(
            "SELECT coalesce(md5(string_agg(row::text, '|' ORDER BY row::text)), 'empty') "  # noqa: S608
            f'FROM (SELECT to_jsonb(t) AS row FROM "{table}" t) rows'
        )
        result[table] = str((await session.execute(statement)).scalar_one())
    return result


async def row_digest(session: AsyncSession, table: str, row_id: uuid.UUID) -> str:
    """One row's exact contents, so a later write to its table cannot hide inside a total."""
    statement = text(
        f'SELECT md5(to_jsonb(t)::text) FROM "{table}" t WHERE t.id = :id'  # noqa: S608
    )
    return str((await session.execute(statement, {"id": row_id})).scalar_one())


async def test_a_compiler_review_may_not_name_a_run_its_candidate_does_not_belong_to(
    session: AsyncSession,
) -> None:
    merchant, run, candidates = await reviewable_run(session, slug="lineage-shop")
    other_run, _ = await compile_source(
        session, unconfirmed_compatibility_source("lineage-shop", version=7)
    )
    assert other_run.id != run.id
    candidate = pending(candidates)[0]
    # Plain identifiers, because a rollback expires every ORM instance and reading one back
    # afterwards is database IO in a place this test has no greenlet for.
    merchant_id, foreign_run_id = merchant.id, other_run.id
    candidate_id, candidate_run_id = candidate.id, candidate.run_id

    with pytest.raises(DBAPIError, match="compiler review must name its candidate run"):
        await session.execute(
            INSERT_REVIEW,
            {
                "id": uuid.uuid7(),
                "merchant": merchant_id,
                "run": foreign_run_id,
                "candidate": candidate_id,
            },
        )
    await session.rollback()

    # The same statement naming the candidate's own run is accepted, so the refusal above is the
    # lineage guard and not some other constraint refusing the whole shape.
    await session.execute(
        INSERT_REVIEW,
        {
            "id": uuid.uuid7(),
            "merchant": merchant_id,
            "run": candidate_run_id,
            "candidate": candidate_id,
        },
    )
    await session.commit()


async def test_a_compiler_run_may_not_publish_a_representation_from_another_run(
    session: AsyncSession,
) -> None:
    merchant, run, candidates = await reviewable_run(session, slug="publication-shop")
    await resolve_every_correction(session, merchant.id, candidates)
    representation = await MerchantCompilerService(session).publish(merchant.id, run.id)

    unpublished, other_candidates = await compile_source(
        session, unconfirmed_compatibility_source("publication-shop", version=7)
    )
    service = MerchantCompilerService(session)
    for candidate in pending(other_candidates):
        await service.review(merchant.id, candidate.id, ReviewDecision.ACCEPT)
    # Plain identifiers, because a rollback expires every ORM instance and reading one back
    # afterwards is database IO in a place this test has no greenlet for.
    merchant_id = merchant.id
    published_id, source_id = representation.id, representation.source_snapshot_id
    unpublished_id = unpublished.id

    with pytest.raises(DBAPIError, match="must name its own compiler representation"):
        await session.execute(
            CLAIM_REPRESENTATION,
            {"representation": published_id, "run": unpublished_id},
        )
    await session.rollback()

    # Also on the way in. A run inserted already claiming somebody else's representation would
    # otherwise never meet the guard, because it would never be updated.
    with pytest.raises(DBAPIError, match="must name its own compiler representation"):
        await session.execute(
            text(
                "INSERT INTO compiler_run (id, merchant_id, source_snapshot_id, "
                "configuration_digest, configuration, status, published_representation_id) "
                "SELECT :id, merchant_id, source_snapshot_id, :digest, configuration, "
                "'COMPLETED', :representation FROM compiler_run WHERE id = :run"
            ),
            {
                "id": uuid.uuid7(),
                "digest": f"sha256:{'b' * 64}",
                "representation": published_id,
                "run": unpublished_id,
            },
        )
    await session.rollback()

    # A manual fixture representation is not a compiler output, so no run may claim one either.
    manual = CommerceRepresentation(
        merchant_id=merchant_id,
        source_snapshot_id=source_id,
        compiler_run_id=None,
        producer=RepresentationProducer.MANUAL_FIXTURE,
        producer_version="hand-authored",
        content_hash=f"sha256:{'a' * 64}",
        payload={"products": []},
    )
    session.add(manual)
    await session.commit()
    manual_id = manual.id
    with pytest.raises(DBAPIError, match="must name its own compiler representation"):
        await session.execute(
            CLAIM_REPRESENTATION, {"representation": manual_id, "run": unpublished_id}
        )
    await session.rollback()


async def test_an_already_published_representation_survives_a_later_publication(
    session: AsyncSession,
) -> None:
    merchant, run, candidates = await reviewable_run(session, slug="immutable-ir-shop")
    await resolve_every_correction(session, merchant.id, candidates)
    service = MerchantCompilerService(session)
    first = await service.publish(merchant.id, run.id)
    before = await row_digest(session, "commerce_representation", first.id)

    later, later_candidates = await compile_source(
        session, unconfirmed_compatibility_source("immutable-ir-shop", version=7)
    )
    for candidate in pending(later_candidates):
        await service.review(merchant.id, candidate.id, ReviewDecision.ACCEPT)
    second = await service.publish(merchant.id, later.id)

    assert second.id != first.id
    assert second.compiler_run_id == later.id
    assert await row_digest(session, "commerce_representation", first.id) == before


async def test_compiler_review_and_publication_leave_financial_state_unchanged(
    session: AsyncSession,
) -> None:
    shop = await build_shop(session, "compiler-money-shop")
    checkout_id = await quote(session, shop)
    await admit(session, shop, checkout_id, key="compiler-isolation")
    await session.commit()

    populated = await digests(session, FINANCIAL_TABLES)
    # A comparison of empty tables against empty tables proves nothing, so the state this test
    # protects is asserted to exist before it is asserted to be unchanged.
    assert [table for table, value in populated.items() if value == "empty"] == [
        "razorpay_checkout"
    ]

    correcting, correcting_candidates = await compile_source(
        session, conflicting_wattage_source("compiler-money-shop")
    )
    deciding, deciding_candidates = await compile_source(
        session, unconfirmed_compatibility_source("compiler-money-shop")
    )
    before = await digests(session, FINANCIAL_TABLES)

    service = MerchantCompilerService(session)
    for candidate in pending(correcting_candidates):
        await service.review(
            shop.merchant_id,
            candidate.id,
            ReviewDecision.CORRECT,
            correction=wattage_correction(candidate.target),
        )
    accepted, rejected = pending(deciding_candidates)
    await service.review(shop.merchant_id, accepted.id, ReviewDecision.ACCEPT)
    await service.review(shop.merchant_id, rejected.id, ReviewDecision.REJECT)
    await service.publish(shop.merchant_id, correcting.id)
    await service.publish(shop.merchant_id, deciding.id)

    assert await digests(session, FINANCIAL_TABLES) == before


async def test_the_whole_review_workflow_writes_only_compiler_tables(
    session: AsyncSession, written_tables: set[str]
) -> None:
    merchant, run, candidates = await reviewable_run(session, slug="write-scope-shop")
    deciding, deciding_candidates = await compile_source(
        session, unconfirmed_compatibility_source("write-scope-shop")
    )
    written_tables.clear()

    service = MerchantCompilerService(session)
    for candidate in pending(candidates):
        await service.review(
            merchant.id,
            candidate.id,
            ReviewDecision.CORRECT,
            correction=wattage_correction(candidate.target),
        )
    accepted, rejected = pending(deciding_candidates)
    await service.review(merchant.id, accepted.id, ReviewDecision.ACCEPT)
    await service.review(merchant.id, rejected.id, ReviewDecision.REJECT)
    await service.publish(merchant.id, run.id)
    await service.publish(merchant.id, deciding.id)

    assert written_tables <= WRITABLE_TABLES, written_tables - WRITABLE_TABLES
    assert written_tables == WRITABLE_TABLES


async def test_dropping_the_merchant_predicate_is_what_keeps_another_shop_out(
    session: AsyncSession,
) -> None:
    """The ownership predicate is load bearing rather than decorative.

    A candidate identifier is a UUID somebody else can hold. The same identifier answers for its
    owner and refuses everybody else at the service the routes call.

    Measured by mutation rather than asserted by reading. Removing the merchant predicate from
    any single query in the review path leaves this green, because the path locks the candidate
    and then its run and both are scoped: that is defense in depth working. Removing it from all
    of them turns this red, and the review lineage trigger refuses the write underneath.
    """
    owner, _, candidates = await acceptable_run(session, slug="owner-shop")
    intruder, _, _ = await acceptable_run(session, slug="intruder-shop")
    candidate = pending(candidates)[0]
    service = MerchantCompilerService(session)

    with pytest.raises(NotFoundError):
        await service.review(intruder.id, candidate.id, ReviewDecision.ACCEPT)
    review = await service.review(owner.id, candidate.id, ReviewDecision.ACCEPT)
    assert review.merchant_id == owner.id
