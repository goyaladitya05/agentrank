"""Shared test fixtures.

Backend tests run against a real PostgreSQL 18 instance, never SQLite. Locally that is
the Docker Compose service, and in CI it is a service container.
"""

import re
import socket
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import Protocol

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agentrank_api.audit import models as audit_models  # noqa: F401  registers tables
from agentrank_api.auth import models as auth_models  # noqa: F401  registers tables
from agentrank_api.auth.service import MerchantCredentialService
from agentrank_api.auth.tokens import TokenMarker
from agentrank_api.checkout import models as checkout_models  # noqa: F401  registers tables
from agentrank_api.commerce import models as commerce_models  # noqa: F401  registers tables
from agentrank_api.config import Settings, get_settings
from agentrank_api.constraints import models as constraint_models  # noqa: F401  registers tables
from agentrank_api.database import create_engine as create_async_engine
from agentrank_api.database import create_session_factory
from agentrank_api.inventory import models as inventory_models  # noqa: F401  registers tables
from agentrank_api.mandates import models as mandate_models  # noqa: F401  registers tables
from agentrank_api.models import Base
from agentrank_api.payments import models as payment_models  # noqa: F401  registers tables
from agentrank_api.razorpay import models as razorpay_models  # noqa: F401  registers tables

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent

THROWAWAY_DATABASE = "agentrank_migration_test"
CATALOG_DATABASE = "agentrank_catalog_test"

# Table names come from this repository's own metadata, never from a caller.
_TABLE_LIST = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
TRUNCATE_ALL = f"TRUNCATE {_TABLE_LIST} CASCADE"


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """Async tests and async fixtures run on asyncio, matching the application."""
    return "asyncio"


@pytest.fixture(scope="session")
def settings() -> Settings:
    """Configuration for the local or CI database."""
    return get_settings()


@pytest.fixture(scope="session")
def alembic_config_factory() -> Callable[[Settings], Config]:
    """Build an Alembic config pointed at a chosen database."""

    def build(target: Settings) -> Config:
        config = Config(REPOSITORY_ROOT / "alembic.ini")
        config.attributes["settings"] = target
        return config

    return build


def _administer(settings: Settings, statement: str) -> None:
    """Run a statement against the maintenance database in autocommit."""
    engine = create_engine(
        settings.database_url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
    )
    try:
        with engine.connect() as connection:
            connection.execute(text(statement))
    finally:
        engine.dispose()


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
    # The database names are constants defined above, never caller supplied.
    _administer(settings, f'DROP DATABASE IF EXISTS "{THROWAWAY_DATABASE}" WITH (FORCE)')
    _administer(settings, f'CREATE DATABASE "{THROWAWAY_DATABASE}"')
    try:
        yield settings.model_copy(update={"postgres_db": THROWAWAY_DATABASE})
    finally:
        _administer(settings, f'DROP DATABASE IF EXISTS "{THROWAWAY_DATABASE}" WITH (FORCE)')


@pytest.fixture(scope="session")
def catalog_settings(
    settings: Settings, alembic_config_factory: Callable[[Settings], Config]
) -> Iterator[Settings]:
    """A migrated database shared by catalog tests.

    Built once per session and migrated with the real chain, so tests see exactly the
    schema a deployment would have rather than one created by metadata.create_all.
    """
    _administer(settings, f'DROP DATABASE IF EXISTS "{CATALOG_DATABASE}" WITH (FORCE)')
    _administer(settings, f'CREATE DATABASE "{CATALOG_DATABASE}"')
    target = settings.model_copy(update={"postgres_db": CATALOG_DATABASE})
    try:
        command.upgrade(alembic_config_factory(target), "head")
        yield target
    finally:
        _administer(settings, f'DROP DATABASE IF EXISTS "{CATALOG_DATABASE}" WITH (FORCE)')


@pytest.fixture(scope="session")
async def catalog_engine(catalog_settings: Settings) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(catalog_settings)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def session(catalog_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session on the catalog database. Every table is emptied afterwards."""
    factory = create_session_factory(catalog_engine)
    async with factory() as db_session:
        yield db_session

    async with catalog_engine.begin() as connection:
        await connection.execute(text(TRUNCATE_ALL))


@pytest.fixture
def factory(catalog_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Independent sessions on the catalog database, so operations can genuinely race.

    A concurrency test that shares one session proves nothing: two coroutines on one
    connection take their turns because they have to, not because PostgreSQL made them.
    """
    return create_session_factory(catalog_engine)


@pytest.fixture
def row_locks(catalog_engine: AsyncEngine) -> Iterator[list[str]]:
    """Every row lock taken on this engine, as the table each one targeted.

    Deadlock freedom is a property of the order locks are taken in, so the order is the thing
    worth asserting, and the only honest way to assert it is to watch the statements.
    """
    taken: list[str] = []

    def record(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: object,
    ) -> None:
        if "FOR UPDATE" not in statement:
            return
        target = re.search(r"\bFROM\s+(\w+)", statement)
        if target is not None:
            taken.append(target.group(1))

    event.listen(catalog_engine.sync_engine, "before_cursor_execute", record)
    try:
        yield taken
    finally:
        event.remove(catalog_engine.sync_engine, "before_cursor_execute", record)


class CredentialIssuer(Protocol):
    """What `issue_credential` hands back: call it with a merchant, await a raw token.

    A protocol rather than a `Callable` alias because the label is optional, and a callable
    alias cannot express a default argument.
    """

    def __call__(self, merchant_id: uuid.UUID, label: str = ...) -> Awaitable[str]: ...


def bearer(token: str) -> dict[str, str]:
    """The header that presents one merchant API key.

    A function rather than a fixture so that it can be used inside the small helpers each API
    test file already has, and imported by the ones that build a client per test.
    """
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def issue_credential(session: AsyncSession) -> CredentialIssuer:
    """Mint a merchant API key and hand back the raw token.

    Every credential a test uses comes from the real service, so the tokens are real tokens:
    generated with the real generator, stored as the real verifier, and verified by the real
    authentication path. A fixture that fabricated a row would be a fixture that could keep
    passing after authentication broke.

    The secret is synthetic in the only sense that matters, which is that nothing outside the
    test database has ever seen it. It is generated per test rather than written down, so there
    is no fixed development key anywhere in this repository for a scanner to find or for
    somebody to copy into an environment that matters.
    """

    async def issue(merchant_id: uuid.UUID, label: str = "test suite") -> str:
        issued = await MerchantCredentialService(session).issue(
            merchant_id=merchant_id, label=label, marker=TokenMarker.DEVELOPMENT
        )
        return issued.token

    return issue
