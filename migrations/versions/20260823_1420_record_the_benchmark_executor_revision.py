"""record the benchmark executor revision

A run records which strategy did its shopping and which version of it, and both are declared. A
declaration is a promise a person keeps, and the failure it cannot catch is the one nobody meant:
edit how a candidate is selected, leave the version alone, and every later run stamps
`reference-v1` while buying something different. Nothing on either run would show it.

`executor_revision` is a labelled digest of the source of the modules that decide one executor's
behaviour, computed at import time rather than declared. It moves whether or not anybody
remembers to, so two runs of one suite produced by different code can be told apart afterwards.

It is not automatic semantic versioning and nothing here claims it is. A digest says two runs came
from different code; it cannot say the behaviour changed, and it moves for a comment. That is why
it sits beside the declared version rather than replacing it: the version is the statement a
reader needs, and this is the evidence beside it.

Nullable, and null means nobody recorded one rather than that nothing changed, which is what
every other nullable pin on this table means. It requires a kind, because a revision with no
strategy beside it names nothing, and a kind without a revision is the ordinary case: a strategy
may have no derivable one.

It joins the run guard's immutable list in the same statement, for the reason every other pin did.
An identity that can be rewritten afterwards is not one.

Revision ID: 9c4a71e2bd08
Revises: 2f7b41c98ade
Created: 2026-08-23 14:20:07.113244
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9c4a71e2bd08"
down_revision: str | None = "2f7b41c98ade"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

HASH_PATTERN = "^sha256:[0-9a-f]{64}$"

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
        OR NEW.executor_revision IS DISTINCT FROM OLD.executor_revision
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
    "        OR NEW.executor_revision IS DISTINCT FROM OLD.executor_revision\n", ""
)


def upgrade() -> None:
    op.add_column(
        "benchmark_run", sa.Column("executor_revision", sa.String(length=71), nullable=True)
    )
    op.create_check_constraint(
        "executor_revision_needs_a_kind",
        "benchmark_run",
        "executor_revision IS NULL OR executor_kind IS NOT NULL",
    )
    op.create_check_constraint(
        "executor_revision_format",
        "benchmark_run",
        f"executor_revision IS NULL OR executor_revision ~ '{HASH_PATTERN}'",
    )
    op.execute(RUN_GUARD)


def downgrade() -> None:
    # Dropping the revision loses the one record of what code produced a historical run that
    # nobody had to remember to write. Allowed rather than refused for the same reason the other
    # pins allow it: the alternative to a run with no revision is no run.
    op.execute(PREVIOUS_RUN_GUARD)
    op.drop_constraint("executor_revision_format", "benchmark_run", type_="check")
    op.drop_constraint("executor_revision_needs_a_kind", "benchmark_run", type_="check")
    op.drop_column("benchmark_run", "executor_revision")
