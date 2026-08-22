"""keep a running benchmark run

`benchmark_run_delete_guard` refused deleting a COMPLETED or an ABORTED run, and permitted
deleting a RUNNING one. That is the wrong way round, and an independent database review proved it
end to end: deleting a RUNNING run took its recorded mission results with it through
`ON DELETE CASCADE`, and released the merchant's world claim at the same time.

Both halves matter. A RUNNING run is the state the whole crash story is built on: it means the
executor was called and what it did is unknown, so its world stays claimed until an operator
aborts it and its results are the only evidence of what happened. Deleting it is the one
operation that removes both at once, and it was the one deletion the guard allowed.

The cascade passed for the reason the mission run guard's own comment describes: during
`ON DELETE CASCADE` the parent is already gone, so the child guard sees no run and lets the row
go. That is correct for a legitimate cascade and it means the parent guard has to be the one that
refuses.

PENDING stays deletable. A PENDING run has every mission run recorded and none started, so there
is no evidence to lose and no world claimed, which is why it is the one status where removing a
run costs nothing.

Revision ID: 4d81e0b6fa73
Revises: 9c4a71e2bd08
Created: 2026-08-23 16:10:52.229517
"""

from collections.abc import Sequence

from alembic import op

revision: str = "4d81e0b6fa73"
down_revision: str | None = "9c4a71e2bd08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUN_DELETE_GUARD = """
CREATE OR REPLACE FUNCTION benchmark_run_delete_guard() RETURNS trigger AS $$
BEGIN
    IF OLD.status <> 'PENDING' THEN
        RAISE EXCEPTION 'a benchmark run that has started cannot be deleted';
    END IF;

    RETURN OLD;
END;
$$ LANGUAGE plpgsql
"""

PREVIOUS_RUN_DELETE_GUARD = """
CREATE OR REPLACE FUNCTION benchmark_run_delete_guard() RETURNS trigger AS $$
BEGIN
    IF OLD.status IN ('COMPLETED', 'ABORTED') THEN
        RAISE EXCEPTION 'a finished benchmark run cannot be deleted';
    END IF;

    RETURN OLD;
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    op.execute(RUN_DELETE_GUARD)


def downgrade() -> None:
    # Restoring the narrower guard restores the hole. Allowed because a downgrade has to be a
    # downgrade, and named here so nobody reads the restoration as a decision somebody made.
    op.execute(PREVIOUS_RUN_DELETE_GUARD)
