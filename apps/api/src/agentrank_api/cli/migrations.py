"""Asking whether a schema downgrade is possible, without running one.

One command, and the whole of its value is that it changes nothing. A downgrade that this
database's data cannot be represented under is refused by the migration that would have to write
the impossible thing, which is where the refusal belongs: it holds however the SQL is run. What
it cannot do is warn. By the time the guard is reached, Alembic has already begun unwinding every
revision above it, and although the whole command is one transaction and rolls back completely,
an operator has still watched a long destructive-looking sequence run and abort.

This reads the same declared conditions before anything starts. It opens no transaction, takes no
lock, writes nothing and runs no migration, so it is safe to run against a database somebody else
is using and safe to run in a deployment check.

```bash
uv run python -m agentrank_api.cli migrations downgrade-check base
uv run python -m agentrank_api.cli migrations downgrade-check b6c2e9f4a7d1
```

Exit codes are meant for a script as well as a person: zero when the downgrade is possible as far
as this repository knows, and the ordinary refusal code when a declared condition holds. A zero
is not a promise the downgrade will succeed. It says no condition this repository has written
down is in the way, and a migration is still free to refuse for a reason nobody has declared.
"""

import argparse
from pathlib import Path
from typing import Any, TextIO

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentrank_api.cli.exits import ExitCode
from agentrank_api.cli.output import write_json
from agentrank_api.config import Settings
from agentrank_api.downgrade import blockers_for
from agentrank_api.payments.provider import PaymentProvider

# The repository root, four packages up from this module, which is where `alembic.ini` lives.
REPOSITORY_ROOT = Path(__file__).resolve().parents[5]


def add_commands(parser: argparse.ArgumentParser) -> None:
    """Declare the migration inspection surface."""
    commands = parser.add_subparsers(dest="command_name", required=True)
    checking = commands.add_parser(
        "downgrade-check",
        help="say whether a downgrade to a revision is possible, without running anything",
        description=(
            "Check the conditions this repository has declared to make a downgrade impossible,"
            " for exactly the revisions a downgrade to the named target would unwind. Nothing is"
            " migrated, nothing is locked and nothing is written. A zero exit means no declared"
            " condition is in the way; it is not a promise that the downgrade will succeed."
        ),
    )
    checking.add_argument(
        "target",
        help="the revision to downgrade to, or 'base' to unwind everything",
    )
    checking.add_argument("--json", dest="as_json", action="store_true", help="machine readable")
    checking.set_defaults(command=downgrade_check)


async def downgrade_check(
    session: AsyncSession,
    sessions: async_sessionmaker[AsyncSession],
    provider: PaymentProvider,
    arguments: argparse.Namespace,
    out: TextIO,
    settings: Settings,
) -> int:
    """Report what would stand in the way of a downgrade, before anybody attempts one.

    A synchronous engine of its own rather than the command's async session, because the Alembic
    APIs that resolve a revision graph and read the version table are synchronous and this is the
    one command whose whole subject is those APIs. It is opened and disposed around one read.
    """
    del session, sessions, provider
    config = Config(REPOSITORY_ROOT / "alembic.ini")
    config.attributes["settings"] = settings
    script = ScriptDirectory.from_config(config)
    engine = create_engine(settings.database_url)
    try:
        with engine.connect() as connection:
            current = MigrationContext.configure(connection).get_current_revision()
            unwinding = _unwound(script, current=current, target=arguments.target)
            blocked = blockers_for(connection, unwinding=unwinding)
    finally:
        engine.dispose()

    payload: dict[str, Any] = {
        "current_revision": current,
        "target": arguments.target,
        "revisions_unwound": list(unwinding),
        "possible": not blocked,
        "blockers": [
            {
                "revision": item.revision,
                "code": item.code,
                "reason": item.reason,
                "rows": item.rows,
            }
            for item in blocked
        ],
    }
    if arguments.as_json:
        write_json(out, payload)
        return ExitCode.OK if not blocked else ExitCode.REFUSED

    print(f"current     {current or 'base'}", file=out)
    print(f"target      {arguments.target}", file=out)
    print(f"unwinds     {len(unwinding)} revision(s)", file=out)
    if not blocked:
        print("possible    yes, as far as this repository has declared", file=out)
        return ExitCode.OK
    print("possible    no", file=out)
    for item in blocked:
        print(f"blocked     {item.sentence}", file=out)
    return ExitCode.REFUSED


def _unwound(script: ScriptDirectory, *, current: str | None, target: str) -> tuple[str, ...]:
    """Exactly the revisions a downgrade to `target` would undo, read from the revision graph.

    Empty when the database is already at or below the target, which is the honest answer for a
    command that would move nothing rather than a refusal to describe it.
    """
    if current is None:
        return ()
    lower = "base" if target == "base" else target
    if lower != "base" and script.get_revision(lower).revision == current:
        return ()
    return tuple(revision.revision for revision in script.iterate_revisions(current, lower))
