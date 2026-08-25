"""Alembic environment.

Migrations run through a synchronous engine. Alembic's own API is synchronous, and DDL
gains nothing from async, so the async engine used by the application is not reused here.
Both use the psycopg 3 dialect, so the same URL works for either.

The whole command runs inside one transaction, which is what makes a migration that refuses
leave nothing behind: PostgreSQL rolls DDL back like anything else, so a downgrade that hits a
compatibility guard six revisions in undoes the five above it as well. That is load bearing
rather than incidental, and a populated-database test pins it, because switching this to a
transaction per migration would turn a clean refusal into a half-unwound database with no other
symptom.

A downgrade is also checked before any of it runs. Some revisions cannot be reversed while
particular data exists, and while the transaction means an operator loses nothing by finding out
late, finding out late is still a long unwind and an abort where a sentence would have done.
`agentrank_api.downgrade` declares those conditions once; this consults them for exactly the
revisions the command would unwind, and refuses before Alembic touches the schema.
"""

from collections.abc import Sequence

from alembic import context
from alembic.script import ScriptDirectory
from alembic.script.revision import RevisionError
from alembic.util import CommandError
from sqlalchemy import Connection, create_engine, pool

from agentrank_api.config import Settings, get_settings
from agentrank_api.downgrade import ImpossibleDowngradeError, blockers_for

# Importing the registry is what registers every table on Base.metadata. Autogenerate and
# `alembic check` see an empty schema without it.
from agentrank_api.registry import Base

config = context.config
target_metadata = Base.metadata

# Tests inject settings for a throwaway database through config.attributes rather than
# writing a URL, which keeps the password out of any config object.
settings: Settings = config.attributes.get("settings") or get_settings()


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting, for review before a production change."""
    context.configure(
        url=settings.database_url.render_as_string(hide_password=False),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def revisions_unwound(
    script: ScriptDirectory, *, current: str | None, target: str | None
) -> Sequence[str]:
    """Exactly the revisions a downgrade to `target` would undo, or nothing for an upgrade.

    Derived from the revision graph rather than from which Alembic command was typed, because
    the command is not visible from here and the graph answers the question directly: a target
    that is a proper ancestor of where this database is means revisions come off.

    `None` is the target, not the absence of one. Alembic resolves `base` to `None` on the way
    in, so the largest downgrade this repository has arrives here looking like nothing at all,
    and reading it as nothing was exactly the bug this function was written with.

    An empty answer for anything else, including an unrecognised target, because this exists to
    add a refusal and never to invent one. Alembic itself refuses a target it cannot resolve a
    moment later, with a better message than this could produce.
    """
    if current is None:
        return ()
    lower = "base" if target in (None, "base") else target
    try:
        if lower != "base" and script.get_revision(lower).revision == current:
            return ()
        return tuple(revision.revision for revision in script.iterate_revisions(current, lower))
    except CommandError, RevisionError:
        # An unresolvable target, or one this database is not above. Neither is a downgrade,
        # and Alembic refuses the first of them a moment later with a better message.
        return ()


def require_possible_downgrade(connection: Connection) -> None:
    """Refuse a downgrade this database's data cannot be represented under, before it starts.

    `alembic check` runs this environment with no destination at all, because it compares
    metadata with a schema rather than moving one, and asking for an argument that was never set
    raises. That absence is read as "nothing is moving" and returns; it is not the same thing as
    the `None` Alembic resolves `base` to, which is a destination and the largest downgrade
    there is.
    """
    try:
        target = context.get_revision_argument()
    except KeyError:
        return
    unwinding = revisions_unwound(
        ScriptDirectory.from_config(config),
        current=context.get_context().get_current_revision(),
        target=target,
    )
    if not unwinding:
        return
    blocked = blockers_for(connection, unwinding=unwinding)
    if blocked:
        raise ImpossibleDowngradeError(blocked)


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    require_possible_downgrade(connection)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against the configured database."""
    engine = create_engine(settings.database_url, poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            do_run_migrations(connection)
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
