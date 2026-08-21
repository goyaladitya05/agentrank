"""Database engine construction and connectivity checks."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from agentrank_api.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    """Build the async engine for the application.

    pool_pre_ping is on because connections are routinely idle between benchmark runs
    and a stale connection should be discarded rather than surfaced as a request error.
    """
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": settings.postgres_connect_timeout},
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build the session factory for the application.

    expire_on_commit is off so that objects stay readable after a commit. Without it,
    serializing a just committed row triggers a fresh SELECT per attribute.
    """
    return async_sessionmaker(engine, expire_on_commit=False)


async def check_connection(engine: AsyncEngine) -> None:
    """Raise if PostgreSQL cannot be reached."""
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
