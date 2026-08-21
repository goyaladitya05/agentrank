"""add checkout ownership targets

Preparation for the checkout tables, and nothing else. Phase 1A made merchant isolation
structural by giving product a redundant unique constraint on (id, merchant_id) so that a
variant could be tied to its merchant through a composite foreign key. Checkout needs the
same guarantee twice more, so the two targets it needs are created here, ahead of the
tables that reference them.

- spending_mandate gains UNIQUE (id, merchant_id), so a checkout can be bound through
  (mandate_id, merchant_id) and cannot name a mandate granted to a different merchant.
- variant gains UNIQUE (id, merchant_id), so a checkout line can be bound through
  (variant_id, merchant_id) and cannot put another merchant's variant on a quote.
- spending_mandate gains an index on merchant_id. It was deliberately left out in Phase
  1B as speculative. It is not speculative now: the unique constraint above has id
  leftmost and does not serve a merchant scoped scan, and deleting a merchant has to
  check RESTRICT against this table.

None of this changes a row. Adding a unique constraint to a table that already holds data
builds an index over what is there, and both column pairs are already unique because id
alone is.

Revision ID: efb23d414a80
Revises: 9360057d8773
Created: 2026-08-21 17:20:18.576294
"""

from collections.abc import Sequence

from alembic import op

revision: str = "efb23d414a80"
down_revision: str | None = "9360057d8773"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        op.f("uq_spending_mandate_id_merchant_id"), "spending_mandate", ["id", "merchant_id"]
    )
    op.create_index(
        op.f("ix_spending_mandate_merchant_id"), "spending_mandate", ["merchant_id"], unique=False
    )
    op.create_unique_constraint(op.f("uq_variant_id_merchant_id"), "variant", ["id", "merchant_id"])


def downgrade() -> None:
    op.drop_constraint(op.f("uq_variant_id_merchant_id"), "variant", type_="unique")
    op.drop_index(op.f("ix_spending_mandate_merchant_id"), table_name="spending_mandate")
    op.drop_constraint(
        op.f("uq_spending_mandate_id_merchant_id"), "spending_mandate", type_="unique"
    )
