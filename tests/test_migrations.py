"""Migrations must round trip against a real PostgreSQL database."""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from benchmark_support import mission as benchmark_mission
from benchmark_support import suite as benchmark_suite
from sqlalchemy import create_engine, func, inspect, select, text

from agentrank_api.audit.models import ActorType, AuditEvent
from agentrank_api.benchmark.definitions import ExpectedOutcome
from agentrank_api.benchmark.models import BenchmarkRunStatus, BenchmarkSuite
from agentrank_api.benchmark.repository import BenchmarkRunRepository, BenchmarkSuiteRepository
from agentrank_api.checkout.models import CheckoutLine, CheckoutSession
from agentrank_api.checkout.quote import QuotedLine
from agentrank_api.checkout.repository import CheckoutRepository
from agentrank_api.commerce import models as commerce_models  # noqa: F401  registers tables
from agentrank_api.commerce.dev_catalog import MERCHANT_SLUG, seed_dev_catalog
from agentrank_api.commerce.models import Merchant, Product, Variant
from agentrank_api.commerce.repository import CatalogRepository, MerchantRepository
from agentrank_api.config import Settings
from agentrank_api.constraints.models import IntentConstraint, IntentConstraintSet
from agentrank_api.constraints.repository import IntentConstraintRepository
from agentrank_api.constraints.rules import ConstraintOperator, IntentConstraintSpec
from agentrank_api.database import create_engine as create_async_engine
from agentrank_api.database import create_session_factory
from agentrank_api.inventory.models import InventoryReservation, InventoryReservationLine
from agentrank_api.inventory.repository import InventoryReservationRepository
from agentrank_api.mandates.models import SpendingMandate
from agentrank_api.mandates.repository import MandateRepository
from agentrank_api.models import Base
from agentrank_api.payments.models import PaymentAttempt, PaymentAttemptStatus
from agentrank_api.payments.repository import PaymentAttemptRepository

AlembicConfigFactory = Callable[[Settings], Config]

# The last revision of each completed phase. Hardcoded on purpose: these are the points at
# which the database first held a kind of real application data, and every later migration
# has to be applied on top of that data rather than to an empty schema.
PHASE_1A_HEAD = "ace599f8cce9"
PHASE_1B_HEAD = "9360057d8773"
PHASE_1C_HEAD = "4dc1a0f57b18"
PHASE_1D_HEAD = "70b5c985a47a"
PHASE_1E_HEAD = "637598637298"
PHASE_1F_HEAD = "ab60fc05d747"
PHASE_1G_HEAD = "4c8de0a1b562"
PHASE_1I_HEAD = "5b3f27ad9e14"
BENCHMARK_DEFINITIONS_HEAD = "a9c07ae31e5e"

HOUR = timedelta(hours=1)
CHECKOUT_ID = uuid.uuid7()
RESERVATION_ID = uuid.uuid7()


def catalog_snapshot(settings: Settings) -> dict[str, Any]:
    """Counts plus a couple of actual values, so an empty table cannot pass as intact."""
    engine = create_engine(settings.database_url)
    try:
        with engine.connect() as connection:
            counts = {
                model.__tablename__: connection.execute(
                    select(func.count()).select_from(model)
                ).scalar_one()
                for model in (Merchant, Product, Variant)
            }
            return counts | {
                "merchant_slug": connection.execute(select(Merchant.slug)).scalars().first(),
                "variant_prices": sorted(
                    connection.execute(select(Variant.price_amount_minor)).scalars().all()
                ),
            }
    finally:
        engine.dispose()


def authorization_snapshot(settings: Settings) -> dict[str, Any]:
    """The Phase 1B rows, which a Phase 1C migration alters the constraints of."""
    engine = create_engine(settings.database_url)
    try:
        with engine.connect() as connection:
            return {
                "mandate_amounts": sorted(
                    connection.execute(select(SpendingMandate.max_total_amount_minor))
                    .scalars()
                    .all()
                ),
                "mandate_statuses": sorted(
                    connection.execute(select(SpendingMandate.status)).scalars().all()
                ),
                "event_types": sorted(
                    connection.execute(select(AuditEvent.event_type)).scalars().all()
                ),
            }
    finally:
        engine.dispose()


def line_snapshots(settings: Settings) -> list[tuple[str | None, dict[str, Any]]]:
    """The semantic snapshot columns a Phase 1D migration adds to checkout_line."""
    engine = create_engine(settings.database_url)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                select(CheckoutLine.product_category, CheckoutLine.variant_attributes)
            ).all()
            return sorted(((row[0], row[1]) for row in rows), key=str)
    finally:
        engine.dispose()


