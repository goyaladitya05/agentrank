"""pin the benchmark run to its catalog

The suite content hash makes a workload reproducible. It says nothing about the other half of
ground truth, which is the merchant: prices change, stock moves, attributes get published and
products are withdrawn, and none of that leaves a trace on a run. The product this benchmark
exists to support is a before and after comparison, so a difference that cannot be attributed is
the one failure the whole design cannot afford.

`catalog_hash` is what a run is compared across. Two runs whose pins match were measured against
the same merchant. Two whose pins differ were not, and any difference between them is jointly
caused by whatever was changed on purpose and by whatever else moved at the same time.

`evaluator_version` is the same idea for the marking rules. Change the failure vocabulary or the
precedence order and every stored classification keeps the words it was written with while new
runs of the same suite version are marked differently, with nothing on either run to show it.

Both are nullable, and null means a run that predates the column rather than a run that was
fine. Both are added to the run guard's immutable list in the same statement, because a pin that
can be rewritten afterwards is not a pin.

Revision ID: 68ad857681f9
Revises: 08b8b602aa7d
Created: 2026-08-22 15:14:03.771845
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "68ad857681f9"
down_revision: str | None = "08b8b602aa7d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

HASH_PATTERN = "^sha256:[0-9a-f]{64}$"

RUN_GUARD = """
CREATE OR REPLACE FUNCTION benchmark_run_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
        OR NEW.merchant_id IS DISTINCT FROM OLD.merchant_id
        OR NEW.suite_id IS DISTINCT FROM OLD.suite_id
        OR NEW.representation_label IS DISTINCT FROM OLD.representation_label
        OR NEW.catalog_hash IS DISTINCT FROM OLD.catalog_hash
        OR NEW.evaluator_version IS DISTINCT FROM OLD.evaluator_version
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
    "        OR NEW.catalog_hash IS DISTINCT FROM OLD.catalog_hash\n"
    "        OR NEW.evaluator_version IS DISTINCT FROM OLD.evaluator_version\n",
    "",
)


def upgrade() -> None:
    op.add_column("benchmark_run", sa.Column("catalog_hash", sa.String(length=71), nullable=True))
    op.add_column(
        "benchmark_run", sa.Column("evaluator_version", sa.String(length=71), nullable=True)
    )
    op.create_check_constraint(
        "catalog_hash_format",
        "benchmark_run",
        f"catalog_hash IS NULL OR catalog_hash ~ '{HASH_PATTERN}'",
    )
    op.create_check_constraint(
        "evaluator_version_format",
        "benchmark_run",
        f"evaluator_version IS NULL OR evaluator_version ~ '{HASH_PATTERN}'",
    )
    op.execute(RUN_GUARD)


def downgrade() -> None:
    # Dropping the pins loses the record of what a historical run was measured against, which is
    # a real loss and not a reversible one. It is allowed rather than refused because nothing has
    # run a benchmark yet, and because the alternative to a run with no pin is no run at all.
    op.execute(PREVIOUS_RUN_GUARD)
    op.drop_constraint("evaluator_version_format", "benchmark_run", type_="check")
    op.drop_constraint("catalog_hash_format", "benchmark_run", type_="check")
    op.drop_column("benchmark_run", "evaluator_version")
    op.drop_column("benchmark_run", "catalog_hash")
