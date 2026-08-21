"""create product and variant

The catalog schema. Points worth knowing when reading this:

- Money is a BIGINT count of minor units with a non negative check, and its currency is a
  separate NOT NULL column checked against three uppercase letters. There is no code path
  that can store an amount without a currency, or a negative one.
- variant carries merchant_id and references product through a composite foreign key onto
  (product.id, product.merchant_id). That is what makes merchant isolation structural: a
  variant cannot be attributed to a merchant that does not own its product. The redundant
  unique constraint on product(id, merchant_id) exists only as that foreign key target.
- SKU is unique per merchant rather than per product, which is what a stock keeping unit
  means.
- attributes is JSONB, checked to be a JSON object rather than an array or a scalar.
- Deleting a merchant cascades to its products and on to their variants. Catalog rows have
  no meaning without their merchant, and orphans are worse than a cascade.
- The index on variant(product_id) is not speculative: a composite foreign key creates no
  index on the referencing side, so both variant lookup by product and the cascade delete
  would otherwise sequential scan.

Revision ID: ace599f8cce9
Revises: f7c298c3d582
Created: 2026-08-21 15:55:30.108654
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "ace599f8cce9"
down_revision: str | None = "f7c298c3d582"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "product",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("category", sa.String(length=120), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(btrim(external_id)) > 0", name=op.f("ck_product_external_id_not_blank")
        ),
        sa.CheckConstraint("length(btrim(title)) > 0", name=op.f("ck_product_title_not_blank")),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchant.id"],
            name=op.f("fk_product_merchant_id_merchant"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_product")),
        sa.UniqueConstraint("id", "merchant_id", name=op.f("uq_product_id_merchant_id")),
        sa.UniqueConstraint(
            "merchant_id", "external_id", name=op.f("uq_product_merchant_id_external_id")
        ),
    )
    op.create_table(
        "variant",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("sku", sa.String(length=128), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=True),
        sa.Column(
            "attributes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("price_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("inventory_quantity", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name=op.f("ck_variant_currency_format")),
        sa.CheckConstraint(
            "jsonb_typeof(attributes) = 'object'", name=op.f("ck_variant_attributes_are_an_object")
        ),
        sa.CheckConstraint(
            "inventory_quantity >= 0", name=op.f("ck_variant_inventory_not_negative")
        ),
        sa.CheckConstraint("length(btrim(sku)) > 0", name=op.f("ck_variant_sku_not_blank")),
        sa.CheckConstraint("price_amount_minor >= 0", name=op.f("ck_variant_price_not_negative")),
        sa.ForeignKeyConstraint(
            ["product_id", "merchant_id"],
            ["product.id", "product.merchant_id"],
            name=op.f("fk_variant_product_id_merchant_id_product"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_variant")),
        sa.UniqueConstraint("merchant_id", "sku", name=op.f("uq_variant_merchant_id_sku")),
    )
    op.create_index(op.f("ix_variant_product_id"), "variant", ["product_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_variant_product_id"), table_name="variant")
    op.drop_table("variant")
    op.drop_table("product")
