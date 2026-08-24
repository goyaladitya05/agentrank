"""The test suite's own databases belong to the process that created them.

Two overlapping test runs against one shared database is not a hypothetical. It happened here:
a full run and a focused run were in flight at once, one suite's teardown `TRUNCATE` raced the
other's live requests, and the symptom was an intermittent 401 that looked like an
authentication defect for a day. The fix was to put the process identifier in the name, and this
is what stops that fix from being quietly reverted.

Nothing here asserts that PostgreSQL keeps two databases apart. It asserts the only part this
repository controls: that two processes never choose the same name, and that the session a test
receives is really on the database that name refers to.
"""

import re
from os import getpid

import pytest
from conftest import CATALOG_DATABASE, THROWAWAY_DATABASE, TRUNCATE_ALL
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.config import Settings
from agentrank_api.models import Base

pytestmark = pytest.mark.anyio

PROCESS_SUFFIXED = re.compile(r"^agentrank_(catalog|migration)_test_\d+$")


def test_every_test_database_name_is_specific_to_this_process() -> None:
    for name in (CATALOG_DATABASE, THROWAWAY_DATABASE):
        assert PROCESS_SUFFIXED.fullmatch(name), name
        assert name.endswith(f"_{getpid()}")
    assert CATALOG_DATABASE != THROWAWAY_DATABASE


def test_the_migration_database_is_never_the_working_database(
    settings: Settings, throwaway_database: Settings
) -> None:
    """Migration tests drop every table, so the one database they must never choose is yours."""
    assert throwaway_database.postgres_db == THROWAWAY_DATABASE
    assert throwaway_database.postgres_db != settings.postgres_db


async def test_a_test_session_runs_against_this_process_own_database(
    session: AsyncSession,
) -> None:
    current = (await session.execute(text("SELECT current_database()"))).scalar_one()
    assert current == CATALOG_DATABASE


def test_cleanup_empties_every_table_this_schema_declares() -> None:
    """A table added without being registered would survive teardown and leak into the next test.

    The statement is built from this repository's own metadata rather than from a list somebody
    has to remember to extend, so this asserts the two cannot drift.
    """
    declared = {table.name for table in Base.metadata.sorted_tables}
    truncated = set(re.findall(r'"(\w+)"', TRUNCATE_ALL))
    assert declared == truncated
    assert "compiler_review" in truncated
