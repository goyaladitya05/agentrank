"""Shared test fixtures.

Backend tests run against a real PostgreSQL 18 instance, never SQLite. Locally that is
the Docker Compose service, and in CI it is a service container.
"""

import socket
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text

from agentrank_api.config import Settings, get_settings

THROWAWAY_DATABASE = "agentrank_migration_test"


@pytest.fixture(scope="session")
def settings() -> Settings:
    """Configuration for the local or CI database."""
    return get_settings()


@pytest.fixture
def unused_port() -> int:
    """A TCP port with nothing listening on it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def throwaway_database(settings: Settings) -> Iterator[Settings]:
    """Settings pointing at a freshly created empty database, dropped afterwards.

    Migration tests upgrade and downgrade the whole chain, which drops every table. That
    must never run against the developer's working database.
    """
    admin_url = settings.database_url.set(database="postgres")

    def administer(statement: str) -> None:
        engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            with engine.connect() as connection:
                connection.execute(text(statement))
        finally:
            engine.dispose()

    # The database name is a constant defined above, never caller supplied.
    administer(f'DROP DATABASE IF EXISTS "{THROWAWAY_DATABASE}" WITH (FORCE)')
    administer(f'CREATE DATABASE "{THROWAWAY_DATABASE}"')
    try:
        yield settings.model_copy(update={"postgres_db": THROWAWAY_DATABASE})
    finally:
        administer(f'DROP DATABASE IF EXISTS "{THROWAWAY_DATABASE}" WITH (FORCE)')
