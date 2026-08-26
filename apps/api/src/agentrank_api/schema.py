"""Which schema this build expects, and which one the database is actually at.

A private-beta deployment applies migrations as an explicit step and then starts processes, so
the window this exists for is real: a process started against a database that has not been
migrated yet, or one migrated past what this build knows about. Both produce failures on the
first request that touches the wrong table, which is a bad place to learn about a deploy that ran
out of order.

The expected revision is a constant here rather than something read from the migrations directory
at runtime. A process should not have to be able to find `migrations/` to know what schema it was
built against, and a deployment that ships only the application package would otherwise have no
answer at all. The obvious hazard of a constant, that somebody adds a migration and forgets this,
is closed by `tests/test_migrations.py`, which fails when it does not equal the Alembic head.

A revision identifier is not a secret. It is a build fact, it appears in migration filenames and
in the repository history, and reporting it is what makes a readiness probe useful during a
deploy rather than merely truthful.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

# The Alembic head this build was written against. Every migration updates it, and a test refuses
# a build where it disagrees with the migration chain.
EXPECTED_REVISION = "e5b7c93af142"


async def applied_revision(engine: AsyncEngine) -> str | None:
    """The migration revision this database is at, or None when it has never been migrated.

    None rather than an exception for a missing table, because "not migrated yet" is an ordinary
    state during a deploy and one a readiness probe has to be able to report rather than crash
    on. Anything else the driver raises propagates: a database that cannot answer this is a
    database that cannot answer anything, and the caller already handles that.

    More than one row is an answer rather than a row to pick from. Alembic writes one per head,
    so two rows is a branched history or a hand stamped database, and taking either of them
    would let a probe report compatible while an unknown head is also applied, and would let two
    consecutive probes disagree with no state change behind them. What comes back names the
    count so a reader learns what is actually wrong.
    """
    async with engine.connect() as connection:
        present = await connection.execute(text("SELECT to_regclass('alembic_version')"))
        if present.scalar_one() is None:
            return None
        found = await connection.execute(
            text("SELECT version_num FROM alembic_version ORDER BY version_num")
        )
        revisions: list[str] = [str(revision) for revision in found.scalars().all()]
        if not revisions:
            return None
        if len(revisions) > 1:
            return f"{len(revisions)} revisions applied"
        return revisions[0]
