"""create checkout session and line

The immutable merchant quote. Points worth knowing when reading this:

- Money is a BIGINT count of minor units with a non negative check and a separate NOT NULL
  currency, exactly as in the catalog and the mandate. On top of that, one check constraint
  holds the identity total = subtotal + shipping - discount on every row, so a quote whose
  parts do not add up is not representable.
- checkout_session references spending_mandate through the composite (mandate_id,
  merchant_id), so a checkout cannot be bound to a mandate granted to another merchant.
  Merchant integrity is transitive through it, since spending_mandate.merchant_id already
  references merchant, which is why there is no second foreign key straight to merchant.
- checkout_line references checkout_session through (checkout_id, merchant_id, currency)
  and variant through (variant_id, merchant_id). Together those mean a line cannot join a
  quote from another merchant, cannot carry a currency the quote does not name, and cannot
  put another merchant's variant on the quote. All three are structural, not conventions.
- The line's foreign key onto checkout_session is named explicitly. The metadata naming
  convention would produce a 66 character name and PostgreSQL truncates identifiers at 63
  bytes, so the name written here and the name in the database would silently disagree.
- unit_price_amount_minor is a snapshot of the catalog price at quote time. Nothing
  recomputes it, which is what lets a checkout stay readable as the quote it was after the
  catalog moves on.
- expires_at is checked against the row's own created_at, so a quote cannot be created
  already expired. Expiry itself is derived by comparing expires_at with the current time;
  no column and no background job records it, exactly as with a mandate.
- The foreign key onto variant is RESTRICT. A quote that points at a variant which no
  longer exists is a hole in the financial record. Deleting a merchant is already blocked
  by spending_mandate before it ever reaches here.
- Two triggers. The session guard makes the quote fields immutable and cancellation
  terminal; the line guard refuses any update at all, because a line has no lifecycle. Both
  are rules about a transition rather than about a row, which a check constraint cannot see.

Revision ID: 4dc1a0f57b18
Revises: efb23d414a80
Created: 2026-08-21 17:24:11.903254
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4dc1a0f57b18"
down_revision: str | None = "efb23d414a80"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

QUOTE_GUARD = """
CREATE FUNCTION checkout_session_quote_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
        OR NEW.merchant_id IS DISTINCT FROM OLD.merchant_id
        OR NEW.mandate_id IS DISTINCT FROM OLD.mandate_id
        OR NEW.currency IS DISTINCT FROM OLD.currency
        OR NEW.subtotal_amount_minor IS DISTINCT FROM OLD.subtotal_amount_minor
        OR NEW.shipping_amount_minor IS DISTINCT FROM OLD.shipping_amount_minor
        OR NEW.discount_amount_minor IS DISTINCT FROM OLD.discount_amount_minor
        OR NEW.total_amount_minor IS DISTINCT FROM OLD.total_amount_minor
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
        OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
    THEN
        RAISE EXCEPTION 'checkout quote fields are immutable';
    END IF;

    IF OLD.status = 'CANCELLED'
        AND (NEW.status IS DISTINCT FROM OLD.status
             OR NEW.cancelled_at IS DISTINCT FROM OLD.cancelled_at)
    THEN
        RAISE EXCEPTION 'a cancelled checkout cannot be changed';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

ATTACH_QUOTE_GUARD = """
CREATE TRIGGER checkout_session_quote_guard
BEFORE UPDATE ON checkout_session
FOR EACH ROW EXECUTE FUNCTION checkout_session_quote_guard()
"""

LINE_GUARD = """
CREATE FUNCTION checkout_line_quote_guard() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'checkout lines are immutable once the quote is written';
END;
$$ LANGUAGE plpgsql
"""

ATTACH_LINE_GUARD = """
CREATE TRIGGER checkout_line_quote_guard
BEFORE UPDATE ON checkout_line
FOR EACH ROW EXECUTE FUNCTION checkout_line_quote_guard()
"""


def upgrade() -> None:
    op.create_table(
        "checkout_session",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("mandate_id", sa.Uuid(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("subtotal_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("shipping_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("discount_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("total_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "OPEN",
                "CANCELLED",
                name="checkout_status",
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
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status = 'CANCELLED') = (cancelled_at IS NOT NULL)",
            name=op.f("ck_checkout_session_cancelled_at_matches_status"),
        ),
        sa.CheckConstraint(
            "currency ~ '^[A-Z]{3}$'", name=op.f("ck_checkout_session_currency_format")
        ),
        sa.CheckConstraint(
            "status IN ('OPEN', 'CANCELLED')", name=op.f("ck_checkout_session_status_known")
        ),
        sa.CheckConstraint(
            "discount_amount_minor >= 0", name=op.f("ck_checkout_session_discount_not_negative")
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name=op.f("ck_checkout_session_expiry_after_creation")
        ),
        sa.CheckConstraint(
            "shipping_amount_minor >= 0", name=op.f("ck_checkout_session_shipping_not_negative")
        ),
        sa.CheckConstraint(
            "subtotal_amount_minor >= 0", name=op.f("ck_checkout_session_subtotal_not_negative")
        ),
        sa.CheckConstraint(
            "total_amount_minor = subtotal_amount_minor + shipping_amount_minor"
            " - discount_amount_minor",
            name=op.f("ck_checkout_session_total_matches_parts"),
        ),
        sa.CheckConstraint(
            "total_amount_minor >= 0", name=op.f("ck_checkout_session_total_not_negative")
        ),
        sa.ForeignKeyConstraint(
            ["mandate_id", "merchant_id"],
            ["spending_mandate.id", "spending_mandate.merchant_id"],
            name=op.f("fk_checkout_session_mandate_id_merchant_id_spending_mandate"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_checkout_session")),
        sa.UniqueConstraint(
            "id",
            "merchant_id",
            "currency",
            name=op.f("uq_checkout_session_id_merchant_id_currency"),
        ),
    )
    op.create_table(
        "checkout_line",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("checkout_id", sa.Uuid(), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("variant_id", sa.Uuid(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "currency ~ '^[A-Z]{3}$'", name=op.f("ck_checkout_line_currency_format")
        ),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_checkout_line_quantity_positive")),
        sa.CheckConstraint(
            "unit_price_amount_minor >= 0", name=op.f("ck_checkout_line_unit_price_not_negative")
        ),
        sa.ForeignKeyConstraint(
            ["checkout_id", "merchant_id", "currency"],
            ["checkout_session.id", "checkout_session.merchant_id", "checkout_session.currency"],
            name="fk_checkout_line_checkout_session",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["variant_id", "merchant_id"],
            ["variant.id", "variant.merchant_id"],
            name=op.f("fk_checkout_line_variant_id_merchant_id_variant"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_checkout_line")),
        sa.UniqueConstraint(
            "checkout_id", "variant_id", name=op.f("uq_checkout_line_checkout_id_variant_id")
        ),
    )
    op.execute(QUOTE_GUARD)
    op.execute(ATTACH_QUOTE_GUARD)
    op.execute(LINE_GUARD)
    op.execute(ATTACH_LINE_GUARD)


def downgrade() -> None:
    # Dropping a table takes its trigger with it, and DROP is not an UPDATE, so neither
    # guard stands in the way. The functions are schema level and have to be dropped by
    # name, otherwise a downgrade leaves orphans behind and the next upgrade fails on
    # CREATE FUNCTION.
    op.drop_table("checkout_line")
    op.drop_table("checkout_session")
    op.execute("DROP FUNCTION checkout_line_quote_guard()")
    op.execute("DROP FUNCTION checkout_session_quote_guard()")
