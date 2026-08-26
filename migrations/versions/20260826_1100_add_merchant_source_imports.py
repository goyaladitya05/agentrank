"""add merchant source imports

A merchant can now hand AgentRank the URLs of their own public pages, have them retrieved and
read deterministically, inspect what came out, and confirm it into an ordinary source snapshot.
That act needs a row, and it needs to be clear about what the row is and is not.

It is not a second source truth. `merchant_source_snapshot` remains the only place a merchant's
catalog is stated, and confirming an import writes one through the existing intake. This table
holds the acquisition: which URLs, what each answered, what the digest of what arrived was, what
was extracted, and what was deliberately left out with the reason.

Three properties are the database's rather than the application's:

- the retrieved evidence is immutable. A trigger permits exactly one update per row, and only the
  confirmation columns may change in it. Nothing can rewrite what an import found after a merchant
  has read it, including the process that wrote it.
- confirmation is all or nothing. The snapshot, the time and the merchant supplied stock level
  arrive together or not at all, and an import that failed can never be confirmed into anything.
- one import per merchant and request key, so a double submit of the import form fetches somebody
  else's website once rather than twice.

The submission origin gains a second member in the same migration, because a snapshot created by
confirming an import was not supplied through the console editor and a column that said it was
would be a column stating something that did not happen. Nothing is backfilled: every existing
submission row was a console submission and already says so.

Revision ID: e5b7c93af142
Revises: c1d4f8a2b6e3
Created: 2026-08-26 11:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5b7c93af142"
down_revision: str | None = "c1d4f8a2b6e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "merchant_source_import"
SUBMISSION = "merchant_source_submission"
SUBMISSION_KEY_PATTERN = r"^[0-9a-zA-Z_-]{8,64}$"

# One update per row, and only the confirmation in it. Written as a trigger rather than as
# application care because the property is "no process can rewrite retrieved evidence", and a
# property that holds because every writer remembers is a property that holds until one does not.
GUARD = """
CREATE FUNCTION merchant_source_import_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'a merchant source import is historical and is not deleted';
    END IF;
    IF OLD.confirmed_at IS NOT NULL THEN
        RAISE EXCEPTION 'a merchant source import has already been confirmed';
    END IF;
    IF NEW.id <> OLD.id
        OR NEW.merchant_id <> OLD.merchant_id
        OR NEW.request_key <> OLD.request_key
        OR NEW.origin <> OLD.origin
        OR NEW.state <> OLD.state
        OR NEW.failure_reason IS DISTINCT FROM OLD.failure_reason
        OR NEW.pages <> OLD.pages
        OR NEW.draft <> OLD.draft
        OR NEW.created_at <> OLD.created_at
    THEN
        RAISE EXCEPTION 'only the confirmation of a merchant source import may be written';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("request_key", sa.String(length=64), nullable=False),
        sa.Column("origin", sa.String(length=260), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("failure_reason", sa.String(length=64), nullable=True),
        sa.Column("pages", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("draft", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("source_snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("stock_level", sa.Integer(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("merchant_id", "request_key", name="uq_merchant_source_import_request"),
        sa.UniqueConstraint("id", "merchant_id", name="uq_merchant_source_import_binding"),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchant.id"],
            name="fk_merchant_source_import_merchant_id_merchant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_snapshot_id", "merchant_id"],
            ["merchant_source_snapshot.id", "merchant_source_snapshot.merchant_id"],
            name="fk_merchant_source_import_snapshot",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(f"request_key ~ '{SUBMISSION_KEY_PATTERN}'", name="request_key_format"),
        sa.CheckConstraint("state IN ('COMPLETED', 'FAILED')", name="state_known"),
        sa.CheckConstraint("jsonb_typeof(pages) = 'array'", name="pages_array"),
        sa.CheckConstraint("jsonb_typeof(draft) = 'object'", name="draft_object"),
        sa.CheckConstraint(
            "(source_snapshot_id IS NULL) = (confirmed_at IS NULL)",
            name="confirmation_complete",
        ),
        sa.CheckConstraint(
            "confirmed_at IS NOT NULL OR stock_level IS NULL",
            name="stock_level_needs_confirmation",
        ),
        sa.CheckConstraint(
            "stock_level IS NULL OR stock_level >= 0", name="stock_level_not_negative"
        ),
        sa.CheckConstraint(
            "state <> 'FAILED' OR source_snapshot_id IS NULL",
            name="failed_import_is_not_confirmed",
        ),
    )
    op.create_index("ix_merchant_source_import_merchant_id", TABLE, ["merchant_id"])
    op.create_index(
        "ix_merchant_source_import_source_snapshot_id_merchant_id",
        TABLE,
        ["source_snapshot_id", "merchant_id"],
    )
    op.execute(GUARD)
    op.execute(
        f"CREATE TRIGGER merchant_source_import_guard BEFORE UPDATE OR DELETE ON {TABLE}"
        " FOR EACH ROW EXECUTE FUNCTION merchant_source_import_guard()"
    )
    op.drop_constraint("origin_known", SUBMISSION, type_="check")
    op.create_check_constraint(
        "origin_known", SUBMISSION, "origin IN ('MERCHANT_CONSOLE', 'MERCHANT_IMPORT')"
    )


def downgrade() -> None:
    """Reverse this migration, refusing rather than discarding what only it can hold.

    Two refusals rather than a quiet cascade. Dropping the table would destroy every merchant's
    record of where their imported source snapshots came from, and narrowing the origin constraint
    would either fail on rows it cannot describe or, if forced, relabel an imported submission as a
    console one. Both are checked before anything is dropped, and the whole downgrade is one
    transaction, so a refusal leaves the schema exactly as it was.
    """
    connection = op.get_bind()
    # Both statements compose a table name into SQL, which is what S608 is for and which a
    # constant defined in this file is not: neither name comes from a caller, a row or a request.
    imports = connection.execute(sa.text(f"SELECT count(*) FROM {TABLE}")).scalar_one()  # noqa: S608
    if imports:
        raise RuntimeError(
            f"{TABLE} holds {imports} row(s) recording where imported source snapshots came from;"
            " downgrading would discard the provenance of source history this database keeps"
        )
    imported = connection.execute(
        sa.text(f"SELECT count(*) FROM {SUBMISSION} WHERE origin = 'MERCHANT_IMPORT'")  # noqa: S608
    ).scalar_one()
    if imported:
        raise RuntimeError(
            f"{SUBMISSION} holds {imported} row(s) whose origin this downgrade cannot describe;"
            " relabelling them as console submissions would state something that did not happen"
        )
    op.drop_constraint("origin_known", SUBMISSION, type_="check")
    op.create_check_constraint("origin_known", SUBMISSION, "origin IN ('MERCHANT_CONSOLE')")
    op.execute(f"DROP TRIGGER merchant_source_import_guard ON {TABLE}")
    op.execute("DROP FUNCTION merchant_source_import_guard()")
    op.drop_index("ix_merchant_source_import_source_snapshot_id_merchant_id", table_name=TABLE)
    op.drop_index("ix_merchant_source_import_merchant_id", table_name=TABLE)
    op.drop_table(TABLE)
