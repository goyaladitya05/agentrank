"""Migrations must round trip against a real PostgreSQL database."""

from collections.abc import Callable

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from agentrank_api.commerce import models as commerce_models  # noqa: F401  registers tables
from agentrank_api.config import Settings
from agentrank_api.models import Base

AlembicConfigFactory = Callable[[Settings], Config]


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
