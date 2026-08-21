"""add checkout semantic snapshot

The two structured values semantic authorization is evaluated against, snapshotted onto
the quote line beside the price that was already snapshotted there. Points worth knowing
when reading this:

- Both columns describe what was quoted, not what the catalog says now. That is the whole
  reason they exist: a merchant changing a variant's colour from black to blue after a
  quote was written must not change whether that historical quote satisfied what the buyer
  asked for. Evaluating against the live catalog would make an authorization decision
  depend on when it was asked.
- Only structured commerce values are copied. Title and description stay in the catalog:
  the evaluator compares machine readable fields, and prose in a financial row would be
  duplication that answers nothing.
- product_category is nullable because product.category is. A catalog that does not say
  what something is cannot satisfy an allowed category constraint, and failing that check
  is the honest answer rather than a reason to infer a category from prose.
- variant_attributes carries the same check constraint the catalog holds on
  variant.attributes, so a consumer of either can rely on a JSON object.
- Adding a NOT NULL column with a server default fills existing rows without an UPDATE
  statement, which matters here: checkout_line_quote_guard refuses every UPDATE on this
  table. An ALTER TABLE is not an UPDATE, so the guard does not fire, and it needs no
  changes because it already refuses every update including ones touching these columns.
- Rows written before this migration therefore carry an empty attribute snapshot and no
  category. That is deliberate and it fails closed: a historical quote cannot satisfy a
  constraint about data nobody recorded, and backfilling from today's catalog would be
  inventing a snapshot that was never taken.

Revision ID: 70b5c985a47a
Revises: d62425ba115d
Created: 2026-08-21 18:07:06.386859
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "70b5c985a47a"
down_revision: str | None = "d62425ba115d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "checkout_line",
        sa.Column("product_category", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "checkout_line",
        sa.Column(
            "variant_attributes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_checkout_line_variant_attributes_are_an_object"),
        "checkout_line",
        "jsonb_typeof(variant_attributes) = 'object'",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_checkout_line_variant_attributes_are_an_object"),
        "checkout_line",
        type_="check",
    )
    op.drop_column("checkout_line", "variant_attributes")
    op.drop_column("checkout_line", "product_category")
