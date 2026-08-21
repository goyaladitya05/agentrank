"""create merchant api credential

The first authentication table, and the first column in `audit_event` since it was created.

Two things happen here and they are one migration because the second references the first. The
credential table holds a verifier and never a secret, and `audit_event.credential_id` is where
an authenticated request records which key authorized it.

The audit column is nullable and is not backfilled. Every event that already exists was written
with no authenticated caller behind it, and inventing one for them would be manufacturing the
exact evidence this column is supposed to provide. Absent means nobody knows.

Three rules are enforced here rather than in application code:

- a stored verifier is a labelled digest of a known algorithm, so a raw token cannot be written
  into the column even by a statement issued outside this application
- one verifier belongs to one credential, through a unique index. Two rows sharing one would
  mean one key authenticating as two merchants
- everything except `revoked_at` is immutable, and revocation is terminal

The last one is a trigger, because it is a rule about a transition rather than about a row. It
is the same shape as the mandate's authorization guard and it is deliberately a whitelist of
what may change rather than a blacklist of what may not: a guard that names the columns it
protects leaves every column added after it unprotected.

Revision ID: ef2868164941
Revises: 4c8de0a1b562
Created: 2026-08-22 02:52:11.804268
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ef2868164941"
down_revision: str | None = "4c8de0a1b562"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CREDENTIAL_GUARD = """
CREATE FUNCTION merchant_api_credential_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
        OR NEW.merchant_id IS DISTINCT FROM OLD.merchant_id
        OR NEW.secret_hash IS DISTINCT FROM OLD.secret_hash
        OR NEW.label IS DISTINCT FROM OLD.label
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION 'merchant api credential fields are immutable';
    END IF;

    IF OLD.revoked_at IS NOT NULL
        AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at
    THEN
        RAISE EXCEPTION 'a revoked merchant api credential cannot be changed';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

ATTACH_GUARD = """
CREATE TRIGGER merchant_api_credential_guard
BEFORE UPDATE ON merchant_api_credential
FOR EACH ROW EXECUTE FUNCTION merchant_api_credential_guard()
"""


def upgrade() -> None:
    op.create_table(
        "merchant_api_credential",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("secret_hash", sa.String(length=71), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "secret_hash ~ '^sha256:[0-9a-f]{64}$'",
            name=op.f("ck_merchant_api_credential_secret_hash_format"),
        ),
        sa.CheckConstraint(
            "length(btrim(label)) > 0", name=op.f("ck_merchant_api_credential_label_not_blank")
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchant.id"],
            name=op.f("fk_merchant_api_credential_merchant_id_merchant"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_merchant_api_credential")),
    )
    op.create_index(
        op.f("ix_merchant_api_credential_merchant_id"),
        "merchant_api_credential",
        ["merchant_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_merchant_api_credential_secret_hash"),
        "merchant_api_credential",
        ["secret_hash"],
        unique=True,
    )
    op.execute(CREDENTIAL_GUARD)
    op.execute(ATTACH_GUARD)

    op.add_column("audit_event", sa.Column("credential_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_audit_event_credential_id_merchant_api_credential"),
        "audit_event",
        "merchant_api_credential",
        ["credential_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    """Remove the column and the table, in that order, and discard attribution.

    This is lossy and it is still done, which is the opposite of the decision the operator
    outcome source migration made, so the difference is worth stating.

    That one refused, because narrowing the constraint would have required rewriting an
    abandoned payment's `outcome_source` into a value claiming a provider answered when none
    did. The only ways through were a lie or a deletion.

    Dropping `credential_id` discards evidence rather than falsifying a record. Every remaining
    field on every event stays exactly as true as it was: what happened, when, which merchant,
    which role, which resource. What is lost is the answer to a question the schema could no
    longer represent at all, and a column that cannot exist cannot hold a wrong answer.

    It cannot be restored by upgrading again. Reapplying this migration produces the column
    empty, and nothing reconstructs which credential authorized a historical request.

    The audit column goes first because it references the table below it.
    """
    op.drop_constraint(
        op.f("fk_audit_event_credential_id_merchant_api_credential"),
        "audit_event",
        type_="foreignkey",
    )
    op.drop_column("audit_event", "credential_id")

    # Dropping the table takes its trigger with it. The function is schema level and has to be
    # dropped by name, otherwise a downgrade leaves an orphan behind and the next upgrade fails
    # on CREATE FUNCTION.
    op.drop_index(
        op.f("ix_merchant_api_credential_secret_hash"), table_name="merchant_api_credential"
    )
    op.drop_index(
        op.f("ix_merchant_api_credential_merchant_id"), table_name="merchant_api_credential"
    )
    op.drop_table("merchant_api_credential")
    op.execute("DROP FUNCTION merchant_api_credential_guard()")
