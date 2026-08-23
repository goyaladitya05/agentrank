"""add merchant compiler runs and review evidence

Revision ID: c4d8e1f5a2b7
Revises: a3c9d7e5f1b2
Created: 2026-08-24 11:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c4d8e1f5a2b7"
down_revision: str | None = "a3c9d7e5f1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "compiler_run",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("source_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("configuration_digest", sa.String(length=71), nullable=False),
        sa.Column("configuration", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_representation_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("id", "merchant_id", name="uq_compiler_run_binding"),
        sa.UniqueConstraint(
            "source_snapshot_id", "configuration_digest", name="uq_compiler_run_input"
        ),
        sa.ForeignKeyConstraint(["merchant_id"], ["merchant.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["source_snapshot_id", "merchant_id"],
            ["merchant_source_snapshot.id", "merchant_source_snapshot.merchant_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "configuration_digest ~ '^sha256:[0-9a-f]{64}$'", name="configuration_digest_format"
        ),
        sa.CheckConstraint("jsonb_typeof(configuration) = 'object'", name="configuration_object"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED')", name="status_known"
        ),
    )
    op.create_index("ix_compiler_run_merchant_id", "compiler_run", ["merchant_id"])
    op.create_table(
        "compiler_candidate",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("target", sa.String(length=256), nullable=False),
        sa.Column("proposal", postgresql.JSONB(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("id", "merchant_id", name="uq_compiler_candidate_binding"),
        sa.UniqueConstraint("run_id", "target", name="uq_compiler_candidate_target"),
        sa.ForeignKeyConstraint(
            ["run_id", "merchant_id"],
            ["compiler_run.id", "compiler_run.merchant_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("jsonb_typeof(proposal) = 'object'", name="proposal_object"),
        sa.CheckConstraint(
            "state IN ('ACCEPTED', 'REVIEW_REQUIRED', 'REJECTED')", name="state_known"
        ),
    )
    op.create_index("ix_compiler_candidate_merchant_id", "compiler_candidate", ["merchant_id"])
    op.create_index("ix_compiler_candidate_run_id", "compiler_candidate", ["run_id"])
    op.create_table(
        "compiler_review",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("correction", postgresql.JSONB(), nullable=True),
        sa.Column("reviewer", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("candidate_id", name="uq_compiler_review_candidate"),
        sa.ForeignKeyConstraint(
            ["candidate_id", "merchant_id"],
            ["compiler_candidate.id", "compiler_candidate.merchant_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "merchant_id"],
            ["compiler_run.id", "compiler_run.merchant_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("decision IN ('ACCEPT', 'CORRECT', 'REJECT')", name="decision_known"),
        sa.CheckConstraint(
            "correction IS NULL OR jsonb_typeof(correction) = 'object'", name="correction_object"
        ),
        sa.CheckConstraint(
            "(decision = 'CORRECT') = (correction IS NOT NULL)", name="correction_matches_decision"
        ),
    )
    op.create_index("ix_compiler_review_merchant_id", "compiler_review", ["merchant_id"])
    op.add_column("commerce_representation", sa.Column("compiler_run_id", sa.Uuid(), nullable=True))
    op.alter_column("commerce_representation", "producer_version", type_=sa.String(length=128))
    op.create_foreign_key(
        "fk_commerce_representation_compiler_run",
        "commerce_representation",
        "compiler_run",
        ["compiler_run_id", "merchant_id"],
        ["id", "merchant_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "compiler_representation_run_binding",
        "commerce_representation",
        "(producer = 'COMPILER') = (compiler_run_id IS NOT NULL)",
    )
    op.execute("""CREATE FUNCTION merchant_compiler_guard() RETURNS trigger AS $$
    BEGIN RAISE EXCEPTION 'compiler evidence is immutable'; END;
    $$ LANGUAGE plpgsql;
    CREATE FUNCTION compiler_run_guard() RETURNS trigger AS $$
    BEGIN
      IF TG_OP = 'DELETE' OR OLD.merchant_id <> NEW.merchant_id
         OR OLD.source_snapshot_id <> NEW.source_snapshot_id
         OR OLD.configuration_digest <> NEW.configuration_digest
         OR OLD.configuration <> NEW.configuration THEN
        RAISE EXCEPTION 'compiler run identity is immutable';
      END IF;
      IF OLD.status = 'PENDING' AND NEW.status = 'RUNNING' THEN RETURN NEW; END IF;
      IF OLD.status = 'RUNNING' AND NEW.status IN ('COMPLETED', 'FAILED') THEN RETURN NEW; END IF;
      IF OLD.status = 'COMPLETED' AND OLD.published_representation_id IS NULL
         AND NEW.status = 'COMPLETED' AND NEW.published_representation_id IS NOT NULL
      THEN RETURN NEW; END IF;
      RAISE EXCEPTION 'compiler run lifecycle is immutable';
    END;
    $$ LANGUAGE plpgsql;
    CREATE TRIGGER compiler_run_guard BEFORE UPDATE OR DELETE ON compiler_run
    FOR EACH ROW EXECUTE FUNCTION compiler_run_guard();
    CREATE TRIGGER compiler_candidate_guard BEFORE UPDATE OR DELETE ON compiler_candidate
    FOR EACH ROW EXECUTE FUNCTION merchant_compiler_guard();
    CREATE TRIGGER compiler_review_guard BEFORE UPDATE OR DELETE ON compiler_review
    FOR EACH ROW EXECUTE FUNCTION merchant_compiler_guard();""")


def downgrade() -> None:
    op.execute("DROP TRIGGER compiler_review_guard ON compiler_review")
    op.execute("DROP TRIGGER compiler_candidate_guard ON compiler_candidate")
    op.execute("DROP TRIGGER compiler_run_guard ON compiler_run")
    op.execute("DROP FUNCTION compiler_run_guard()")
    op.execute("DROP FUNCTION merchant_compiler_guard()")
    op.drop_constraint(
        "compiler_representation_run_binding", "commerce_representation", type_="check"
    )
    op.drop_constraint(
        "fk_commerce_representation_compiler_run", "commerce_representation", type_="foreignkey"
    )
    op.alter_column("commerce_representation", "producer_version", type_=sa.String(length=64))
    op.drop_column("commerce_representation", "compiler_run_id")
    op.drop_index("ix_compiler_review_merchant_id", table_name="compiler_review")
    op.drop_table("compiler_review")
    op.drop_index("ix_compiler_candidate_run_id", table_name="compiler_candidate")
    op.drop_index("ix_compiler_candidate_merchant_id", table_name="compiler_candidate")
    op.drop_table("compiler_candidate")
    op.drop_index("ix_compiler_run_merchant_id", table_name="compiler_run")
    op.drop_table("compiler_run")