def quote_snapshot(settings: Settings) -> dict[str, Any]:
    """The Phase 1C rows, which a Phase 1D migration adds columns to."""
    engine = create_engine(settings.database_url)
    try:
        with engine.connect() as connection:
            return {
                "checkout_totals": sorted(
                    connection.execute(select(CheckoutSession.total_amount_minor)).scalars().all()
                ),
                "line_unit_prices": sorted(
                    connection.execute(select(CheckoutLine.unit_price_amount_minor)).scalars().all()
                ),
            }
    finally:
        engine.dispose()


def reservation_snapshot(settings: Settings) -> dict[str, Any]:
    """The Phase 1E rows, which the Phase 1F migration adds a column and states to.

    The status is read because the Phase 1F migration rebuilds the partial unique index over
    a wider predicate and rewrites the guard as a whitelist. A reservation written before any
    of that has to come through still ACTIVE and still holding exactly what it held.
    """
    engine = create_engine(settings.database_url)
    try:
        with engine.connect() as connection:
            return {
                "statuses": sorted(
                    connection.execute(select(InventoryReservation.status)).scalars().all()
                ),
                "quantities": sorted(
                    connection.execute(select(InventoryReservationLine.quantity)).scalars().all()
                ),
            }
    finally:
        engine.dispose()


def constraint_snapshot(settings: Settings) -> dict[str, Any]:
    """The Phase 1D rows, which the Phase 1E migration must leave alone."""
    engine = create_engine(settings.database_url)
    try:
        with engine.connect() as connection:
            return {
                "set_count": connection.execute(
                    select(func.count()).select_from(IntentConstraintSet)
                ).scalar_one(),
                "constraints": sorted(
                    connection.execute(
                        select(IntentConstraint.attribute_key, IntentConstraint.value)
                    ).all(),
                    key=str,
                ),
            }
    finally:
        engine.dispose()


def attribution_snapshot(settings: Settings) -> dict[str, int]:
    """How many audit events name a credential, which for historical rows must be none.

    The point of counting both sides is that a migration which backfilled every row and one
    which backfilled none would both leave the table with the same number of rows in it.
    """
    engine = create_engine(settings.database_url)
    try:
        with engine.connect() as connection:
            attributed = connection.execute(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.credential_id.is_not(None))
            ).scalar_one()
            unattributed = connection.execute(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.credential_id.is_(None))
            ).scalar_one()
            return {"attributed": attributed, "unattributed": unattributed}
    finally:
        engine.dispose()


def checkout_status(settings: Settings) -> list[str]:
    """The checkout statuses, which a downgrade must never quietly rewrite."""
    engine = create_engine(settings.database_url)
    try:
        with engine.connect() as connection:
            return sorted(connection.execute(select(CheckoutSession.status)).scalars().all())
    finally:
        engine.dispose()


def row_count(settings: Settings, model: type[Base]) -> int:
    engine = create_engine(settings.database_url)
    try:
        with engine.connect() as connection:
            return int(connection.execute(select(func.count()).select_from(model)).scalar_one())
    finally:
        engine.dispose()


