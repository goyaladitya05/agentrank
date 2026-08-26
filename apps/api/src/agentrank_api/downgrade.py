"""Refusing an impossible schema downgrade before Alembic starts unwinding one.

Some migrations cannot be reversed while particular data exists, and the honest thing for one of
those to do is refuse. `benchmark_evaluation_launch` is the standing example: the revision that
gave a launch a purpose cannot be undone while an initial evaluation exists, because the older
shape requires a representation and a compiler run on every launch and there is no way to write
one that is true. Inventing them would claim a merchant measured a document that never existed,
and deleting the row would erase the command behind a benchmark run. So the migration raises.

That refusal is correct and this module does not weaken it. What it adds is timing.

The refusal lives inside a downgrade step, so it is reached only after Alembic has already begun
unwinding every revision above it. Those steps are undone when the transaction rolls back, and
a test written for this phase proves that on a populated database: a `downgrade base` against a
schema holding an initial evaluation leaves every table, every row and the recorded revision
exactly as they were. Atomicity is a property of `migrations/env.py` running the whole command in
one transaction and of PostgreSQL's transactional DDL, and it is now pinned by a test rather than
assumed, because a future `transaction_per_migration` would silently turn a clean refusal into a
half-unwound database.

What atomicity does not give is warning. An operator who runs a downgrade that cannot succeed
watches a long sequence of destructive-looking steps run and then abort, and learns why only at
the end. So the same conditions are declared here as data, checked against the database before
Alembic touches anything, and reported by an operator command that runs no migration at all.

One declaration, two readers. `migrations/env.py` consults it at the start of every downgrade,
and `agentrank_api.cli migrations` consults it on demand. The in-migration guard stays exactly
where it is: it is what makes the refusal true no matter who runs the SQL, and this is what makes
it visible before anybody does.
"""

from collections.abc import Collection, Sequence
from dataclasses import dataclass

from sqlalchemy import Connection, inspect, text


@dataclass(frozen=True, slots=True)
class DowngradeBlocker:
    """One condition under which a revision's downgrade is known to be impossible.

    `revision` is the migration whose own guard would refuse. `probe` counts the rows that
    cannot be represented by the revision below it, and `table` is what the probe reads: a
    database that has not reached that revision has no such table, and a preflight that assumed
    otherwise would fail on the schema rather than report on the data.
    """

    revision: str
    code: str
    table: str
    probe: str
    reason: str


# Every downgrade this repository knows to be impossible under some data, and nothing it merely
# suspects. A blocker declared here must correspond to a refusal the migration itself makes, or
# this becomes a second opinion that can disagree with the thing that actually runs.
BLOCKERS: tuple[DowngradeBlocker, ...] = (
    DowngradeBlocker(
        revision="b6c2e9f4a7d1",
        code="initial_evaluation_launches",
        table="benchmark_evaluation_launch",
        probe="SELECT count(*) FROM benchmark_evaluation_launch WHERE purpose = 'INITIAL'",
        reason=(
            "initial evaluation launches cannot be represented before this revision, which"
            " requires a representation and a compiler run on every launch"
        ),
    ),
    DowngradeBlocker(
        revision="e5b7c93af142",
        code="merchant_source_imports",
        table="merchant_source_import",
        probe="SELECT count(*) FROM merchant_source_import",
        reason=(
            "merchant source imports record which public pages produced which source snapshot,"
            " and nothing below this revision can hold that provenance"
        ),
    ),
    DowngradeBlocker(
        revision="e5b7c93af142",
        code="imported_source_submissions",
        table="merchant_source_submission",
        probe="SELECT count(*) FROM merchant_source_submission WHERE origin = 'MERCHANT_IMPORT'",
        reason=(
            "imported source submissions cannot be described before this revision, which is the"
            " one that admits an origin other than the console"
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class BlockedDowngrade:
    """One reason a downgrade would be refused, and how much data it is refused over."""

    revision: str
    code: str
    reason: str
    rows: int

    @property
    def sentence(self) -> str:
        return f"{self.rows} row(s) at revision {self.revision}: {self.reason}"


def blockers_for(connection: Connection, *, unwinding: Collection[str]) -> list[BlockedDowngrade]:
    """Which declared blockers actually hold, for the revisions a downgrade would unwind.

    Read only. Nothing here writes, locks or migrates, so running it is never the thing that
    changes a database's mind about whether the downgrade is possible.
    """
    tables = set(inspect(connection).get_table_names())
    found: list[BlockedDowngrade] = []
    for blocker in BLOCKERS:
        if blocker.revision not in unwinding or blocker.table not in tables:
            continue
        rows = connection.execute(text(blocker.probe)).scalar_one()
        if rows:
            found.append(
                BlockedDowngrade(
                    revision=blocker.revision,
                    code=blocker.code,
                    reason=blocker.reason,
                    rows=int(rows),
                )
            )
    return found


class ImpossibleDowngradeError(RuntimeError):
    """A downgrade this database's own data cannot be represented under.

    Raised before Alembic runs anything, so an operator reads the reason instead of watching a
    long unwind abort. The message names the revisions and the row counts and nothing else: a
    refusal about a merchant's evaluations is not a place to print a merchant's evaluations.
    """

    def __init__(self, blocked: Sequence[BlockedDowngrade]) -> None:
        listed = "; ".join(item.sentence for item in blocked)
        super().__init__(
            f"this downgrade is refused before any migration runs, because {listed}."
            " Nothing has been changed"
        )
        self.blocked = tuple(blocked)
