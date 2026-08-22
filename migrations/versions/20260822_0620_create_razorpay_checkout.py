"""create razorpay checkout

The durable relationship between one AgentRank payment attempt and one Razorpay Order. Points
worth knowing when reading this:

- `payment_attempt` gains a unique constraint on (id, merchant_id, amount_minor, currency)
  first, because PostgreSQL needs one on exactly those columns for a composite foreign key to
  point at them. Adding it changes no row: all four are already unique because id alone is.
- `razorpay_checkout` references `payment_attempt` through that composite. One foreign key,
  three invariants: a binding cannot name another merchant's payment, cannot carry an amount
  that differs from what was admitted, and cannot carry a different currency. Every referenced
  column is immutable at the database through the payment attempt guard, so the freezing is
  structural rather than a copy something could later diverge from. This is what makes "the
  provider amount comes from authoritative admitted state" a property of the schema.
- Three unique constraints, each stopping a different duplication. One attempt has at most one
  logical order. One receipt belongs to one binding. One provider order belongs to one binding,
  and NULLs are distinct in PostgreSQL so every row still waiting for an order coexists.
- The foreign key is named explicitly. The metadata naming convention would produce a name
  longer than the 63 bytes PostgreSQL keeps, so the name written here and the name in the
  database would silently disagree.
- The guard is a whitelist of transitions from the start, matching every other guard in this
  schema. PREPARING may become AWAITING_PAYMENT, AWAITING_PAYMENT may become CONFIRMED, and
  CONFIRMED accepts no update at all. Ownership, money, identity and the provider order
  identifier are immutable, and the order identifier specifically cannot be moved once written:
  rebinding a payment attempt to a different Razorpay order would be the most expensive single
  update available in this table.
- No guard refuses DELETE, so cascades still work, and DROP is not an UPDATE, so a downgrade
  does too.
- The downgrade is unconditional and lossy, and that is a deliberate difference from the payment
  attempt downgrade. See `downgrade`.

Revision ID: 7a1c4e93b8d0
Revises: ef2868164941
Created: 2026-08-22 06:20:04.118273
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7a1c4e93b8d0"
down_revision: str | None = "ef2868164941"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHECKOUT_GUARD = """
CREATE FUNCTION razorpay_checkout_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
        OR NEW.merchant_id IS DISTINCT FROM OLD.merchant_id
        OR NEW.payment_attempt_id IS DISTINCT FROM OLD.payment_attempt_id
        OR NEW.provider_receipt IS DISTINCT FROM OLD.provider_receipt
        OR NEW.amount_minor IS DISTINCT FROM OLD.amount_minor
        OR NEW.currency IS DISTINCT FROM OLD.currency
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION 'razorpay checkout ownership, money and identity are immutable';
    END IF;

    IF OLD.provider_order_id IS NOT NULL
        AND NEW.provider_order_id IS DISTINCT FROM OLD.provider_order_id
    THEN
        RAISE EXCEPTION 'a razorpay checkout cannot be rebound to another order';
    END IF;

    IF OLD.order_created_at IS NOT NULL
        AND NEW.order_created_at IS DISTINCT FROM OLD.order_created_at
    THEN
        RAISE EXCEPTION 'a razorpay order creation time cannot be moved';
    END IF;

    IF OLD.status = 'CONFIRMED' THEN
        RAISE EXCEPTION 'a confirmed razorpay checkout cannot be changed';
    END IF;

    IF (OLD.status, NEW.status) NOT IN (
        ('PREPARING', 'PREPARING'),
        ('PREPARING', 'AWAITING_PAYMENT'),
        ('AWAITING_PAYMENT', 'AWAITING_PAYMENT'),
        ('AWAITING_PAYMENT', 'CONFIRMED')
    ) THEN
        RAISE EXCEPTION 'razorpay checkout status cannot go from % to %', OLD.status, NEW.status;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

