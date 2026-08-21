"""Migrations must round trip against a real PostgreSQL database."""

from collections.abc import Callable
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, func, inspect, select

from agentrank_api.commerce import models as commerce_models  # noqa: F401  registers tables
from agentrank_api.commerce.dev_catalog import MERCHANT_SLUG, seed_dev_catalog
from agentrank_api.commerce.models import Merchant, Product, Variant
from agentrank_api.config import Settings
from agentrank_api.database import create_engine as create_async_engine
from agentrank_api.database import create_session_factory
from agentrank_api.models import Base

AlembicConfigFactory = Callable[[Settings], Config]

# The last revision of Phase 1A. Hardcoded on purpose: this is the point at which the
# database first held real application data, and everything after it has to be applied on
# top of that data rather than to an empty schema.
PHASE_1A_HEAD = "ace599f8cce9"


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
async def test_a_migration_applies_to_a_database_that_already_holds_catalog_data(
    throwaway_database: Settings, alembic_config_factory: AlembicConfigFactory
) -> None:
    """The real risk is not an empty database, it is a populated one.

    Phase 1B is the first migration applied on top of existing application data, so the
    chain is exercised the way a deployment would: upgrade to the last Phase 1A revision,
    seed the development catalog, then move forward, back and forward again with that
    data in place.
    """
    config = alembic_config_factory(throwaway_database)
    command.upgrade(config, PHASE_1A_HEAD)

    engine = create_async_engine(throwaway_database)
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            await seed_dev_catalog(session)
            await session.commit()
    finally:
        await engine.dispose()

    seeded = catalog_snapshot(throwaway_database)
    assert seeded["merchant_slug"] == MERCHANT_SLUG
    assert seeded["variant"] > 0

    command.upgrade(config, "head")
    assert catalog_snapshot(throwaway_database) == seeded
    assert {table.name for table in Base.metadata.sorted_tables} <= table_names(throwaway_database)

    # A downgrade removes what this phase added and leaves the catalog untouched.
    command.downgrade(config, PHASE_1A_HEAD)
    assert catalog_snapshot(throwaway_database) == seeded
    assert "spending_mandate" not in table_names(throwaway_database)

    command.upgrade(config, "head")
    assert catalog_snapshot(throwaway_database) == seeded
