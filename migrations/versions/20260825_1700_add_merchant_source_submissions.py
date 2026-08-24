"""add merchant source submissions

A merchant can now supply source evidence from the console, and that command needs a durable
record of its own. A source snapshot answers what the merchant said; it cannot answer which
command said it, which mechanism supplied it, or what a repeat of that command should do.

Three properties are the database's rather than the application's:

- one submission per merchant per request key, which is what makes a double submit and a retry
  after a lost response the same command rather than two snapshots.
- one creator per snapshot, through a partial unique index over `created_snapshot`. Many
  submissions may name a snapshot, because evidence identical to the merchant's current snapshot
  resolves to it rather than being written again, and exactly one of them wrote it.
- a submission is evidence, so it is immutable once written.

Nothing is backfilled. Every snapshot that existed before this table was published by the
operator command line, and a row inventing a console origin for one would be a row stating
something that did not happen. Absence is read as the operator origin instead.

Revision ID: a4b7c1d9e2f5
Revises: f2a5b8c1d3e6
Created: 2026-08-25 17:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a4b7c1d9e2f5"
down_revision: str | None = "f2a5b8c1d3e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CREATOR_INDEX = "uq_merchant_source_submission_creator"

GUARD = """
CREATE FUNCTION merchant_source_submission_guard() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'a merchant source submission is evidence and is immutable';
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    op.create_table(
        "merchant_source_submission",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("request_key", sa.String(length=64), nullable=False),
        sa.Column("source_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("origin", sa.String(length=32), nullable=False),
        sa.Column("created_snapshot", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "merchant_id", "request_key", name="uq_merchant_source_submission_request"
        ),
        sa.ForeignKeyConstraint(["merchant_id"], ["merchant.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["source_snapshot_id", "merchant_id"],
            ["merchant_source_snapshot.id", "merchant_source_snapshot.merchant_id"],
            name="fk_merchant_source_submission_snapshot",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("request_key ~ '^[0-9a-zA-Z_-]{8,64}$'", name="request_key_format"),
        sa.CheckConstraint("origin IN ('MERCHANT_CONSOLE')", name="origin_known"),
    )
    op.create_index(
        CREATOR_INDEX,
        "merchant_source_submission",
        ["source_snapshot_id"],
        unique=True,
        postgresql_where=sa.text("created_snapshot"),
    )
    op.create_index(
        "ix_merchant_source_submission_merchant_id",
        "merchant_source_submission",
        ["merchant_id"],
    )
    op.create_index(
        "ix_merchant_source_submission_source_snapshot_id_merchant_id",
        "merchant_source_submission",
        ["source_snapshot_id", "merchant_id"],
    )
    op.execute(GUARD)
    op.execute(
        "CREATE TRIGGER merchant_source_submission_guard BEFORE UPDATE OR DELETE"
        " ON merchant_source_submission"
        " FOR EACH ROW EXECUTE FUNCTION merchant_source_submission_guard()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER merchant_source_submission_guard ON merchant_source_submission")
    op.execute("DROP FUNCTION merchant_source_submission_guard()")
    op.drop_index(
        "ix_merchant_source_submission_source_snapshot_id_merchant_id",
        table_name="merchant_source_submission",
    )
    op.drop_index(
        "ix_merchant_source_submission_merchant_id", table_name="merchant_source_submission"
    )
    op.drop_index(CREATOR_INDEX, table_name="merchant_source_submission")
    op.drop_table("merchant_source_submission")
