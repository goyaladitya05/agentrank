"""let the database say which source snapshot and which representation are current

"Current" has been decided by ordering on the primary key, which is a version 7 UUID generated
in Python at insert. That reads as time ordering and is not one. CPython's `uuid7` is monotonic
inside a single process; two processes generating one in the same millisecond share the
timestamp and draw independent random counters, so their relative order is a coin flip. A
second API process, or the operator command line running beside one, is enough to reach it.

Being wrong here is not cosmetic. `current()` is what the next submission compares its evidence
against, so an inverted answer writes a duplicate snapshot as a new version. The published
Commerce IR read the same way is what a re-evaluation measures, so an inverted answer measures
the representation the merchant replaced.

`write_order` replaces the coincidence with a fact. It is `GENERATED ALWAYS AS IDENTITY`, so
PostgreSQL assigns it at INSERT and no application code can supply, override or backdate one.
Two inserts from any two processes take two values in the order the inserts reached the
database, which is exactly the relation these reads have always been trying to express.

`created_at` was never a candidate and the reason is worth writing down where the column is
defined: it defaults to `now()`, which PostgreSQL evaluates as `transaction_timestamp()`, so a
transaction that began earlier and committed later carries the earlier timestamp. Two
submissions serializing on the per-merchant advisory lock are precisely that shape.

The sequence behind an identity column is allocated with `CACHE 1`, which is PostgreSQL's
default and is what makes values ordered across sessions rather than merely unique. Nothing
here raises it, and raising it later would hand each backend a private block and reintroduce
the defect this migration removes.

Existing rows are backfilled in primary key order. That is the ordering these tables have been
read with until now, so no merchant's history changes meaning as a result of this migration;
it only stops being able to change meaning later.

Revision ID: c8d3f1a6b204
Revises: b6c2e9f4a7d1
Created: 2026-08-25 21:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c8d3f1a6b204"
down_revision: str | None = "b6c2e9f4a7d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The tables whose newest row is a semantic answer rather than a listing order, each with the
# immutability trigger that refuses UPDATE on it. Both tables are append only and enforce it in
# the database, which is why the backfill below has to suspend the guard and put it back: the one
# statement that may ever write this column is the one that introduces it.
#
# That guard is also what makes `write_order` permanent. No later UPDATE can move a row's place
# in the history, because no later UPDATE is possible at all.
TABLES: tuple[tuple[str, str], ...] = (
    ("merchant_source_snapshot", "merchant_source_snapshot_guard"),
    ("commerce_representation", "commerce_representation_guard"),
)

COLUMN = "write_order"


def upgrade() -> None:
    # The statements below compose a table name into SQL, which is what S608 is for and which a
    # bound parameter cannot do: an identifier is not a value. Every name comes from `TABLES`
    # above, which is a literal in this file, so there is no input here for anybody to inject
    # through. Suppressed per statement rather than for the module, so a later statement that
    # does take a value has to argue for itself.
    for table, guard in TABLES:
        # Added nullable and filled first, because an identity column cannot be attached to a
        # column that still holds a NULL, and because the backfill order has to be chosen here
        # rather than left to whatever order a table rewrite happens to visit rows in.
        op.add_column(table, sa.Column(COLUMN, sa.BigInteger(), nullable=True))
        op.execute(sa.text(f"ALTER TABLE {table} DISABLE TRIGGER {guard}"))
        op.execute(
            sa.text(
                f"UPDATE {table} AS target SET {COLUMN} = ordered.position"  # noqa: S608
                f" FROM (SELECT id, row_number() OVER (ORDER BY id) AS position"
                f" FROM {table}) AS ordered"
                " WHERE target.id = ordered.id"
            )
        )
        op.execute(sa.text(f"ALTER TABLE {table} ENABLE TRIGGER {guard}"))
        op.alter_column(table, COLUMN, nullable=False)
        op.execute(
            sa.text(f"ALTER TABLE {table} ALTER COLUMN {COLUMN} ADD GENERATED ALWAYS AS IDENTITY")
        )
        # The identity starts at 1 regardless of what the backfill wrote, so the next insert
        # would collide with a backfilled row. Restarted past the highest value there is.
        op.execute(
            sa.text(
                f"SELECT setval(pg_get_serial_sequence('{table}', '{COLUMN}'),"  # noqa: S608
                f" coalesce((SELECT max({COLUMN}) FROM {table}), 0) + 1, false)"
            )
        )
        op.create_unique_constraint(op.f(f"uq_{table}_{COLUMN}"), table, [COLUMN])


def downgrade() -> None:
    for table, _ in reversed(TABLES):
        op.drop_constraint(op.f(f"uq_{table}_{COLUMN}"), table, type_="unique")
        op.drop_column(table, COLUMN)
