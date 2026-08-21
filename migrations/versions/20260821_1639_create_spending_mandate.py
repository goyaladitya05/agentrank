"""create spending mandate

The authoritative financial authorization boundary. Points worth knowing when reading
this:

- Money is a BIGINT count of minor units with a non negative check, and its currency is a
  separate NOT NULL column checked against three uppercase letters, exactly as in the
  catalog. There is no code path that can authorize an amount without a currency.
- The validity window is ordered by a check constraint, so a mandate that expires before
  it begins is not representable. Expiry itself is derived from valid_until and the
  current time; no column and no background job records it.
- status and revoked_at are two views of one fact, so a check constraint keeps them in
  agreement rather than trusting the writer.
- status is VARCHAR with a check constraint rather than a native enum. Adding a value
  later is then an ordinary constraint change instead of ALTER TYPE.
- The foreign key onto merchant is RESTRICT, not CASCADE. A financial authorization is not
  catalog data and must not vanish as a side effect of removing a merchant.
- A trigger makes the authorization fields immutable and revocation terminal. That is a
  rule about a transition rather than about a row, which a check constraint cannot see.
  Without it, "immutable" would be a convention that any UPDATE could ignore.

Revision ID: e13cf9e64a4e
Revises: ace599f8cce9
Created: 2026-08-21 16:39:08.117370
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e13cf9e64a4e"
down_revision: str | None = "ace599f8cce9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AUTHORIZATION_GUARD = """
CREATE FUNCTION spending_mandate_authorization_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
        OR NEW.merchant_id IS DISTINCT FROM OLD.merchant_id
        OR NEW.max_total_amount_minor IS DISTINCT FROM OLD.max_total_amount_minor
        OR NEW.currency IS DISTINCT FROM OLD.currency
        OR NEW.max_quantity IS DISTINCT FROM OLD.max_quantity
        OR NEW.valid_from IS DISTINCT FROM OLD.valid_from
        OR NEW.valid_until IS DISTINCT FROM OLD.valid_until
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION 'spending mandate authorization fields are immutable';
    END IF;

    IF OLD.status = 'REVOKED'
        AND (NEW.status IS DISTINCT FROM OLD.status
             OR NEW.revoked_at IS DISTINCT FROM OLD.revoked_at)
    THEN
        RAISE EXCEPTION 'a revoked spending mandate cannot be changed';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

ATTACH_GUARD = """
CREATE TRIGGER spending_mandate_authorization_guard
BEFORE UPDATE ON spending_mandate
FOR EACH ROW EXECUTE FUNCTION spending_mandate_authorization_guard()
"""


def upgrade() -> None:
    op.create_table(
        "spending_mandate",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("max_total_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("max_quantity", sa.Integer(), nullable=True),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "ACTIVE",
                "REVOKED",
                name="mandate_status",
                native_enum=False,
                create_constraint=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status = 'REVOKED') = (revoked_at IS NOT NULL)",
            name=op.f("ck_spending_mandate_revoked_at_matches_status"),
        ),
        sa.CheckConstraint(
            "currency ~ '^[A-Z]{3}$'", name=op.f("ck_spending_mandate_currency_format")
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'REVOKED')", name=op.f("ck_spending_mandate_status_known")
        ),
        sa.CheckConstraint(
            "max_quantity IS NULL OR max_quantity > 0",
            name=op.f("ck_spending_mandate_quantity_positive"),
        ),
        sa.CheckConstraint(
            "max_total_amount_minor >= 0", name=op.f("ck_spending_mandate_amount_not_negative")
        ),
        sa.CheckConstraint(
            "valid_until > valid_from", name=op.f("ck_spending_mandate_validity_window_ordered")
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchant.id"],
            name=op.f("fk_spending_mandate_merchant_id_merchant"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_spending_mandate")),
    )
    op.execute(AUTHORIZATION_GUARD)
    op.execute(ATTACH_GUARD)


def downgrade() -> None:
    # Dropping the table takes its trigger with it. The function is schema level and has
    # to be dropped by name, otherwise a downgrade leaves an orphan behind and the next
    # upgrade fails on CREATE FUNCTION.
    op.drop_table("spending_mandate")
    op.execute("DROP FUNCTION spending_mandate_authorization_guard()")