def table_names(settings: Settings) -> set[str]:
    engine = create_engine(settings.database_url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def current_revision(settings: Settings) -> str | None:
    engine = create_engine(settings.database_url)
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()


async def seed_payment_chain(target: Settings) -> uuid.UUID:
    """Everything a payment attempt needs beneath it, written through the ORM at head.

    A merchant, a mandate, a catalog entry, a quote, a hold and one ADMITTED attempt. Written
    with the repositories rather than as SQL, which is safe here and not at the older revisions
    above: the models describe head and this runs at head.

    Returns the attempt identifier, because every test using this then moves that one row into
    the state it wants to meet a downgrade with.
    """
    engine = create_async_engine(target)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            merchant = await MerchantRepository(session).create(slug="downgrade", name="Downgrade")
            mandate = await MandateRepository(session).create(
                merchant_id=merchant.id,
                max_total_amount_minor=250000,
                currency="INR",
                valid_from=datetime.now(UTC) - HOUR,
                valid_until=datetime.now(UTC) + HOUR,
            )
            catalog = CatalogRepository(session)
            product = await catalog.create_product(
                merchant_id=merchant.id, external_id="dg-1", title="Charger", category="chargers"
            )
            variant = await catalog.create_variant(
                product=product,
                sku="DG-1",
                price_amount_minor=250000,
                currency="INR",
                inventory_quantity=3,
                attributes={"color": "black"},
            )
            checkout = await CheckoutRepository(session).create(
                merchant_id=merchant.id,
                mandate_id=mandate.id,
                currency="INR",
                lines=[
                    QuotedLine(
                        variant_id=variant.id,
                        quantity=1,
                        unit_price_amount_minor=250000,
                        product_category="chargers",
                        variant_attributes={"color": "black"},
                    )
                ],
                expires_at=datetime.now(UTC) + HOUR,
            )
            reservation = await InventoryReservationRepository(session).create(
                merchant_id=merchant.id,
                checkout_id=checkout.id,
                expires_at=datetime.now(UTC) + HOUR,
                quantities={variant.id: 1},
            )
            attempt = await PaymentAttemptRepository(session).create(
                merchant_id=merchant.id,
                checkout_id=checkout.id,
                mandate_id=mandate.id,
                reservation_id=reservation.id,
                idempotency_key="pay-downgrade-01",
                amount_minor=checkout.total_amount_minor,
                currency=checkout.currency,
            )
            await session.commit()
            return attempt.id
    finally:
        await engine.dispose()


def execute(target: Settings, statement: str, **parameters: Any) -> None:
    """Run one statement against a migrated database, outside the ORM.

    Used to put a row into a state the application would reach through a service, without
    running the service. The statements are constants in this file and never caller supplied.
    """
    engine = create_engine(target.database_url)
    try:
        with engine.begin() as connection:
            connection.execute(text(statement), parameters)
    finally:
        engine.dispose()


def test_migrations_upgrade_downgrade_and_upgrade_again(
    throwaway_database: Settings, alembic_config_factory: AlembicConfigFactory
) -> None:
    config = alembic_config_factory(throwaway_database)
    head = ScriptDirectory.from_config(config).get_current_head()

    command.upgrade(config, "head")
    assert current_revision(throwaway_database) == head
    # Every table the models declare must actually exist. Asserting against the metadata
    # rather than a hardcoded list means new tables are covered without editing this test.
    expected = {table.name for table in Base.metadata.sorted_tables}
    assert expected <= table_names(throwaway_database)

    command.downgrade(config, "base")
    # Every migration must undo its own schema. Anything left behind other than the
    # version bookkeeping table is a downgrade that does not actually downgrade.
    assert table_names(throwaway_database) - {"alembic_version"} == set()

    command.upgrade(config, "head")
    assert current_revision(throwaway_database) == head
    assert expected <= table_names(throwaway_database)


def test_models_match_migrations(
    throwaway_database: Settings, alembic_config_factory: AlembicConfigFactory
) -> None:
    """A model change that skipped a migration must fail here, not in production."""
    config = alembic_config_factory(throwaway_database)
    command.upgrade(config, "head")

    command.check(config)


@pytest.mark.anyio
async def test_migrations_apply_to_a_database_that_already_holds_data(
    throwaway_database: Settings, alembic_config_factory: AlembicConfigFactory
) -> None:
    """The real risk is not an empty database, it is a populated one.

    The chain is exercised the way a deployment would meet it. Each phase's data is written
    at that phase's head, and only then does the next migration run. By the time the Phase
    1E migration runs, the database already holds a Phase 1A catalog, a Phase 1B mandate
    and audit event, a Phase 1C quote with its lines, and a Phase 1D constraint set, and
    every one of them has to come through the upgrade, the reversal and the reupgrade
    unchanged.

    Phase 1D is the first migration to add columns to a table whose rows a trigger refuses
    to update. An ALTER TABLE is not an UPDATE, so the new columns take their default
    without the guard firing, and that is exactly the thing an empty database cannot say
    anything about.

    Phase 1E adds a unique constraint to that same table and then a table referencing it.
    Building a unique index over rows that already exist is the other operation an empty
    database says nothing about, and the reservation written afterwards is what makes the
    downgrade drop a table with rows in it rather than an empty one.
    """
    config = alembic_config_factory(throwaway_database)
    command.upgrade(config, PHASE_1A_HEAD)

    engine = create_async_engine(throwaway_database)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await seed_dev_catalog(session)
            await session.commit()

        seeded = catalog_snapshot(throwaway_database)
        assert seeded["merchant_slug"] == MERCHANT_SLUG
        assert seeded["variant"] > 0

        # Phase 1B data: an authorization and an audit event on top of that catalog.
        command.upgrade(config, PHASE_1B_HEAD)
        async with factory() as session:
            merchant_id = (await session.execute(select(Merchant.id))).scalars().one()
            mandate = await MandateRepository(session).create(
                merchant_id=merchant_id,
                max_total_amount_minor=750000,
                currency="INR",
                valid_from=datetime.now(UTC),
                valid_until=datetime.now(UTC) + HOUR,
            )
            # SQL rather than the repository, for the reason the checkout line below states:
            # the ORM model always describes head, and at this revision `audit_event` has no
            # `credential_id` column. Writing the row the way the Phase 1B release wrote it is
            # the only way to meet the Phase 1H migration with the data it will actually find,
            # which is an event that predates authentication entirely.
            await session.execute(
                text(
                    "INSERT INTO audit_event (id, merchant_id, actor_type, event_type,"
                    " resource_type, resource_id, payload)"
                    " VALUES (:id, :merchant_id, :actor_type, 'mandate.created',"
                    " 'spending_mandate', :resource_id, :payload)"
                ),
                {
                    "id": uuid.uuid7(),
                    "merchant_id": merchant_id,
                    "actor_type": ActorType.BUYER.value,
                    "resource_id": mandate.id,
                    "payload": '{"currency": "INR"}',
                },
            )
            await session.commit()
            mandate_id = mandate.id

        authorized = authorization_snapshot(throwaway_database)
        assert authorized["mandate_amounts"] == [750000]

        # Phase 1C data: a quote priced from that catalog, against that mandate.
        command.upgrade(config, PHASE_1C_HEAD)
        assert catalog_snapshot(throwaway_database) == seeded
        assert authorization_snapshot(throwaway_database) == authorized

        # Written as SQL rather than through the repository, on purpose. The ORM models
        # always describe head, and at this revision checkout_line has no semantic snapshot
        # columns. Writing the row the way the Phase 1C release wrote it is the only way to
        # meet the next migration with the data it will actually find.
        async with factory() as session:
            variant = (
                (
                    await session.execute(
                        select(Variant).where(
                            Variant.currency == "INR", Variant.is_active.is_(True)
                        )
                    )
                )
                .scalars()
                .first()
            )
            assert variant is not None
            await session.execute(
                text(
                    "INSERT INTO checkout_session (id, merchant_id, mandate_id, currency,"
                    " subtotal_amount_minor, shipping_amount_minor, discount_amount_minor,"
                    " total_amount_minor, status, expires_at)"
                    " VALUES (:id, :merchant_id, :mandate_id, 'INR', :amount, 0, 0,"
                    " :amount, 'OPEN', now() + interval '1 hour')"
                ),
                {
                    "id": CHECKOUT_ID,
                    "merchant_id": merchant_id,
                    "mandate_id": mandate_id,
                    "amount": variant.price_amount_minor,
                },
            )
            await session.execute(
                text(
                    "INSERT INTO checkout_line (id, checkout_id, merchant_id, variant_id,"
                    " quantity, unit_price_amount_minor, currency)"
                    " VALUES (:id, :checkout_id, :merchant_id, :variant_id, 1, :amount,"
                    " 'INR')"
                ),
                {
                    "id": uuid.uuid7(),
                    "checkout_id": CHECKOUT_ID,
                    "merchant_id": merchant_id,
                    "variant_id": variant.id,
                    "amount": variant.price_amount_minor,
                },
            )
            await session.commit()

        quoted = quote_snapshot(throwaway_database)
        assert quoted["checkout_totals"] == [variant.price_amount_minor]

        # Phase 1D data: semantic authorization written against that existing mandate.
        command.upgrade(config, PHASE_1D_HEAD)
        assert catalog_snapshot(throwaway_database) == seeded
        assert authorization_snapshot(throwaway_database) == authorized
        assert quote_snapshot(throwaway_database) == quoted
        # A line written before the snapshot existed takes the column defaults rather than
        # a backfill from today's catalog, and an empty snapshot fails closed.
        assert line_snapshots(throwaway_database) == [(None, {})]

        async with factory() as session:
            await IntentConstraintRepository(session).create(
                merchant_id=merchant_id,
                mandate_id=mandate_id,
                specs=[
                    IntentConstraintSpec.required_attribute("color", ConstraintOperator.EQ, "black")
                ],
            )
            await session.commit()

        constrained = constraint_snapshot(throwaway_database)
        assert constrained["set_count"] == 1

        # Phase 1E data: stock held against that existing quote, so the downgrade below has
        # rows to remove rather than only tables.
        command.upgrade(config, PHASE_1E_HEAD)
        assert catalog_snapshot(throwaway_database) == seeded
        assert authorization_snapshot(throwaway_database) == authorized
        assert quote_snapshot(throwaway_database) == quoted
        assert constraint_snapshot(throwaway_database) == constrained
        assert line_snapshots(throwaway_database) == [(None, {})]
        assert "inventory_reservation" in table_names(throwaway_database)

        # Written as SQL rather than through the repository, for the same reason the quote
        # above was. The ORM models always describe head, and at this revision
        # inventory_reservation has no consumed_at column. Writing the row the way the Phase
        # 1E release wrote it is the only way to meet the next migration with the data it
        # will actually find.
        async with factory() as session:
            line = (await session.execute(select(CheckoutLine))).scalars().one()
            await session.execute(
                text(
                    "INSERT INTO inventory_reservation (id, merchant_id, checkout_id, status,"
                    " expires_at) VALUES (:id, :merchant_id, :checkout_id, 'ACTIVE',"
                    " now() + interval '1 hour')"
                ),
                {
                    "id": RESERVATION_ID,
                    "merchant_id": merchant_id,
                    "checkout_id": CHECKOUT_ID,
                },
            )
            await session.execute(
                text(
                    "INSERT INTO inventory_reservation_line (id, reservation_id, merchant_id,"
                    " variant_id, quantity) VALUES (:id, :reservation_id, :merchant_id,"
                    " :variant_id, :quantity)"
                ),
                {
                    "id": uuid.uuid7(),
                    "reservation_id": RESERVATION_ID,
                    "merchant_id": merchant_id,
                    "variant_id": line.variant_id,
                    "quantity": line.quantity,
                },
            )
            await session.commit()

        assert row_count(throwaway_database, InventoryReservation) == 1
        assert row_count(throwaway_database, InventoryReservationLine) == 1
        held = reservation_snapshot(throwaway_database)
        assert held["statuses"] == ["ACTIVE"]

        # Phase 1F data: a payment admitted against all of it. The migration that runs here
        # is the one that adds statuses to two tables whose rows already exist, rebuilds a
        # partial unique index over a wider predicate, and replaces three triggers. None of
        # those is a thing an empty database can say anything about.
        command.upgrade(config, PHASE_1G_HEAD)
        assert catalog_snapshot(throwaway_database) == seeded
        assert authorization_snapshot(throwaway_database) == authorized
        assert quote_snapshot(throwaway_database) == quoted
        assert constraint_snapshot(throwaway_database) == constrained
        assert line_snapshots(throwaway_database) == [(None, {})]
        # A reservation written before COMMITTED existed comes through ACTIVE, holding the
        # same units, with the new column defaulted rather than backfilled.
        assert reservation_snapshot(throwaway_database) == held

        async with factory() as session:
            checkout = (await session.execute(select(CheckoutSession))).scalars().one()
            attempt = await PaymentAttemptRepository(session).create(
                merchant_id=merchant_id,
                checkout_id=checkout.id,
                mandate_id=mandate_id,
                reservation_id=RESERVATION_ID,
                idempotency_key="pay-migration-01",
                amount_minor=checkout.total_amount_minor,
                currency=checkout.currency,
            )
            await session.commit()
            assert attempt.status is PaymentAttemptStatus.ADMITTED

        assert row_count(throwaway_database, PaymentAttempt) == 1
        unattributed = authorization_snapshot(throwaway_database)

        # Phase 1H data: authentication arrives on a database whose whole history predates it.
        # The audit event written above was recorded when nothing authenticated anybody, and
        # the migration must add the column without inventing a value for it. A backfill here
        # would be manufacturing the exact evidence the column exists to provide.
        command.upgrade(config, "head")
        assert catalog_snapshot(throwaway_database) == seeded
        assert authorization_snapshot(throwaway_database) == unattributed
        assert quote_snapshot(throwaway_database) == quoted
        assert constraint_snapshot(throwaway_database) == constrained
        assert reservation_snapshot(throwaway_database) == held
        assert row_count(throwaway_database, PaymentAttempt) == 1
        assert {table.name for table in Base.metadata.sorted_tables} <= table_names(
            throwaway_database
        )
        assert attribution_snapshot(throwaway_database) == {"attributed": 0, "unattributed": 1}
    finally:
        await engine.dispose()

    # A downgrade removes what this phase added and leaves everything earlier untouched. The
    # payment attempt's foreign keys are RESTRICT and a trigger refuses every update, and a
    # DROP TABLE is neither, so neither may block the reversal. The three guards it replaced
    # go back to the blacklists they were, which is correct again once the statuses they did
    # not cover no longer exist.
    command.downgrade(config, PHASE_1E_HEAD)
    assert catalog_snapshot(throwaway_database) == seeded
    assert authorization_snapshot(throwaway_database) == authorized
    assert quote_snapshot(throwaway_database) == quoted
    assert constraint_snapshot(throwaway_database) == constrained
    assert reservation_snapshot(throwaway_database) == held
    assert "payment_attempt" not in table_names(throwaway_database)

    command.downgrade(config, PHASE_1D_HEAD)
    assert catalog_snapshot(throwaway_database) == seeded
    assert authorization_snapshot(throwaway_database) == authorized
    assert quote_snapshot(throwaway_database) == quoted
    assert constraint_snapshot(throwaway_database) == constrained
    assert "inventory_reservation" not in table_names(throwaway_database)
    assert "inventory_reservation_line" not in table_names(throwaway_database)

    command.downgrade(config, PHASE_1C_HEAD)
    assert catalog_snapshot(throwaway_database) == seeded
    assert authorization_snapshot(throwaway_database) == authorized
    assert quote_snapshot(throwaway_database) == quoted
    assert "intent_constraint_set" not in table_names(throwaway_database)
    assert "intent_constraint" not in table_names(throwaway_database)

    command.upgrade(config, "head")
    assert catalog_snapshot(throwaway_database) == seeded
    assert authorization_snapshot(throwaway_database) == authorized
    assert quote_snapshot(throwaway_database) == quoted
    # The reservation is gone with the table that held it, and the payment attempt with it,
    # which is what a downgrade of these phases means. Everything written before them is
    # still here.
    assert row_count(throwaway_database, InventoryReservation) == 0
    assert row_count(throwaway_database, PaymentAttempt) == 0


@pytest.mark.anyio
async def test_downgrading_past_operator_abandonment_refuses_rather_than_falsifying_it(
    throwaway_database: Settings, alembic_config_factory: AlembicConfigFactory
) -> None:
    """An operator decision about money cannot be represented by a schema that has no word for it.

    The previous constraint allows EXECUTION and RECONCILIATION only. Narrowing back with an
    abandoned attempt present would have to either rewrite the source, which would claim a
    provider answered when none did, or drop the row, which would erase the decision. Both
    falsify financial history, so the migration refuses and says which rows and why.

    The refusal is intentional rather than a constraint violation. The whole run is one
    transaction, so nothing is half applied and the database stays at head.
    """
    config = alembic_config_factory(throwaway_database)
    command.upgrade(config, "head")
    head = ScriptDirectory.from_config(config).get_current_head()
    attempt_id = await seed_payment_chain(throwaway_database)
    execute(
        throwaway_database,
        "UPDATE payment_attempt SET status = 'IN_FLIGHT', dispatched_at = now() WHERE id = :id",
        id=attempt_id,
    )
    execute(
        throwaway_database,
        "UPDATE payment_attempt SET status = 'FAILED', resolved_at = now(),"
        " outcome_source = 'OPERATOR', failure_code = 'OPERATOR_ABANDONED' WHERE id = :id",
        id=attempt_id,
    )

    with pytest.raises(RuntimeError, match="not lossless"):
        command.downgrade(config, PHASE_1F_HEAD)

    assert current_revision(throwaway_database) == head
    assert row_count(throwaway_database, PaymentAttempt) == 1


@pytest.mark.anyio
async def test_downgrading_past_operator_abandonment_succeeds_without_one(
    throwaway_database: Settings, alembic_config_factory: AlembicConfigFactory
) -> None:
    """A provider resolved payment is representable either side, so nothing stands in the way."""
    config = alembic_config_factory(throwaway_database)
    command.upgrade(config, "head")
    attempt_id = await seed_payment_chain(throwaway_database)
    execute(
        throwaway_database,
        "UPDATE payment_attempt SET status = 'IN_FLIGHT', dispatched_at = now() WHERE id = :id",
        id=attempt_id,
    )
    execute(
        throwaway_database,
        "UPDATE payment_attempt SET status = 'FAILED', resolved_at = now(),"
        " outcome_source = 'EXECUTION', failure_code = 'CARD_DECLINED' WHERE id = :id",
        id=attempt_id,
    )

    command.downgrade(config, PHASE_1F_HEAD)

    assert current_revision(throwaway_database) == PHASE_1F_HEAD
    assert row_count(throwaway_database, PaymentAttempt) == 1


@pytest.mark.anyio
async def test_downgrading_past_payments_succeeds_with_only_representable_state(
    throwaway_database: Settings, alembic_config_factory: AlembicConfigFactory
) -> None:
    """A database that was upgraded and never took a payment reverses cleanly.

    The catalog, the mandate, the quote and the hold all exist in the previous schema, so
    nothing here needs a mapping and nothing is refused. This is the case the conditional
    guard must not break.
    """
    config = alembic_config_factory(throwaway_database)
    command.upgrade(config, "head")
    await seed_payment_chain(throwaway_database)
    # The one payment state the previous schema loses without falsifying anything. ADMITTED
    # has provably never reached a provider, so dropping the row loses an authorization rather
    # than a movement of money. The hold beside it is still ACTIVE because nothing committed
    # it, which is what a synthetic row can arrange and a real admission would not.
    execute(throwaway_database, "UPDATE inventory_reservation SET status = 'ACTIVE'")

    command.downgrade(config, PHASE_1E_HEAD)

    assert current_revision(throwaway_database) == PHASE_1E_HEAD
    assert "payment_attempt" not in table_names(throwaway_database)
    assert row_count(throwaway_database, InventoryReservation) == 1


@pytest.mark.anyio
async def test_downgrading_past_a_paid_checkout_refuses_rather_than_calling_it_open(
    throwaway_database: Settings, alembic_config_factory: AlembicConfigFactory
) -> None:
    """PAID has no equivalent in the previous schema, and OPEN is not one.

    Mapping a sale onto OPEN would say the purchase never happened, which is the specific
    falsification this guard exists to refuse. The message names the table and what was found.
    """
    config = alembic_config_factory(throwaway_database)
    command.upgrade(config, "head")
    head = ScriptDirectory.from_config(config).get_current_head()
    attempt_id = await seed_payment_chain(throwaway_database)
    execute(
        throwaway_database,
        "UPDATE payment_attempt SET status = 'IN_FLIGHT', dispatched_at = now() WHERE id = :id",
        id=attempt_id,
    )
    execute(
        throwaway_database,
        "UPDATE payment_attempt SET status = 'SUCCEEDED', resolved_at = now(),"
        " outcome_source = 'EXECUTION', provider_reference = 'paid' WHERE id = :id",
        id=attempt_id,
    )
    execute(throwaway_database, "UPDATE checkout_session SET status = 'PAID', paid_at = now()")
    # Through COMMITTED, because the reservation guard is a transition whitelist and ACTIVE to
    # CONSUMED is not one of the transitions it permits.
    execute(throwaway_database, "UPDATE inventory_reservation SET status = 'COMMITTED'")
    execute(
        throwaway_database,
        "UPDATE inventory_reservation SET status = 'CONSUMED', consumed_at = now()",
    )

    with pytest.raises(RuntimeError) as refused:
        command.downgrade(config, PHASE_1E_HEAD)

    message = str(refused.value)
    assert "not lossless" in message
    assert "checkout_session" in message
    assert "inventory_reservation" in message
    assert "payment_attempt" in message

    # Atomic. The whole run is one transaction, so a refusal leaves the database exactly where
    # it was rather than partway back.
    assert current_revision(throwaway_database) == head
    assert "payment_attempt" in table_names(throwaway_database)
    assert row_count(throwaway_database, PaymentAttempt) == 1
    assert checkout_status(throwaway_database) == ["PAID"]
    assert reservation_snapshot(throwaway_database)["statuses"] == ["CONSUMED"]


@pytest.mark.anyio
async def test_downgrading_past_an_unresolved_payment_refuses(
    throwaway_database: Settings, alembic_config_factory: AlembicConfigFactory
) -> None:
    """The state that matters most, because nobody knows whether money moved.

    An UNKNOWN attempt may have been charged. Dropping it would erase the only record that a
    payment is undecided, and the hold it is bound to would go back to a schema that thinks an
    expiry governs it.
    """
    config = alembic_config_factory(throwaway_database)
    command.upgrade(config, "head")
    head = ScriptDirectory.from_config(config).get_current_head()
    attempt_id = await seed_payment_chain(throwaway_database)
    execute(
        throwaway_database,
        "UPDATE payment_attempt SET status = 'IN_FLIGHT', dispatched_at = now() WHERE id = :id",
        id=attempt_id,
    )
    execute(
        throwaway_database,
        "UPDATE payment_attempt SET status = 'UNKNOWN', outcome_source = 'EXECUTION'"
        " WHERE id = :id",
        id=attempt_id,
    )
    execute(throwaway_database, "UPDATE inventory_reservation SET status = 'COMMITTED'")

    with pytest.raises(RuntimeError, match="not lossless"):
        command.downgrade(config, PHASE_1E_HEAD)

    assert current_revision(throwaway_database) == head
    assert row_count(throwaway_database, PaymentAttempt) == 1


def benchmark_snapshot(settings: Settings) -> dict[str, Any]:
    """Everything about published benchmark definitions that a later migration could move."""
    engine = create_engine(settings.database_url)
    try:
        with engine.connect() as connection:
            suite = connection.execute(
                text(
                    "SELECT suite_key, version, merchant_slug, name, definition_hash"
                    " FROM benchmark_suite"
                )
            ).one()
            missions = connection.execute(
                text(
                    "SELECT mission_key, ordinal, objective, quantity, budget_amount_minor,"
                    " currency, expected_outcome, simulated_value_amount_minor"
                    " FROM benchmark_mission ORDER BY ordinal"
                )
            ).all()
    finally:
        engine.dispose()
    return {"suite": tuple(suite), "missions": [tuple(row) for row in missions]}


def benchmark_run_snapshot(settings: Settings) -> dict[str, Any]:
    engine = create_engine(settings.database_url)
    try:
        with engine.connect() as connection:
            run = connection.execute(
                text("SELECT status, representation_label FROM benchmark_run")
            ).one()
            results = connection.execute(
                text(
                    "SELECT status, primary_failure_reason, unsafe_attempt, unsafe_completion"
                    " FROM benchmark_mission_run"
                    " JOIN benchmark_mission ON benchmark_mission.id ="
                    " benchmark_mission_run.mission_id"
                    " ORDER BY benchmark_mission.ordinal"
                )
            ).all()
    finally:
        engine.dispose()
    return {"run": tuple(run), "results": [tuple(row) for row in results]}


@pytest.mark.anyio
async def test_benchmark_migrations_apply_to_a_database_that_already_holds_data(
    throwaway_database: Settings, alembic_config_factory: AlembicConfigFactory
) -> None:
    """The benchmark tables meet a database that already holds a catalog and payments.

    Two things here cannot be said by an empty database. The run migration adds a unique
    constraint to `payment_attempt`, which means building a unique index over rows that already
    exist. And a published suite has to survive a downgrade of the run tables and then a
    reupgrade, because a historical definition outliving the results that referenced it is the
    whole point of publishing one.
    """
    config = alembic_config_factory(throwaway_database)
    command.upgrade(config, PHASE_1I_HEAD)

    engine = create_async_engine(throwaway_database)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await seed_dev_catalog(session)
            await session.commit()
        seeded = catalog_snapshot(throwaway_database)

        command.upgrade(config, BENCHMARK_DEFINITIONS_HEAD)
        async with factory() as session:
            await BenchmarkSuiteRepository(session).create(
                benchmark_suite(
                    benchmark_mission("buy-a-charger"),
                    benchmark_mission(
                        "nothing-fits", outcome=ExpectedOutcome.NO_ACCEPTABLE_PURCHASE
                    ),
                )
            )
            await session.commit()
        published = benchmark_snapshot(throwaway_database)

        command.upgrade(config, "head")
        # The definitions came through the migration that added the run tables unchanged.
        assert benchmark_snapshot(throwaway_database) == published

        async with factory() as session:
            merchant_id = (await session.execute(select(Merchant.id))).scalars().one()
            suite = await BenchmarkSuiteRepository(session).get("test-suite", 1)
            assert suite is not None
            run = await BenchmarkRunRepository(session).create(
                merchant_id=merchant_id, suite=suite, representation_label="baseline"
            )
            run.status = BenchmarkRunStatus.RUNNING
            run.started_at = datetime.now(UTC)
            await session.commit()
        recorded = benchmark_run_snapshot(throwaway_database)
        assert recorded["run"] == ("RUNNING", "baseline")
        assert len(recorded["results"]) == 2
    finally:
        await engine.dispose()

    # A downgrade of the run tables takes the results with them and leaves the definitions
    # alone, which is what makes a published suite worth publishing.
    command.downgrade(config, BENCHMARK_DEFINITIONS_HEAD)
    assert benchmark_snapshot(throwaway_database) == published
    assert catalog_snapshot(throwaway_database) == seeded
    assert "benchmark_run" not in table_names(throwaway_database)
    assert "benchmark_mission_run" not in table_names(throwaway_database)

    command.downgrade(config, PHASE_1I_HEAD)
    assert catalog_snapshot(throwaway_database) == seeded
    assert "benchmark_suite" not in table_names(throwaway_database)
    assert "benchmark_mission" not in table_names(throwaway_database)

    command.upgrade(config, "head")
    assert catalog_snapshot(throwaway_database) == seeded
    # The definitions are gone with the tables that held them, which is what a downgrade of
    # these phases means. Everything written before them is still here.
    assert row_count(throwaway_database, BenchmarkSuite) == 0
