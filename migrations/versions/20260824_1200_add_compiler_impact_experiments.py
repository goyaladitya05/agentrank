"""add controlled compiler impact experiments

Revision ID: d5e9f1a2b3c4
Revises: c4d8e1f5a2b7
Created: 2026-08-24 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d5e9f1a2b3c4"
down_revision: str | None = "c4d8e1f5a2b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "compiler_impact_experiment",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("suite_id", sa.Uuid(), nullable=False),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column("source_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("compiled_representation_id", sa.Uuid(), nullable=False),
        sa.Column("buyer_configuration_digest", sa.String(length=71), nullable=False),
        sa.Column("buyer_configuration", postgresql.JSONB(), nullable=False),
        sa.Column("methodology", postgresql.JSONB(), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.UniqueConstraint("id", "merchant_id", name="uq_compiler_impact_experiment_binding"),
        sa.ForeignKeyConstraint(["merchant_id"], ["merchant.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["suite_id"], ["benchmark_suite.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["environment_id", "merchant_id"],
            ["benchmark_environment.id", "benchmark_environment.merchant_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_snapshot_id", "merchant_id"],
            ["merchant_source_snapshot.id", "merchant_source_snapshot.merchant_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["compiled_representation_id", "merchant_id"],
            ["commerce_representation.id", "commerce_representation.merchant_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("sample_count > 0 AND sample_count <= 3", name="sample_count_bounded"),
        sa.CheckConstraint(
            "buyer_configuration_digest ~ '^sha256:[0-9a-f]{64}$'", name="buyer_digest_format"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(buyer_configuration) = 'object'",
            name="buyer_configuration_object",
        ),
        sa.CheckConstraint("jsonb_typeof(methodology) = 'object'", name="methodology_object"),
    )
    op.create_index(
        "ix_compiler_impact_experiment_merchant_id",
        "compiler_impact_experiment",
        ["merchant_id"],
    )
    op.create_index(
        "ix_compiler_impact_experiment_environment_id_merchant_id",
        "compiler_impact_experiment",
        ["environment_id", "merchant_id"],
    )
    op.create_table(
        "compiler_impact_sample",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("experiment_id", sa.Uuid(), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("pair_ordinal", sa.Integer(), nullable=False),
        sa.Column("execution_ordinal", sa.Integer(), nullable=False),
        sa.Column("representation_kind", sa.String(length=16), nullable=False),
        sa.Column("source_snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("representation_id", sa.Uuid(), nullable=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.UniqueConstraint(
            "experiment_id",
            "pair_ordinal",
            "representation_kind",
            name="uq_compiler_impact_pair_arm",
        ),
        sa.UniqueConstraint(
            "experiment_id", "execution_ordinal", name="uq_compiler_impact_execution_ordinal"
        ),
        sa.UniqueConstraint("run_id", name="uq_compiler_impact_sample_run"),
        sa.ForeignKeyConstraint(
            ["experiment_id", "merchant_id"],
            ["compiler_impact_experiment.id", "compiler_impact_experiment.merchant_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_snapshot_id", "merchant_id"],
            ["merchant_source_snapshot.id", "merchant_source_snapshot.merchant_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["representation_id", "merchant_id"],
            ["commerce_representation.id", "commerce_representation.merchant_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "merchant_id"],
            ["benchmark_run.id", "benchmark_run.merchant_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("pair_ordinal > 0", name="pair_ordinal_positive"),
        sa.CheckConstraint("execution_ordinal > 0", name="execution_ordinal_positive"),
        sa.CheckConstraint(
            "representation_kind IN ('RAW', 'COMPILED')",
            name="representation_kind_known",
        ),
        sa.CheckConstraint(
            "(representation_kind = 'RAW' AND source_snapshot_id IS NOT NULL"
            " AND representation_id IS NULL) OR (representation_kind = 'COMPILED'"
            " AND source_snapshot_id IS NULL AND representation_id IS NOT NULL)",
            name="representation_identity_shape",
        ),
    )
    op.create_index(
        "ix_compiler_impact_sample_experiment_id_execution_ordinal",
        "compiler_impact_sample",
        ["experiment_id", "execution_ordinal"],
    )
    op.create_index(
        "ix_compiler_impact_sample_merchant_id", "compiler_impact_sample", ["merchant_id"]
    )
    op.execute("""
        CREATE FUNCTION compiler_impact_experiment_guard() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'compiler impact experiments are immutable'; END;
        $$ LANGUAGE plpgsql;
        CREATE FUNCTION compiler_impact_sample_guard() RETURNS trigger AS $$
        BEGIN
          IF TG_OP = 'DELETE' OR OLD.experiment_id <> NEW.experiment_id
             OR OLD.merchant_id <> NEW.merchant_id OR OLD.pair_ordinal <> NEW.pair_ordinal
             OR OLD.execution_ordinal <> NEW.execution_ordinal
             OR OLD.representation_kind <> NEW.representation_kind
             OR OLD.source_snapshot_id IS DISTINCT FROM NEW.source_snapshot_id
             OR OLD.representation_id IS DISTINCT FROM NEW.representation_id
          THEN RAISE EXCEPTION 'compiler impact sample identity is immutable'; END IF;
          IF OLD.run_id IS NULL AND NEW.run_id IS NOT NULL THEN RETURN NEW; END IF;
          RAISE EXCEPTION 'compiler impact sample run binding is immutable';
        END;
        $$ LANGUAGE plpgsql;
        CREATE FUNCTION compiler_impact_sample_insert_guard() RETURNS trigger AS $$
        DECLARE experiment record;
        BEGIN
          SELECT * INTO experiment FROM compiler_impact_experiment WHERE id = NEW.experiment_id;
          IF NOT FOUND OR experiment.merchant_id <> NEW.merchant_id THEN
            RAISE EXCEPTION 'compiler impact sample experiment binding is invalid';
          END IF;
          IF NEW.pair_ordinal > experiment.sample_count OR NEW.execution_ordinal <> (
             CASE NEW.representation_kind WHEN 'RAW' THEN (NEW.pair_ordinal * 2 - 1)
             WHEN 'COMPILED' THEN (NEW.pair_ordinal * 2) END
          ) THEN
            RAISE EXCEPTION 'compiler impact sample is outside its experiment plan';
          END IF;
          IF NEW.representation_kind = 'RAW'
             AND NEW.source_snapshot_id <> experiment.source_snapshot_id THEN
            RAISE EXCEPTION 'compiler impact raw sample source does not match experiment';
          END IF;
          IF NEW.representation_kind = 'COMPILED'
             AND NEW.representation_id <> experiment.compiled_representation_id THEN
            RAISE EXCEPTION 'compiler impact compiled sample does not match experiment';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER compiler_impact_experiment_guard BEFORE UPDATE OR DELETE
        ON compiler_impact_experiment FOR EACH ROW
        EXECUTE FUNCTION compiler_impact_experiment_guard();
        CREATE TRIGGER compiler_impact_sample_guard BEFORE UPDATE OR DELETE
        ON compiler_impact_sample FOR EACH ROW EXECUTE FUNCTION compiler_impact_sample_guard();
        CREATE TRIGGER compiler_impact_sample_insert_guard BEFORE INSERT
        ON compiler_impact_sample FOR EACH ROW
        EXECUTE FUNCTION compiler_impact_sample_insert_guard();
    """)


def downgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM compiler_impact_experiment) THEN
            RAISE EXCEPTION 'cannot downgrade while compiler impact experiment evidence exists';
          END IF;
        END;
        $$;
    """)
    op.execute("DROP TRIGGER IF EXISTS compiler_impact_sample_guard ON compiler_impact_sample")
    op.execute(
        "DROP TRIGGER IF EXISTS compiler_impact_sample_insert_guard ON compiler_impact_sample"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS compiler_impact_experiment_guard ON compiler_impact_experiment"
    )
    op.execute("DROP FUNCTION IF EXISTS compiler_impact_sample_guard()")
    op.execute("DROP FUNCTION IF EXISTS compiler_impact_sample_insert_guard()")
    op.execute("DROP FUNCTION IF EXISTS compiler_impact_experiment_guard()")
    op.drop_index("ix_compiler_impact_sample_merchant_id", table_name="compiler_impact_sample")
    op.drop_index(
        "ix_compiler_impact_sample_experiment_id_execution_ordinal",
        table_name="compiler_impact_sample",
    )
    op.drop_table("compiler_impact_sample")
    op.execute("DROP INDEX IF EXISTS ix_compiler_impact_experiment_environment_id_merchant_id")
    op.drop_index(
        "ix_compiler_impact_experiment_merchant_id", table_name="compiler_impact_experiment"
    )
    op.drop_table("compiler_impact_experiment")
