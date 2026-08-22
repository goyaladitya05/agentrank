"""pin the benchmark run to its executor

A run already records which missions, which world and which marking rules produced it. It does
not record what did the shopping, and that is the one dimension none of the others can stand in
for: change how the reference executor selects a candidate and every earlier run keeps its
numbers while new runs of the same suite against the same world are produced differently, with
nothing on either to show it.

`executor_kind` and `executor_version` are a declared identity rather than a derived one. A git
commit moves for a comment; a version moves when somebody says the behavior changed, which is the
statement a reader actually needs. There is no model identifier and no provider column beside
them, because neither exists and a column for one would be a guess at the shape of an agent that
has not been built.

Both nullable and both constrained to appear together. A kind without a version names a strategy
nobody can pin down, and a version without a kind names nothing at all. Null in both means the run
predates the columns or was produced by something nobody wrote down, and never that the executor
was the ordinary one.

Both join the run guard's immutable list in the same statement, for the reason every other pin
did: an identity that can be rewritten afterwards is not one.

Revision ID: 7c1f902ab4de
Revises: 3d4b17c9e5aa
Created: 2026-08-22 20:20:44.913022
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7c1f902ab4de"
down_revision: str | None = "3d4b17c9e5aa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KEY_PATTERN = "^[a-z0-9]+(-[a-z0-9]+)*$"

RUN_GUARD = """
CREATE OR REPLACE FUNCTION benchmark_run_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
        OR NEW.merchant_id IS DISTINCT FROM OLD.merchant_id
        OR NEW.suite_id IS DISTINCT FROM OLD.suite_id
        OR NEW.environment_id IS DISTINCT FROM OLD.environment_id
        OR NEW.representation_label IS DISTINCT FROM OLD.representation_label
        OR NEW.catalog_hash IS DISTINCT FROM OLD.catalog_hash
        OR NEW.evaluator_version IS DISTINCT FROM OLD.evaluator_version
        OR NEW.executor_kind IS DISTINCT FROM OLD.executor_kind
        OR NEW.executor_version IS DISTINCT FROM OLD.executor_version
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION 'benchmark run ownership and identity are immutable';
    END IF;

    IF OLD.started_at IS NOT NULL AND NEW.started_at IS DISTINCT FROM OLD.started_at THEN
        RAISE EXCEPTION 'a benchmark run start time cannot be moved';
    END IF;

    IF OLD.status IN ('COMPLETED', 'ABORTED') THEN
        RAISE EXCEPTION 'a % benchmark run cannot be changed', lower(OLD.status);
    END IF;

    IF (OLD.status, NEW.status) NOT IN (
        ('PENDING', 'PENDING'),
        ('PENDING', 'RUNNING'),
        ('RUNNING', 'RUNNING'),
        ('RUNNING', 'COMPLETED'),
        ('RUNNING', 'ABORTED')
    ) THEN
        RAISE EXCEPTION 'benchmark run status cannot go from % to %', OLD.status, NEW.status;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

PREVIOUS_RUN_GUARD = RUN_GUARD.replace(
    "        OR NEW.executor_kind IS DISTINCT FROM OLD.executor_kind\n"
    "        OR NEW.executor_version IS DISTINCT FROM OLD.executor_version\n",
    "",
)


def upgrade() -> None:
    op.add_column("benchmark_run", sa.Column("executor_kind", sa.String(length=64), nullable=True))
    op.add_column("benchmark_run", sa.Column("executor_version", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "executor_identity_shape",
        "benchmark_run",
        "(executor_kind IS NULL) = (executor_version IS NULL)",
    )
    op.create_check_constraint(
        "executor_kind_format",
        "benchmark_run",
        f"executor_kind IS NULL OR executor_kind ~ '{KEY_PATTERN}'",
    )
    op.create_check_constraint(
        "executor_version_positive",
        "benchmark_run",
        "executor_version IS NULL OR executor_version > 0",
    )
    op.execute(RUN_GUARD)


def downgrade() -> None:
    # Dropping the executor identity loses the record of what produced a historical run, which is
    # a real loss and not a reversible one. It is allowed rather than refused for the same reason
    # the other pins allow it: the alternative to a run with no executor identity is no run.
    op.execute(PREVIOUS_RUN_GUARD)
    op.drop_constraint("executor_version_positive", "benchmark_run", type_="check")
    op.drop_constraint("executor_kind_format", "benchmark_run", type_="check")
    op.drop_constraint("executor_identity_shape", "benchmark_run", type_="check")
    op.drop_column("benchmark_run", "executor_version")
    op.drop_column("benchmark_run", "executor_kind")
