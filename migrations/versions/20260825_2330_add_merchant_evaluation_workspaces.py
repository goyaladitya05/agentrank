"""add merchant evaluation workspaces

A merchant's source snapshot can now be turned into the world and the workload a first
evaluation needs, and that act needs a row of its own. The world is a `benchmark_environment`
and the workload is a `benchmark_suite`; neither is duplicated here. What neither can hold is
that a specific source snapshot, read by a specific generator under a specific configuration,
produced those two artifacts and in that order.

Four properties are the database's rather than the application's:

- a workspace is immutable, by the same trigger every other historical artifact carries. A
  benchmark run points at the world and the workload this row names.
- one workspace per merchant, source snapshot and configuration digest. That is the whole of
  retry, duplicate submit and concurrent bootstrap safety, and it is also what makes a different
  configuration a different workspace rather than a refused one.
- an environment and a suite belong to at most one workspace, so "which evidence produced this
  world" has exactly one answer.
- `write_order` is `GENERATED ALWAYS AS IDENTITY`, so which workspace is a merchant's current
  one is decided by PostgreSQL at INSERT rather than by a clock or by a version 7 UUID drawn in
  two processes at once.

Nothing is backfilled. Every world that existed before this table was authored as files by an
operator, and a row claiming one of those was generated from a source snapshot would be a row
stating something that did not happen.

Revision ID: c8d2e4f6a1b3
Revises: 791257b7c3b3
Created: 2026-08-25 23:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c8d2e4f6a1b3"
down_revision: str | None = "791257b7c3b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "merchant_evaluation_workspace"
KEY_PATTERN = r"^[a-z0-9]+(-[a-z0-9]+)*$"
HASH_PATTERN = r"^sha256:[0-9a-f]{64}$"

GUARD = """
CREATE FUNCTION merchant_evaluation_workspace_guard() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'a merchant evaluation workspace is historical and is immutable';
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("write_order", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("source_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column("suite_id", sa.Uuid(), nullable=False),
        sa.Column("generator_version", sa.String(length=64), nullable=False),
        sa.Column("configuration_digest", sa.String(length=71), nullable=False),
        sa.Column("catalog_hash", sa.String(length=71), nullable=False),
        sa.Column("suite_hash", sa.String(length=71), nullable=False),
        sa.Column("catalog_fixture", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("composition", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("write_order", name="uq_merchant_evaluation_workspace_write_order"),
        sa.UniqueConstraint(
            "merchant_id",
            "source_snapshot_id",
            "configuration_digest",
            name="uq_merchant_evaluation_workspace_identity",
        ),
        sa.UniqueConstraint("environment_id", name="uq_merchant_evaluation_workspace_environment"),
        sa.UniqueConstraint("suite_id", name="uq_merchant_evaluation_workspace_suite"),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchant.id"],
            name="fk_merchant_evaluation_workspace_merchant_id_merchant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_snapshot_id", "merchant_id"],
            ["merchant_source_snapshot.id", "merchant_source_snapshot.merchant_id"],
            name="fk_merchant_evaluation_workspace_source",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["environment_id", "merchant_id"],
            ["benchmark_environment.id", "benchmark_environment.merchant_id"],
            name="fk_merchant_evaluation_workspace_environment",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["suite_id"],
            ["benchmark_suite.id"],
            name="fk_merchant_evaluation_workspace_suite_id_benchmark_suite",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            f"generator_version ~ '{KEY_PATTERN}'",
            name="generator_format",
        ),
        sa.CheckConstraint(
            f"configuration_digest ~ '{HASH_PATTERN}'",
            name="configuration_digest_format",
        ),
        sa.CheckConstraint(
            f"catalog_hash ~ '{HASH_PATTERN}'",
            name="catalog_hash_format",
        ),
        sa.CheckConstraint(
            f"suite_hash ~ '{HASH_PATTERN}'",
            name="suite_hash_format",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(catalog_fixture) = 'object'",
            name="catalog_fixture_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(composition) = 'object'",
            name="composition_object",
        ),
    )
    op.create_index("ix_merchant_evaluation_workspace_merchant_id", TABLE, ["merchant_id"])
    op.create_index(
        "ix_merchant_evaluation_workspace_source_snapshot_id_merchant_id",
        TABLE,
        ["source_snapshot_id", "merchant_id"],
    )
    op.create_index("ix_merchant_evaluation_workspace_suite_id", TABLE, ["suite_id"])
    op.execute(GUARD)
    op.execute(
        f"CREATE TRIGGER merchant_evaluation_workspace_guard BEFORE UPDATE OR DELETE ON {TABLE}"
        " FOR EACH ROW EXECUTE FUNCTION merchant_evaluation_workspace_guard()"
    )


def downgrade() -> None:
    op.execute(f"DROP TRIGGER merchant_evaluation_workspace_guard ON {TABLE}")
    op.execute("DROP FUNCTION merchant_evaluation_workspace_guard()")
    op.drop_index("ix_merchant_evaluation_workspace_suite_id", table_name=TABLE)
    op.drop_index(
        "ix_merchant_evaluation_workspace_source_snapshot_id_merchant_id", table_name=TABLE
    )
    op.drop_index("ix_merchant_evaluation_workspace_merchant_id", table_name=TABLE)
    op.drop_table(TABLE)
