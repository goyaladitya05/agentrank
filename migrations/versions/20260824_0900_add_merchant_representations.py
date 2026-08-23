"""add immutable merchant source snapshots and Commerce IR

Revision ID: a3c9d7e5f1b2
Revises: f2d9c8b7a6e5
Created: 2026-08-24 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a3c9d7e5f1b2"
down_revision: str | None = "f2d9c8b7a6e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "merchant_source_snapshot",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("source_key", sa.String(length=64), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=71), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "merchant_id", "source_key", "source_version", name="uq_source_version"
        ),
        sa.UniqueConstraint("id", "merchant_id", name="uq_source_snapshot_binding"),
        sa.ForeignKeyConstraint(["merchant_id"], ["merchant.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("source_key ~ '^[a-z0-9]+(-[a-z0-9]+)*$'", name="source_key_format"),
        sa.CheckConstraint("source_version > 0", name="source_version_positive"),
        sa.CheckConstraint("content_hash ~ '^sha256:[0-9a-f]{64}$'", name="content_hash_format"),
        sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name="payload_object"),
    )
    op.create_index(
        "ix_merchant_source_snapshot_merchant_id", "merchant_source_snapshot", ["merchant_id"]
    )
    op.create_table(
        "commerce_representation",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("source_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("producer", sa.String(length=24), nullable=False),
        sa.Column("producer_version", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=71), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("id", "merchant_id", name="uq_commerce_representation_binding"),
        sa.UniqueConstraint(
            "source_snapshot_id", "producer", "producer_version", name="uq_representation_producer"
        ),
        sa.ForeignKeyConstraint(
            ["source_snapshot_id", "merchant_id"],
            ["merchant_source_snapshot.id", "merchant_source_snapshot.merchant_id"],
            name="fk_commerce_representation_source",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("content_hash ~ '^sha256:[0-9a-f]{64}$'", name="content_hash_format"),
        sa.CheckConstraint("producer IN ('MANUAL_FIXTURE', 'COMPILER')", name="producer_known"),
        sa.CheckConstraint(
            "length(btrim(producer_version)) > 0", name="producer_version_not_blank"
        ),
        sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name="payload_object"),
    )
    op.create_index(
        "ix_commerce_representation_merchant_id", "commerce_representation", ["merchant_id"]
    )
    op.create_index(
        "ix_commerce_representation_source_snapshot_id_merchant_id",
        "commerce_representation",
        ["source_snapshot_id", "merchant_id"],
    )
    op.execute("""CREATE FUNCTION merchant_representation_guard() RETURNS trigger AS $$
    BEGIN RAISE EXCEPTION 'published merchant representations are immutable'; END;
    $$ LANGUAGE plpgsql;
    CREATE TRIGGER merchant_source_snapshot_guard BEFORE UPDATE OR DELETE
    ON merchant_source_snapshot
    FOR EACH ROW EXECUTE FUNCTION merchant_representation_guard();
    CREATE TRIGGER commerce_representation_guard BEFORE UPDATE OR DELETE ON commerce_representation
    FOR EACH ROW EXECUTE FUNCTION merchant_representation_guard();""")
    op.add_column("benchmark_run", sa.Column("representation_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_benchmark_run_representation",
        "benchmark_run",
        "commerce_representation",
        ["representation_id", "merchant_id"],
        ["id", "merchant_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_benchmark_run_representation_id_merchant_id",
        "benchmark_run",
        ["representation_id", "merchant_id"],
        postgresql_where=sa.text("representation_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_benchmark_run_representation_id_merchant_id", table_name="benchmark_run")
    op.drop_constraint("fk_benchmark_run_representation", "benchmark_run", type_="foreignkey")
    op.drop_column("benchmark_run", "representation_id")
    op.execute("DROP TRIGGER commerce_representation_guard ON commerce_representation")
    op.execute("DROP TRIGGER merchant_source_snapshot_guard ON merchant_source_snapshot")
    op.execute("DROP FUNCTION merchant_representation_guard()")
    op.drop_index(
        "ix_commerce_representation_source_snapshot_id_merchant_id",
        table_name="commerce_representation",
    )
    op.drop_index("ix_commerce_representation_merchant_id", table_name="commerce_representation")
    op.drop_table("commerce_representation")
    op.drop_index("ix_merchant_source_snapshot_merchant_id", table_name="merchant_source_snapshot")
    op.drop_table("merchant_source_snapshot")