ATTACH_GUARD = """
CREATE TRIGGER razorpay_checkout_guard
BEFORE UPDATE ON razorpay_checkout
FOR EACH ROW EXECUTE FUNCTION razorpay_checkout_guard()
"""


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_payment_attempt_binding",
        "payment_attempt",
        ["id", "merchant_id", "amount_minor", "currency"],
    )

    op.create_table(
        "razorpay_checkout",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("payment_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("provider_receipt", sa.String(length=40), nullable=False),
        sa.Column("provider_order_id", sa.String(length=64), nullable=True),
        sa.Column("provider_payment_id", sa.String(length=64), nullable=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "PREPARING",
                "AWAITING_PAYMENT",
                "CONFIRMED",
                name="razorpay_checkout_status",
                native_enum=False,
                create_constraint=False,
                length=20,
            ),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("order_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('PREPARING', 'AWAITING_PAYMENT', 'CONFIRMED')",
            name=op.f("ck_razorpay_checkout_status_known"),
        ),
        sa.CheckConstraint("amount_minor > 0", name=op.f("ck_razorpay_checkout_amount_positive")),
        sa.CheckConstraint(
            "currency ~ '^[A-Z]{3}$'", name=op.f("ck_razorpay_checkout_currency_format")
        ),
        sa.CheckConstraint(
            "length(btrim(provider_receipt)) > 0",
            name=op.f("ck_razorpay_checkout_provider_receipt_not_blank"),
        ),
        sa.CheckConstraint(
            "provider_order_id IS NULL OR length(btrim(provider_order_id)) > 0",
            name=op.f("ck_razorpay_checkout_provider_order_id_not_blank"),
        ),
        sa.CheckConstraint(
            "provider_payment_id IS NULL OR length(btrim(provider_payment_id)) > 0",
            name=op.f("ck_razorpay_checkout_provider_payment_id_not_blank"),
        ),
        sa.CheckConstraint(
            "(status = 'PREPARING') = (provider_order_id IS NULL)",
            name=op.f("ck_razorpay_checkout_order_id_matches_status"),
        ),
        sa.CheckConstraint(
            "(provider_order_id IS NULL) = (order_created_at IS NULL)",
            name=op.f("ck_razorpay_checkout_order_created_at_matches_order_id"),
        ),
        sa.CheckConstraint(
            "(status = 'CONFIRMED') = (provider_payment_id IS NOT NULL)",
            name=op.f("ck_razorpay_checkout_payment_id_matches_status"),
        ),
        sa.CheckConstraint(
            "(status = 'CONFIRMED') = (confirmed_at IS NOT NULL)",
            name=op.f("ck_razorpay_checkout_confirmed_at_matches_status"),
        ),
        sa.ForeignKeyConstraint(
            ["payment_attempt_id", "merchant_id", "amount_minor", "currency"],
            [
                "payment_attempt.id",
                "payment_attempt.merchant_id",
                "payment_attempt.amount_minor",
                "payment_attempt.currency",
            ],
            name="fk_razorpay_checkout_payment_attempt",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_razorpay_checkout")),
        sa.UniqueConstraint("payment_attempt_id", name="uq_razorpay_checkout_attempt"),
        sa.UniqueConstraint("provider_receipt", name="uq_razorpay_checkout_receipt"),
        sa.UniqueConstraint("provider_order_id", name="uq_razorpay_checkout_order"),
    )
    op.create_index(op.f("ix_razorpay_checkout_merchant_id"), "razorpay_checkout", ["merchant_id"])
    op.execute(CHECKOUT_GUARD)
    op.execute(ATTACH_GUARD)


def downgrade() -> None:
    """Drop the bindings, unconditionally, and say what that costs.

    Deliberately different from the payment attempt downgrade, which refuses when the database
    holds facts the previous schema cannot express. The difference is what the previous schema
    would have to invent.

    Nothing here is authoritative. Whether a payment succeeded, whether a checkout is paid,
    whether stock was consumed and how much money moved are all recorded on `payment_attempt`,
    `checkout_session`, `inventory_reservation` and `variant`, and none of those rows changes.
    What is lost is the correlation between an AgentRank attempt and a Razorpay order, which is
    evidence rather than truth, and it is reconstructible: the receipt is a deterministic
    function of the merchant and the attempt, so the same question can be asked of Razorpay
    again at any time. That is exactly why the receipt is derived rather than random.

    It is still lossy. An order that was created and never confirmed becomes invisible to this
    application until somebody recomputes its receipt, and reapplying this migration produces an
    empty table.

    The unique constraint on `payment_attempt` goes last, because the foreign key above depends
    on it.
    """
    op.drop_index(op.f("ix_razorpay_checkout_merchant_id"), table_name="razorpay_checkout")
    # Dropping the table takes its trigger with it. The function is schema level and has to be
    # dropped by name, otherwise a downgrade leaves an orphan behind and the next upgrade fails
    # on CREATE FUNCTION.
    op.drop_table("razorpay_checkout")
    op.execute("DROP FUNCTION razorpay_checkout_guard()")
    op.drop_constraint("uq_payment_attempt_binding", "payment_attempt", type_="unique")
