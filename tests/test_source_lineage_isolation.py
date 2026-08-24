"""What supplying and compiling source evidence provably does not touch.

Two claims, both mechanism tests rather than behavior tests.

The first is that a source is a description and never an authority. A merchant may say their
charger costs one rupee and is in infinite stock; the runtime holds the price, the shelf, the
mandate, the quote, the hold and the payment, and none of them moves. That is asserted by hashing
the actual contents of every money, stock and order table around a real submission and a real
compilation, and by watching which tables the whole workflow's statements name at all.

The second is that the recovery model is historical. A new snapshot, a new run and a new
representation leave the old snapshot, the old run, the old reviews, the old representation and
every benchmark run that measured it byte identical. A recovery path that quietly rewrote history
would make every earlier result mean something else.
"""

import re
from collections.abc import Iterator, Sequence

import pytest
from commerce_support import admit, build_shop, quote
from compiler_support import pending, resolve_every_correction
from source_support import FIRST_KEY, SECOND_KEY, contradicted_document, voltedge_document
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from agentrank_api.compiler.models import ReviewDecision
from agentrank_api.compiler.service import MerchantCompilerService
from agentrank_api.representation.intake import MerchantSourceIntakeService
from agentrank_api.representation.schemas import SourceSubmissionRequest

pytestmark = pytest.mark.anyio

# Every table that holds money, an obligation to a buyer, or held stock. A source document may
# describe any of these and may change none of them, and a table added to this schema without
# being considered here is the failure mode the explicit list exists to make visible.
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

# The only tables supplying evidence and compiling it may write. Everything a benchmark, an
# experiment or an earlier review already recorded lives outside this set.
WRITABLE_TABLES = {
    "merchant_source_snapshot",
    "merchant_source_submission",
    "compiler_run",
    "compiler_candidate",
}

WRITE = re.compile(r"^\s*(INSERT INTO|UPDATE|DELETE FROM)\s+\"?(\w+)", re.IGNORECASE)


@pytest.fixture
def written_tables(catalog_engine: AsyncEngine) -> Iterator[set[str]]:
    """Every table any statement on this engine wrote to, as the table it named."""
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
    """A content digest of every row of each table, ordered so the answer is stable."""
    result: dict[str, str] = {}
    for table in tables:
        # Table names are constants in this module, never caller supplied.
        statement = text(
            "SELECT coalesce(md5(string_agg(row::text, '|' ORDER BY row::text)), 'empty') "  # noqa: S608
            f'FROM (SELECT to_jsonb(t) AS row FROM "{table}" t) rows'
        )
        result[table] = str((await session.execute(statement)).scalar_one())
    return result


def request(body: dict[str, object], request_key: str) -> SourceSubmissionRequest:
    return SourceSubmissionRequest.model_validate({**body, "request_key": request_key})


def contradicting_price_and_stock() -> dict[str, object]:
    """A source that says something about money and stock that the runtime does not agree with.

    The point of the document is that it is wrong on purpose. A source is what the merchant says,
    the commerce runtime is what is true, and a submission that could make the two agree would be
    a submission that had written to the wrong one.
    """
    body = contradicted_document()
    for product in body["products"]:
        for variant in product["variants"]:
            variant["price_amount_minor"] = 1
            variant["inventory_quantity"] = 99999
    return body


async def test_supplying_and_compiling_source_leaves_financial_state_unchanged(
    session: AsyncSession,
) -> None:
    shop = await build_shop(session, "source-money-shop")
    checkout_id = await quote(session, shop)
    await admit(session, shop, checkout_id, key="source-isolation")
    await session.commit()

    populated = await digests(session, FINANCIAL_TABLES)
    # A comparison of empty tables against empty tables proves nothing, so the state this test
    # protects is asserted to exist before it is asserted to be unchanged.
    assert [table for table, value in populated.items() if value == "empty"] == [
        "razorpay_checkout"
    ]
    before = await digests(session, FINANCIAL_TABLES)

    intake = MerchantSourceIntakeService(session)
    outcome = await intake.submit(
        shop.merchant_id,
        request_key=FIRST_KEY,
        document=request(contradicting_price_and_stock(), FIRST_KEY),
    )
    run = await MerchantCompilerService(session).run(shop.merchant_id, outcome.snapshot.id)

    assert run.status.value == "COMPLETED"
    # The compiler read the merchant's claim and carries it as a proposal, which is exactly the
    # separation this test exists for: a proposal about a price is not a price.
    prices = [
        candidate.proposal["fact"]["value"]
        for candidate in await MerchantCompilerService(session).candidates(shop.merchant_id, run.id)
        if candidate.target.endswith(".price")
    ]
    assert prices and all(price["amount_minor"] == 1 for price in prices)
    assert await digests(session, FINANCIAL_TABLES) == before


async def test_supplying_and_compiling_source_writes_only_source_and_compiler_tables(
    session: AsyncSession, written_tables: set[str]
) -> None:
    shop = await build_shop(session, "source-write-scope-shop")
    await session.commit()
    written_tables.clear()

    intake = MerchantSourceIntakeService(session)
    outcome = await intake.submit(
        shop.merchant_id,
        request_key=FIRST_KEY,
        document=request(contradicting_price_and_stock(), FIRST_KEY),
    )
    await intake.submit(
        shop.merchant_id,
        request_key=SECOND_KEY,
        document=request(contradicting_price_and_stock(), SECOND_KEY),
    )
    await MerchantCompilerService(session).run(shop.merchant_id, outcome.snapshot.id)

    assert written_tables <= WRITABLE_TABLES, written_tables - WRITABLE_TABLES
    assert written_tables == WRITABLE_TABLES


