"""create intent constraints

The authoritative semantic half of an authorization. Points worth knowing when reading
this:

- intent_constraint_set references spending_mandate through the composite (mandate_id,
  merchant_id), so a constraint set cannot be attached to a mandate granted to another
  merchant. Merchant integrity is transitive through it, since spending_mandate.merchant_id
  already references merchant, which is why there is no second foreign key straight to
  merchant.
- The unique constraint on mandate_id is what makes the binding one to one. Without it a
  caller could evaluate a checkout against whichever of several sets suited them, and an
  authorization whose terms the caller picks is not an authorization.
- intent_constraint references its set through (constraint_set_id, merchant_id), so a
  constraint cannot join another merchant's set.
- Both foreign keys are named explicitly. The metadata naming convention would produce
  names longer than 63 bytes and PostgreSQL truncates identifiers there, so the name
  written here and the name in the database would silently disagree.
- kind and operator are ordinary columns rather than fields inside the JSON document, so
  the shape of an authorization is readable in SQL and three check constraints can hold it:
  the value is a JSON scalar or a non empty array, an allowed category names no attribute
  and compares with IN, a required attribute names one, and the operator's promised
  comparison is actually possible against the stored value.
- Those checks are written with CASE rather than OR. PostgreSQL does not promise to
  evaluate the sides of an OR in order, and jsonb_array_length raises on a value that is
  not an array.
- uq_intent_constraint_target uses NULLS NOT DISTINCT so that it also covers the allowed
  category rows, whose attribute_key is null. Two EQ rules for one attribute are a
  contradiction rather than a tighter bound, and two allowed category rows would split one
  membership rule into two that each had to pass.
- The value column has no server default. A constraint with no value asks nothing, and
  defaulting one into existence would be an authorization rule that silently passes.
- One trigger function, attached to both tables, refusing UPDATE and DELETE outright.
  Authorization data has no lifecycle: it is written once with its mandate and then only
  read. This is what stops a later code path, ORM or otherwise, from turning "black only"
  into "any colour" after the fact. DROP is neither, so a downgrade still works, and
  TRUNCATE does not fire row triggers, so test cleanup is unaffected.

Revision ID: d62425ba115d
Revises: 4dc1a0f57b18
Created: 2026-08-21 17:57:14.740133
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d62425ba115d"
down_revision: str | None = "4dc1a0f57b18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AUTHORIZATION_GUARD = """
CREATE FUNCTION intent_constraint_authorization_guard() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'authorized intent constraints are immutable';
END;
$$ LANGUAGE plpgsql
"""

ATTACH_SET_GUARD = """
CREATE TRIGGER intent_constraint_set_authorization_guard
BEFORE UPDATE OR DELETE ON intent_constraint_set
FOR EACH ROW EXECUTE FUNCTION intent_constraint_authorization_guard()
"""

ATTACH_CONSTRAINT_GUARD = """
CREATE TRIGGER intent_constraint_authorization_guard
BEFORE UPDATE OR DELETE ON intent_constraint
FOR EACH ROW EXECUTE FUNCTION intent_constraint_authorization_guard()
"""


def upgrade() -> None:
    op.create_table(
        "intent_constraint_set",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("mandate_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["mandate_id", "merchant_id"],
            ["spending_mandate.id", "spending_mandate.merchant_id"],
            name="fk_intent_constraint_set_mandate",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_intent_constraint_set")),
        sa.UniqueConstraint(
            "id", "merchant_id", name=op.f("uq_intent_constraint_set_id_merchant_id")
        ),
        sa.UniqueConstraint("mandate_id", name=op.f("uq_intent_constraint_set_mandate_id")),
    )
    op.create_table(
        "intent_constraint",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("constraint_set_id", sa.Uuid(), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "ALLOWED_CATEGORY",
                "REQUIRED_ATTRIBUTE",
                name="intent_constraint_kind",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("attribute_key", sa.String(length=200), nullable=True),
        sa.Column(
            "operator",
            sa.Enum(
                "EQ",
                "NE",
                "GTE",
                "LTE",
                "IN",
                name="intent_constraint_operator",
                native_enum=False,
                create_constraint=False,
                length=8,
            ),
            nullable=False,
        ),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "CASE kind"
            " WHEN 'allowed_category'"
            " THEN attribute_key IS NULL AND operator = 'IN'"
            " WHEN 'required_attribute'"
            " THEN attribute_key IS NOT NULL AND length(btrim(attribute_key)) > 0"
            " ELSE false END",
            name=op.f("ck_intent_constraint_kind_shape"),
        ),
        sa.CheckConstraint(
            "CASE operator"
            " WHEN 'IN' THEN jsonb_typeof(value) = 'array'"
            " WHEN 'GTE' THEN jsonb_typeof(value) = 'number'"
            " WHEN 'LTE' THEN jsonb_typeof(value) = 'number'"
            " ELSE jsonb_typeof(value) <> 'array' END",
            name=op.f("ck_intent_constraint_operator_value_shape"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(value) IN ('string', 'number', 'boolean', 'array')"
            " AND CASE WHEN jsonb_typeof(value) = 'array'"
            " THEN jsonb_array_length(value) > 0 ELSE true END",
            name=op.f("ck_intent_constraint_value_shape"),
        ),
        sa.CheckConstraint(
            "kind IN ('allowed_category', 'required_attribute')",
            name=op.f("ck_intent_constraint_kind_known"),
        ),
        sa.CheckConstraint(
            "operator IN ('EQ', 'NE', 'GTE', 'LTE', 'IN')",
            name=op.f("ck_intent_constraint_operator_known"),
        ),
        sa.ForeignKeyConstraint(
            ["constraint_set_id", "merchant_id"],
            ["intent_constraint_set.id", "intent_constraint_set.merchant_id"],
            name="fk_intent_constraint_constraint_set",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_intent_constraint")),
        sa.UniqueConstraint(
            "constraint_set_id",
            "kind",
            "attribute_key",
            "operator",
            name="uq_intent_constraint_target",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        op.f("ix_intent_constraint_constraint_set_id"),
        "intent_constraint",
        ["constraint_set_id"],
        unique=False,
    )
    op.execute(AUTHORIZATION_GUARD)
    op.execute(ATTACH_SET_GUARD)
    op.execute(ATTACH_CONSTRAINT_GUARD)


def downgrade() -> None:
    # Dropping a table takes its triggers with it, and DROP is neither an UPDATE nor a
    # DELETE, so the guard does not stand in the way. The function is schema level and has
    # to be dropped by name, otherwise a downgrade leaves an orphan behind and the next
    # upgrade fails on CREATE FUNCTION.
    op.drop_index(op.f("ix_intent_constraint_constraint_set_id"), table_name="intent_constraint")
    op.drop_table("intent_constraint")
    op.drop_table("intent_constraint_set")
    op.execute("DROP FUNCTION intent_constraint_authorization_guard()")
