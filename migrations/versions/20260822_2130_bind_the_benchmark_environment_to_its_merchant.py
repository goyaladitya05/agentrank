"""bind the benchmark environment to its merchant

Preparing a benchmark world overwrites a merchant's catalog and gives back the stock its missions
were holding. Which merchant that is has to be provable, and it was not.

The registration was checked by identifier and the overwrite was performed by name. `merchant.slug`
is mutable, and seeding creates a merchant when the slug it is given does not exist, so renaming a
registered benchmark merchant and taking its old slug was enough to have a benchmark rewrite the
catalog of a shop nobody registered. Found by an independent database review before anything ran
against a real merchant.

`benchmark_environment.merchant_slug` and the composite foreign key onto `(merchant.id,
merchant.slug)` close it at the layer that cannot be bypassed. The slug on the row is provably the
merchant's own, so preparation can read the target off the registration instead of resolving a
name, and a merchant that is a benchmark world can no longer be renamed while it is one. That last
part is the point rather than a side effect.

`uq_merchant_binding` is not a rule but a target. `(id, slug)` is already unique because each
column is, and PostgreSQL requires a unique constraint on exactly the referenced columns.

The environment index on benchmark_run is the other half of the review. The old note said no index
was needed because a registered environment cannot be deleted, which rests an index decision on a
trigger, and `session_replication_role = 'replica'` switches row triggers off during a restore. The
partial index costs nothing and removes the coupling.

Revision ID: 5e2a1c4d7b93
Revises: 7c1f902ab4de
Created: 2026-08-22 21:30:18.664201
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5e2a1c4d7b93"
down_revision: str | None = "7c1f902ab4de"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KEY_PATTERN = "^[a-z0-9]+(-[a-z0-9]+)*$"

# Written before the column is made NOT NULL. Every existing registration has a merchant, and its
# slug is what the row should have carried from the start.
BACKFILL = """
UPDATE benchmark_environment AS environment
SET merchant_slug = merchant.slug
FROM merchant
WHERE merchant.id = environment.merchant_id
"""


def upgrade() -> None:
    op.create_unique_constraint("uq_merchant_binding", "merchant", ["id", "slug"])
    op.add_column(
        "benchmark_environment", sa.Column("merchant_slug", sa.String(length=64), nullable=True)
    )
    op.execute(BACKFILL)
    op.alter_column("benchmark_environment", "merchant_slug", nullable=False)
    op.create_check_constraint(
        "merchant_slug_format",
        "benchmark_environment",
        f"merchant_slug ~ '{KEY_PATTERN}'",
    )
    op.create_foreign_key(
        "fk_benchmark_environment_merchant_binding",
        "benchmark_environment",
        "merchant",
        ["merchant_id", "merchant_slug"],
        ["id", "slug"],
        ondelete="RESTRICT",
    )
    op.create_index(
        op.f("ix_benchmark_run_environment_id_merchant_id"),
        "benchmark_run",
        ["environment_id", "merchant_id"],
        postgresql_where=sa.text("environment_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_benchmark_run_environment_id_merchant_id"),
        table_name="benchmark_run",
        postgresql_where=sa.text("environment_id IS NOT NULL"),
    )
    op.drop_constraint(
        "fk_benchmark_environment_merchant_binding", "benchmark_environment", type_="foreignkey"
    )
    op.drop_constraint("merchant_slug_format", "benchmark_environment", type_="check")
    op.drop_column("benchmark_environment", "merchant_slug")
    op.drop_constraint("uq_merchant_binding", "merchant", type_="unique")
