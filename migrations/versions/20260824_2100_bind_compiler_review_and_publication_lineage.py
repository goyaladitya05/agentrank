"""bind compiler review and published representation lineage

Revision ID: b8c2d4e6f0a1
Revises: f7a1b4c5d6e9
Created: 2026-08-24 21:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b8c2d4e6f0a1"
down_revision: str | None = "f7a1b4c5d6e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""CREATE FUNCTION compiler_review_run_binding_guard() RETURNS trigger AS $$
    BEGIN
      IF NOT EXISTS (
        SELECT 1 FROM compiler_candidate
        WHERE id = NEW.candidate_id AND merchant_id = NEW.merchant_id AND run_id = NEW.run_id
      ) THEN
        RAISE EXCEPTION 'compiler review must name its candidate run';
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    CREATE TRIGGER compiler_review_run_binding_guard BEFORE INSERT OR UPDATE ON compiler_review
    FOR EACH ROW EXECUTE FUNCTION compiler_review_run_binding_guard();

    CREATE FUNCTION compiler_publication_lineage_guard() RETURNS trigger AS $$
    BEGIN
      IF NEW.published_representation_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM commerce_representation
        WHERE id = NEW.published_representation_id
          AND merchant_id = NEW.merchant_id
          AND compiler_run_id = NEW.id
          AND producer = 'COMPILER'
      ) THEN
        RAISE EXCEPTION 'compiler run must name its own compiler representation';
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    CREATE TRIGGER compiler_publication_lineage_guard BEFORE UPDATE ON compiler_run
    FOR EACH ROW EXECUTE FUNCTION compiler_publication_lineage_guard();""")


def downgrade() -> None:
    op.execute("DROP TRIGGER compiler_publication_lineage_guard ON compiler_run")
    op.execute("DROP FUNCTION compiler_publication_lineage_guard()")
    op.execute("DROP TRIGGER compiler_review_run_binding_guard ON compiler_review")
    op.execute("DROP FUNCTION compiler_review_run_binding_guard()")