async def test_a_second_source_line_leaves_the_first_representation_and_reviews_alone(
    session: AsyncSession,
) -> None:
    """The whole recovery model, asserted as bytes.

    The merchant compiles, reviews, publishes, then supplies newer evidence and compiles again.
    Every row the first pass wrote is unchanged afterwards, and the second representation is a
    second row rather than a replacement.
    """
    shop = await build_shop(session, "recovery-shop")
    await session.commit()
    intake = MerchantSourceIntakeService(session)
    compiler = MerchantCompilerService(session)

    first_snapshot = (
        await intake.submit(
            shop.merchant_id,
            request_key=FIRST_KEY,
            document=request(contradicted_document(), FIRST_KEY),
        )
    ).snapshot
    first_run = await compiler.run(shop.merchant_id, first_snapshot.id)
    candidates = await compiler.candidates(shop.merchant_id, first_run.id)
    assert pending(candidates)
    await resolve_every_correction(session, shop.merchant_id, candidates)
    first_representation = await compiler.publish(shop.merchant_id, first_run.id)
    await session.commit()

    # Plain identifiers, so nothing below reads through an instance a later commit expired.
    first_snapshot_id, first_run_id = first_snapshot.id, first_run.id
    first_representation_id = first_representation.id
    settled = {
        "snapshot": await _row(session, "merchant_source_snapshot", first_snapshot_id),
        "run": await _row(session, "compiler_run", first_run_id),
        "representation": await _row(session, "commerce_representation", first_representation_id),
        "candidates": await _scoped(session, "compiler_candidate", "run_id", first_run_id),
        "reviews": await _scoped(session, "compiler_review", "run_id", first_run_id),
    }
    assert all(value is not None and value != "empty" for value in settled.values())

    second_snapshot = (
        await intake.submit(
            shop.merchant_id,
            request_key=SECOND_KEY,
            document=request(voltedge_document(), SECOND_KEY),
        )
    ).snapshot
    second_run = await compiler.run(shop.merchant_id, second_snapshot.id)
    second_representation = await compiler.publish(shop.merchant_id, second_run.id)
    await session.commit()

    assert second_snapshot.id != first_snapshot_id
    assert second_run.id != first_run_id
    assert second_representation.id != first_representation_id
    assert second_run.source_snapshot_id == second_snapshot.id
    assert second_representation.compiler_run_id == second_run.id
    assert {
        "snapshot": await _row(session, "merchant_source_snapshot", first_snapshot_id),
        "run": await _row(session, "compiler_run", first_run_id),
        "representation": await _row(session, "commerce_representation", first_representation_id),
        "candidates": await _scoped(session, "compiler_candidate", "run_id", first_run_id),
        "reviews": await _scoped(session, "compiler_review", "run_id", first_run_id),
    } == settled


async def test_a_settled_review_is_untouched_by_a_later_source_line(
    session: AsyncSession,
) -> None:
    """A rejected fact stays rejected. The newer run gets its own candidate and its own answer."""
    shop = await build_shop(session, "settled-review-shop")
    await session.commit()
    intake = MerchantSourceIntakeService(session)
    compiler = MerchantCompilerService(session)

    unconfirmed = contradicted_document()
    unconfirmed["products"][1]["description"] = (
        f"{unconfirmed['products'][1]['description']} Supports USB-PD."
    )
    first_snapshot = (
        await intake.submit(
            shop.merchant_id, request_key=FIRST_KEY, document=request(unconfirmed, FIRST_KEY)
        )
    ).snapshot
    first_run = await compiler.run(shop.merchant_id, first_snapshot.id)
    compatibility = [
        candidate
        for candidate in await compiler.candidates(shop.merchant_id, first_run.id)
        if ".compatibility." in candidate.target
    ]
    assert compatibility
    rejected = compatibility[0]
    review = await compiler.review(shop.merchant_id, rejected.id, ReviewDecision.REJECT)
    await session.commit()
    settled = await _row(session, "compiler_review", review.id)

    changed = dict(unconfirmed)
    changed["policy_text"] = {**unconfirmed["policy_text"], "returns": "Returns within ten days."}
    second_snapshot = (
        await intake.submit(
            shop.merchant_id, request_key=SECOND_KEY, document=request(changed, SECOND_KEY)
        )
    ).snapshot
    second_run = await compiler.run(shop.merchant_id, second_snapshot.id)
    successor = [
        candidate
        for candidate in await compiler.candidates(shop.merchant_id, second_run.id)
        if candidate.target == rejected.target
    ]

    assert len(successor) == 1
    assert successor[0].id != rejected.id
    assert successor[0].run_id == second_run.id
    assert await _row(session, "compiler_review", review.id) == settled


async def _row(session: AsyncSession, table: str, row_id: object) -> str | None:
    """One row's exact contents, so a later write to its table cannot hide inside a total."""
    statement = text(
        f'SELECT md5(to_jsonb(t)::text) FROM "{table}" t WHERE t.id = :id'  # noqa: S608
    )
    return (await session.execute(statement, {"id": row_id})).scalar_one_or_none()


async def _scoped(session: AsyncSession, table: str, column: str, value: object) -> str:
    """The exact contents of every row of one table belonging to one owner.

    A whole table digest would change simply because the second pass appended rows to it, which
    says nothing about whether the first pass's rows moved. This scopes the digest to the rows
    that are supposed to be frozen. Table and column names are constants in this module.
    """
    statement = text(
        "SELECT coalesce(md5(string_agg(row::text, '|' ORDER BY row::text)), 'empty') "  # noqa: S608
        f'FROM (SELECT to_jsonb(t) AS row FROM "{table}" t WHERE t."{column}" = :value) rows'
    )
    return str((await session.execute(statement, {"value": value})).scalar_one())
