"""create audit event

The append only record every later phase builds its transaction traces on. Points worth
knowing when reading this:

- The table is append only, enforced by a trigger that refuses UPDATE and DELETE outright.
  The application offers neither operation either, but an audit trail whose immutability
  depends on nobody writing the wrong function is not immutable.
- occurred_at defaults to now(), which inside a transaction is the transaction time, so a
  state change and the event recording it carry the same instant rather than two clock
  readings that only look simultaneous.
- payload is JSONB checked to be an object, so a consumer can rely on the shape. It holds
  structured detail about the event, never a credential, a token or a line of log text.
- event_type is checked against a lowercase dotted pattern. These are stable machine
  readable identifiers such as mandate.created, never display text.
- Two indexes, one per way the table is read: everything for a merchant, and everything
  about one resource. Both are time ordered.
- The foreign key onto merchant is RESTRICT. History does not disappear because a merchant
  row did.

Revision ID: 9360057d8773
Revises: e13cf9e64a4e
Created: 2026-08-21 16:50:32.002357
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "9360057d8773"
down_revision: str | None = "e13cf9e64a4e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY_GUARD = """
CREATE FUNCTION audit_event_append_only_guard() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_event is append only, % is not permitted', TG_OP;
END;
$$ LANGUAGE plpgsql
"""

ATTACH_GUARD = """
CREATE TRIGGER audit_event_append_only_guard
BEFORE UPDATE OR DELETE ON audit_event
FOR EACH ROW EXECUTE FUNCTION audit_event_append_only_guard()
"""


def upgrade() -> None:
    op.create_table(
        "audit_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "actor_type",
            sa.Enum("SYSTEM", "BUYER", name="actor_type", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "actor_type IN ('SYSTEM', 'BUYER')", name=op.f("ck_audit_event_actor_type_known")
        ),
        sa.CheckConstraint(
            "event_type ~ '^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)+$'",
            name=op.f("ck_audit_event_event_type_format"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'", name=op.f("ck_audit_event_payload_is_an_object")
        ),
        sa.CheckConstraint(
            "length(btrim(resource_type)) > 0", name=op.f("ck_audit_event_resource_type_not_blank")
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchant.id"],
            name=op.f("fk_audit_event_merchant_id_merchant"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_event")),
    )
    op.create_index(
        op.f("ix_audit_event_merchant_id_occurred_at"),
        "audit_event",
        ["merchant_id", "occurred_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_audit_event_resource_type_resource_id_occurred_at"),
        "audit_event",
        ["resource_type", "resource_id", "occurred_at"],
        unique=False,
    )
    op.execute(APPEND_ONLY_GUARD)
    op.execute(ATTACH_GUARD)


def downgrade() -> None:
    # Dropping the table takes its trigger with it, and DROP is neither an UPDATE nor a
    # DELETE, so the guard does not stand in the way of a downgrade. The function is
    # schema level and has to be dropped by name.
    op.drop_index(
        op.f("ix_audit_event_resource_type_resource_id_occurred_at"), table_name="audit_event"
    )
    op.drop_index(op.f("ix_audit_event_merchant_id_occurred_at"), table_name="audit_event")
    op.drop_table("audit_event")
    op.execute("DROP FUNCTION audit_event_append_only_guard()")
