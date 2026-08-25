"""hold the console's browser sessions in PostgreSQL instead of one Next.js process

The console used to keep a merchant's session in the memory of whichever Next.js process minted
it: a random cookie token, a map from that token to the merchant API key, and nothing durable at
all. A cookie identified a session only to that one process, so a second console instance signed
the merchant out and so did every restart. Phase 5A's topology allows both, so the session moved
here.

Two properties are enforced by the schema rather than by the service that writes to it.

A session belongs to one merchant's credential, checked as a composite foreign key. That is what
`uq_merchant_api_credential_binding` is added for: without a unique key over the pair, a session
could name one merchant and a credential belonging to another, which is the single thing this
table must never be able to hold. The constraint is created before the table that references it.

A session is immutable except for being closed. The trigger below refuses any UPDATE that moves
the merchant, the credential, the verifier, the creation time or the expiry, and refuses to
un-revoke one. So a session cannot be lengthened, cannot be handed to another tenant and cannot
be reopened after a sign-out, and none of that depends on application code remembering.

DELETE is deliberately allowed, unlike on the evidence tables. An expired session is not evidence
about anything: it records no benchmark, no source snapshot and no compiler run, and nothing
references it. Bounded cleanup of settled rows is the intended use and is the reason
`ix_merchant_console_session_expires_at` exists.

The table stores a digest and never a working credential, exactly like `merchant_api_credential`
beside it, and it stores no merchant API key at all.

Revision ID: 791257b7c3b3
Revises: c8d3f1a6b204
Created: 2026-08-25 22:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "791257b7c3b3"
down_revision: str | None = "c8d3f1a6b204"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "merchant_console_session"

# Everything about a session except whether it is closed. Named once so the trigger and this
# comment cannot disagree about what "immutable except for being closed" means.
FROZEN = ("id", "merchant_id", "credential_id", "verifier_hash", "created_at", "expires_at")


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_merchant_api_credential_binding", "merchant_api_credential", ["id", "merchant_id"]
    )
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("credential_id", sa.Uuid(), nullable=False),
        sa.Column("verifier_hash", sa.String(length=71), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "verifier_hash ~ '^sha256:[0-9a-f]{64}$'",
            name=op.f("ck_merchant_console_session_verifier_hash_format"),
        ),
        sa.ForeignKeyConstraint(
            ["credential_id", "merchant_id"],
            ["merchant_api_credential.id", "merchant_api_credential.merchant_id"],
            name="fk_merchant_console_session_credential",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_merchant_console_session")),
    )
    op.create_index(
        op.f("ix_merchant_console_session_expires_at"), TABLE, ["expires_at"], unique=False
    )
    op.create_index(
        op.f("ix_merchant_console_session_merchant_id"), TABLE, ["merchant_id"], unique=False
    )
    op.create_index(
        op.f("ix_merchant_console_session_verifier_hash"), TABLE, ["verifier_hash"], unique=True
    )

    moved = " OR ".join(f"NEW.{column} IS DISTINCT FROM OLD.{column}" for column in FROZEN)
    op.execute(f"""CREATE FUNCTION merchant_console_session_guard() RETURNS trigger AS $$
    BEGIN
        IF {moved} THEN
            RAISE EXCEPTION 'a console session is immutable except for being revoked';
        END IF;
        IF OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at THEN
            RAISE EXCEPTION 'a revoked console session cannot be reopened or re-revoked';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    CREATE TRIGGER merchant_console_session_guard BEFORE UPDATE ON {TABLE}
    FOR EACH ROW EXECUTE FUNCTION merchant_console_session_guard();""")


def downgrade() -> None:
    op.execute(f"DROP TRIGGER merchant_console_session_guard ON {TABLE}")
    op.execute("DROP FUNCTION merchant_console_session_guard()")
    op.drop_index(op.f("ix_merchant_console_session_verifier_hash"), table_name=TABLE)
    op.drop_index(op.f("ix_merchant_console_session_merchant_id"), table_name=TABLE)
    op.drop_index(op.f("ix_merchant_console_session_expires_at"), table_name=TABLE)
    op.drop_table(TABLE)
    op.drop_constraint(
        "uq_merchant_api_credential_binding", "merchant_api_credential", type_="unique"
    )
