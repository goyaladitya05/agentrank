"""create inventory reservation

Stock held for a checkout that is being prepared for execution. Points worth knowing when
reading this:

- checkout_session gains UNIQUE (id, merchant_id) first, because the reservation's
  composite foreign key needs a unique constraint on exactly those two columns to point
  at. The existing one carries the currency as well, which is what a checkout line needs
  and what a reservation, holding no money, does not. Adding it changes no row: both
  columns are already unique because id alone is.
- inventory_reservation references checkout_session through (checkout_id, merchant_id), so
  a reservation cannot hold stock against another merchant's checkout. Merchant integrity
  is transitive through it, since checkout_session.merchant_id already reaches merchant.
- inventory_reservation_line references its reservation through (reservation_id,
  merchant_id) and variant through (variant_id, merchant_id), so a line can neither join
  another merchant's reservation nor hold another merchant's stock.
- Both of those foreign keys are named explicitly. The metadata naming convention would
  produce names longer than 63 bytes and PostgreSQL truncates identifiers there, so the
  name written here and the name in the database would silently disagree.
- variant.inventory_quantity is not touched, now or ever, by anything in this migration or
  the code above it. It stays the authoritative total stock. Available quantity is that
  total minus the effective reservations against it, computed at read time. There is
  deliberately no second counter to drift from the first.
- expires_at is checked against the row's own created_at, so a reservation cannot be
  created already expired. Expiry itself is derived by comparing expires_at with the
  current time; no column and no background job records it, exactly as with a mandate and
  a checkout.
- uq_inventory_reservation_active_checkout is a partial unique index on checkout_id where
  the status is ACTIVE. That is what makes one active reservation per checkout structural
  rather than a convention, and it is what lets a released reservation stay as history
  while a later one is written. The predicate deliberately says nothing about expiry: a
  predicate calling now() is not immutable and PostgreSQL will not index on one. Expiry is
  handled by the accounting query, and the two together give the property that matters,
  which is that stock is never counted twice for one checkout.
- Two triggers. The reservation guard makes ownership and expiry immutable and release
  terminal; the line guard refuses any update at all, because a reservation line has no
  lifecycle. Both are rules about a transition rather than about a row, which a check
  constraint cannot see. Neither refuses DELETE, so the cascade from a reservation to its
  lines still works, and DROP is neither, so a downgrade does too.

Revision ID: 637598637298
Revises: 70b5c985a47a
Created: 2026-08-21 18:46:19.943723
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "637598637298"
down_revision: str | None = "70b5c985a47a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RESERVATION_GUARD = """
CREATE FUNCTION inventory_reservation_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
        OR NEW.merchant_id IS DISTINCT FROM OLD.merchant_id
        OR NEW.checkout_id IS DISTINCT FROM OLD.checkout_id
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
        OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
    THEN
        RAISE EXCEPTION 'inventory reservation ownership and expiry are immutable';
    END IF;

    IF OLD.status = 'RELEASED'
        AND (NEW.status IS DISTINCT FROM OLD.status
             OR NEW.released_at IS DISTINCT FROM OLD.released_at)
    THEN
        RAISE EXCEPTION 'a released reservation cannot be changed';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

ATTACH_RESERVATION_GUARD = """
CREATE TRIGGER inventory_reservation_guard
BEFORE UPDATE ON inventory_reservation
FOR EACH ROW EXECUTE FUNCTION inventory_reservation_guard()
"""

LINE_GUARD = """
CREATE FUNCTION inventory_reservation_line_guard() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'inventory reservation lines are immutable';
END;
$$ LANGUAGE plpgsql
"""

ATTACH_LINE_GUARD = """
CREATE TRIGGER inventory_reservation_line_guard
BEFORE UPDATE ON inventory_reservation_line
FOR EACH ROW EXECUTE FUNCTION inventory_reservation_line_guard()
"""


def upgrade() -> None:
    op.create_unique_constraint(
        op.f("uq_checkout_session_id_merchant_id"), "checkout_session", ["id", "merchant_id"]
    )
    op.create_table(
        "inventory_reservation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("checkout_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "ACTIVE",
                "RELEASED",
                name="reservation_status",
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
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status = 'RELEASED') = (released_at IS NOT NULL)",
            name=op.f("ck_inventory_reservation_released_at_matches_status"),
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name=op.f("ck_inventory_reservation_expiry_after_creation"),
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'RELEASED')",
            name=op.f("ck_inventory_reservation_status_known"),
        ),
        sa.ForeignKeyConstraint(
            ["checkout_id", "merchant_id"],
            ["checkout_session.id", "checkout_session.merchant_id"],
            name="fk_inventory_reservation_checkout",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inventory_reservation")),
        sa.UniqueConstraint(
            "id", "merchant_id", name=op.f("uq_inventory_reservation_id_merchant_id")
        ),
    )
    op.create_index(
        op.f("ix_inventory_reservation_checkout_id"),
        "inventory_reservation",
        ["checkout_id"],
        unique=False,
    )
    op.create_index(
        "uq_inventory_reservation_active_checkout",
        "inventory_reservation",
        ["checkout_id"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.create_table(
        "inventory_reservation_line",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("reservation_id", sa.Uuid(), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("variant_id", sa.Uuid(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "quantity > 0", name=op.f("ck_inventory_reservation_line_quantity_positive")
        ),
        sa.ForeignKeyConstraint(
            ["reservation_id", "merchant_id"],
            ["inventory_reservation.id", "inventory_reservation.merchant_id"],
            name="fk_inventory_reservation_line_reservation",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["variant_id", "merchant_id"],
            ["variant.id", "variant.merchant_id"],
            name=op.f("fk_inventory_reservation_line_variant_id_merchant_id_variant"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inventory_reservation_line")),
        sa.UniqueConstraint(
            "reservation_id",
            "variant_id",
            name=op.f("uq_inventory_reservation_line_reservation_id_variant_id"),
        ),
    )
    op.create_index(
        op.f("ix_inventory_reservation_line_reservation_id"),
        "inventory_reservation_line",
        ["reservation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_inventory_reservation_line_variant_id"),
        "inventory_reservation_line",
        ["variant_id"],
        unique=False,
    )
    op.execute(RESERVATION_GUARD)
    op.execute(ATTACH_RESERVATION_GUARD)
    op.execute(LINE_GUARD)
    op.execute(ATTACH_LINE_GUARD)


def downgrade() -> None:
    # Dropping a table takes its trigger with it, and DROP is not an UPDATE, so neither
    # guard stands in the way. The functions are schema level and have to be dropped by
    # name, otherwise a downgrade leaves orphans behind and the next upgrade fails on
    # CREATE FUNCTION.
    op.drop_index(
        op.f("ix_inventory_reservation_line_variant_id"),
        table_name="inventory_reservation_line",
    )
    op.drop_index(
        op.f("ix_inventory_reservation_line_reservation_id"),
        table_name="inventory_reservation_line",
    )
    op.drop_table("inventory_reservation_line")
    op.drop_index(
        "uq_inventory_reservation_active_checkout",
        table_name="inventory_reservation",
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.drop_index(op.f("ix_inventory_reservation_checkout_id"), table_name="inventory_reservation")
    op.drop_table("inventory_reservation")
    op.execute("DROP FUNCTION inventory_reservation_line_guard()")
    op.execute("DROP FUNCTION inventory_reservation_guard()")
    op.drop_constraint(
        op.f("uq_checkout_session_id_merchant_id"), "checkout_session", type_="unique"
    )
