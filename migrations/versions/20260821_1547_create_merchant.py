"""create merchant

The first table with real DDL. Slug is unique and format checked because it will appear
in URLs and in fixture identity. Name is checked non blank rather than merely NOT NULL,
since an empty string is not a name.

Constraint names are wrapped in op.f so that they are used verbatim. Without it the
metadata naming convention is applied a second time and produces names like
ck_merchant_ck_merchant_name_not_blank. A migration is a historical record and must not
change its output if the convention is ever edited.

Revision ID: f7c298c3d582
Revises: 47e8b9946f4c
Created: 2026-08-21 15:47:39.815784
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f7c298c3d582"
down_revision: str | None = "47e8b9946f4c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "merchant",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
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
            "slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'",
            name=op.f("ck_merchant_slug_format"),
        ),
        sa.CheckConstraint("length(btrim(name)) > 0", name=op.f("ck_merchant_name_not_blank")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_merchant")),
        sa.UniqueConstraint("slug", name=op.f("uq_merchant_slug")),
    )


def downgrade() -> None:
    op.drop_table("merchant")
