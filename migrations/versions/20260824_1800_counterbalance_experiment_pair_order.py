"""counterbalance compiler impact experiment pair order

Revision ID: e6f0a3b4c5d8
Revises: d5e9f1a2b3c4
Created: 2026-08-24 18:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e6f0a3b4c5d8"
down_revision: str | None = "d5e9f1a2b3c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION compiler_impact_sample_insert_guard() RETURNS trigger AS $$
        DECLARE experiment record; expected_kind text; first_slot boolean;
        BEGIN
          SELECT * INTO experiment FROM compiler_impact_experiment WHERE id = NEW.experiment_id;
          IF NOT FOUND OR experiment.merchant_id <> NEW.merchant_id THEN
            RAISE EXCEPTION 'compiler impact sample experiment binding is invalid';
          END IF;
          first_slot := NEW.execution_ordinal = NEW.pair_ordinal * 2 - 1;
          IF COALESCE(experiment.methodology->>'pair_order', 'raw_then_compiled')
             = 'counterbalanced' AND NEW.pair_ordinal % 2 = 0 THEN
            expected_kind := CASE WHEN first_slot THEN 'COMPILED' ELSE 'RAW' END;
          ELSE
            expected_kind := CASE WHEN first_slot THEN 'RAW' ELSE 'COMPILED' END;
          END IF;
          IF NEW.pair_ordinal > experiment.sample_count OR NEW.representation_kind <> expected_kind
             OR NEW.execution_ordinal NOT IN (NEW.pair_ordinal * 2 - 1, NEW.pair_ordinal * 2) THEN
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
    """)


def downgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM compiler_impact_experiment
            WHERE methodology->>'pair_order' = 'counterbalanced'
          ) THEN
            RAISE EXCEPTION
              'cannot downgrade while counterbalanced experiment evidence exists';
          END IF;
        END;
        $$;
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION compiler_impact_sample_insert_guard() RETURNS trigger AS $$
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
    """)
