"""serialize benchmark environment runs

A benchmark run prepares the merchant's world before every mission, so two runs against one
merchant are not two measurements. Process B's preparation, between two of process A's missions,
resets A's shelf and releases what A was holding. Both runs commit, both carry a catalog pin, and
both are quietly wrong with nothing on either to show it.

This is the durable half of closing that. A partial unique index over the merchant, restricted to
runs that are RUNNING, says at most one run may own a merchant's catalog at a time. It holds
across processes because PostgreSQL holds it, and it survives a crash because the row survives a
crash: a process that dies owning a world leaves its run RUNNING and the claim standing, which is
the intended behaviour rather than a leak. What that run did is unknown, and letting the next run
reset the world underneath it would destroy the only evidence of it. `benchmark abort` is the
auditable act that says reuse is safe, and it releases the claim by moving the run out of the
index predicate.

Keyed on the merchant rather than on `environment_id`, which is the stronger of the two. What a
run owns is a catalog and a catalog belongs to a merchant, so two worlds registered against one
merchant are two names for one shelf. `environment_id` is nullable besides, and PostgreSQL treats
nulls in a unique index as distinct, so every run against an unregistered merchant would sit
outside the invariant. Merchants are never serialized against each other: two benchmark worlds
with two merchants run at the same time, which is the point of keying it at all.

Not an advisory lock, and that is the whole reason this is a row predicate. A run lasts as long as
its missions take, which will soon mean model calls and network operations, and a transaction held
open across those is not a claim, it is an outage waiting to happen.

The predicate is static, as a partial index has to be. It is exactly one status: PENDING means
every mission run exists and none has started, so nothing has been touched, and both terminal
statuses mean the run has let go. The lifecycle trigger already refuses every transition except
PENDING to RUNNING to terminal, so a row enters this predicate once and leaves it once.

Revision ID: 2f7b41c98ade
Revises: 5e2a1c4d7b93
Created: 2026-08-23 09:30:12.481907
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2f7b41c98ade"
down_revision: str | None = "5e2a1c4d7b93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACTIVE_RUN_PREDICATE = "status = 'RUNNING'"

# Written before the index exists. A database that already holds two running runs against one
# merchant cannot have the invariant applied to it without a decision, and this migration will not
# make that decision by picking a winner: it refuses, names the merchant, and leaves an operator to
# close the runs that should not be open. Silently aborting somebody's run is exactly the loss of
# evidence the invariant exists to prevent.
EXISTING_CONFLICTS = """
DO $$
DECLARE
    offender uuid;
BEGIN
    SELECT merchant_id INTO offender
    FROM benchmark_run
    WHERE status = 'RUNNING'
    GROUP BY merchant_id
    HAVING count(*) > 1
    LIMIT 1;

    IF offender IS NOT NULL THEN
        RAISE EXCEPTION 'merchant % already has more than one running benchmark run. Close all'
            ' but one with the benchmark abort command before applying this migration', offender;
    END IF;
END $$
"""


def upgrade() -> None:
    op.execute(EXISTING_CONFLICTS)
    op.create_index(
        "uq_benchmark_run_active_merchant",
        "benchmark_run",
        ["merchant_id"],
        unique=True,
        postgresql_where=sa.text(ACTIVE_RUN_PREDICATE),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_benchmark_run_active_merchant",
        table_name="benchmark_run",
        postgresql_where=sa.text(ACTIVE_RUN_PREDICATE),
    )
