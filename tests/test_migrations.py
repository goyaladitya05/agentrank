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
from sqlalchemy import create_engine, func, inspect, select, text

from agentrank_api.audit.models import ActorType, AuditEvent
from agentrank_api.audit.repository import AuditRepository
from agentrank_api.checkout.models import CheckoutLine, CheckoutSession
from agentrank_api.commerce import models as commerce_models  # noqa: F401  registers tables
from agentrank_api.commerce.dev_catalog import MERCHANT_SLUG, seed_dev_catalog
from agentrank_api.commerce.models import Merchant, Product, Variant
from agentrank_api.config import Settings
from agentrank_api.constraints.repository import IntentConstraintRepository
from agentrank_api.constraints.rules import ConstraintOperator, IntentConstraintSpec
from agentrank_api.database import create_engine as create_async_engine
from agentrank_api.database import create_session_factory
from agentrank_api.mandates.models import SpendingMandate
from agentrank_api.mandates.repository import MandateRepository
from agentrank_api.models import Base

AlembicConfigFactory = Callable[[Settings], Config]

# The last revision of each completed phase. Hardcoded on purpose: these are the points at
# which the database first held a kind of real application data, and every later migration
# has to be applied on top of that data rather than to an empty schema.
PHASE_1A_HEAD = "ace599f8cce9"
PHASE_1B_HEAD = "9360057d8773"
PHASE_1C_HEAD = "4dc1a0f57b18"

HOUR = timedelta(hours=1)
CHECKOUT_ID = uuid.uuid7()


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
    1D migration runs, the database already holds a Phase 1A catalog, a Phase 1B mandate
    and audit event, and a Phase 1C quote with its lines, and every one of them has to come
    through the upgrade, the reversal and the reupgrade unchanged.

    Phase 1D is the first migration to add columns to a table whose rows a trigger refuses
    to update. An ALTER TABLE is not an UPDATE, so the new columns take their default
    without the guard firing, and that is exactly the thing an empty database cannot say
    anything about.
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
            await AuditRepository(session).append(
                merchant_id=merchant_id,
                actor_type=ActorType.BUYER,
                event_type="mandate.created",
                resource_type="spending_mandate",
                resource_id=mandate.id,
                payload={"currency": "INR"},
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

        command.upgrade(config, "head")
        assert catalog_snapshot(throwaway_database) == seeded
        assert authorization_snapshot(throwaway_database) == authorized
        assert quote_snapshot(throwaway_database) == quoted
        assert {table.name for table in Base.metadata.sorted_tables} <= table_names(
            throwaway_database
        )
        # A line written before the snapshot existed takes the column defaults rather than
        # a backfill from today's catalog, and an empty snapshot fails closed.
        assert line_snapshots(throwaway_database) == [(None, {})]

        # Semantic authorization written against that existing mandate, so the downgrade
        # below has rows to remove rather than only tables. The foreign key onto the
        # mandate is RESTRICT and a trigger refuses DELETE, and a DROP TABLE is neither, so
        # neither may block the reversal.
        async with factory() as session:
            await IntentConstraintRepository(session).create(
                merchant_id=merchant_id,
                mandate_id=mandate_id,
                specs=[
                    IntentConstraintSpec.required_attribute("color", ConstraintOperator.EQ, "black")
                ],
            )
            await session.commit()
    finally:
        await engine.dispose()

    # A downgrade removes what this phase added and leaves everything earlier untouched.
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
