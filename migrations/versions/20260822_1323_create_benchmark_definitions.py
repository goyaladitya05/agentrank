"""create benchmark definitions

The historical record of what a benchmark run measured. Points worth knowing when reading
this:

- benchmark_suite has no merchant_id and no foreign key to merchant. A suite is a global
  workload template that a run binds to a merchant, not a merchant's property. merchant_slug
  records which catalog the missions were authored against, as a slug rather than a
  reference, so a suite can be published before its merchant exists and stays readable after
  that merchant is gone.
- uq_benchmark_suite_version is the reproducibility guarantee at the storage layer. There is
  exactly one place a definition of `voltedge-core@1` can live, and the trigger below makes
  sure the thing living there never changes.
- definition_hash is a labelled sha256 over the canonicalised semantic content of the suite.
  It is what turns "somebody edited the fixture and forgot the version" from a silent
  reinterpretation of every historical result into a refusal at publish time.
- benchmark_mission separates the buyer facing columns from the two oracle columns,
  expected_outcome and simulated_value_amount_minor. The separation is a column list rather
  than a convention so that a projection handing an agent the answer is visible in a diff.
- ck_benchmark_mission_simulated_value_matches_outcome is what keeps simulated GMV honest at
  the storage layer. A mission whose purchase is available is worth something, and one for
  which nothing acceptable exists is worth nothing, so potential GMV cannot be inflated by a
  sale that could never have happened. It is written with CASE rather than OR because
  PostgreSQL does not promise to evaluate the sides of an OR in order.
- Both unique constraints on benchmark_mission have suite_id leftmost, so a lookup by suite
  and the RESTRICT check are served without a further index.
- One trigger function, attached to both tables, refusing UPDATE and DELETE outright. A
  published definition has no lifecycle: it is written once and then only read. This is what
  stops a later code path, ORM or otherwise, from changing what a historical run measured.
  DROP is neither, so a downgrade still works, and TRUNCATE does not fire row triggers, so
  test cleanup is unaffected.

Revision ID: a9c07ae31e5e
Revises: 5b3f27ad9e14
Created: 2026-08-22 13:23:06.918278
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a9c07ae31e5e"
down_revision: str | None = "5b3f27ad9e14"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFINITION_GUARD = """
CREATE FUNCTION benchmark_definition_guard() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'published benchmark definitions are immutable';
END;
$$ LANGUAGE plpgsql
"""

ATTACH_SUITE_GUARD = """
CREATE TRIGGER benchmark_suite_definition_guard
BEFORE UPDATE OR DELETE ON benchmark_suite
FOR EACH ROW EXECUTE FUNCTION benchmark_definition_guard()
"""

ATTACH_MISSION_GUARD = """
CREATE TRIGGER benchmark_mission_definition_guard
BEFORE UPDATE OR DELETE ON benchmark_mission
FOR EACH ROW EXECUTE FUNCTION benchmark_definition_guard()
"""


def upgrade() -> None:
    op.create_table(
        "benchmark_suite",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("suite_key", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("merchant_slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("definition_hash", sa.String(length=71), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "definition_hash ~ '^sha256:[0-9a-f]{64}$'",
            name=op.f("ck_benchmark_suite_definition_hash_format"),
        ),
        sa.CheckConstraint(
            "merchant_slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'",
            name=op.f("ck_benchmark_suite_merchant_slug_format"),
        ),
        sa.CheckConstraint(
            "suite_key ~ '^[a-z0-9]+(-[a-z0-9]+)*$'",
            name=op.f("ck_benchmark_suite_suite_key_format"),
        ),
        sa.CheckConstraint(
            "length(btrim(name)) > 0", name=op.f("ck_benchmark_suite_name_not_blank")
        ),
        sa.CheckConstraint("version > 0", name=op.f("ck_benchmark_suite_version_positive")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_benchmark_suite")),
        sa.UniqueConstraint("suite_key", "version", name="uq_benchmark_suite_version"),
    )
    op.create_table(
        "benchmark_mission",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("suite_id", sa.Uuid(), nullable=False),
        sa.Column("mission_key", sa.String(length=64), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("objective", sa.String(length=1000), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("budget_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column(
            "hard_constraints",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "preferences",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "expected_outcome",
            sa.Enum(
                "PURCHASE_AVAILABLE",
                "NO_ACCEPTABLE_PURCHASE",
                name="benchmark_expected_outcome",
                native_enum=False,
                create_constraint=False,
                length=24,
            ),
            nullable=False,
        ),
        sa.Column("simulated_value_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "CASE expected_outcome"
            " WHEN 'PURCHASE_AVAILABLE' THEN simulated_value_amount_minor > 0"
            " WHEN 'NO_ACCEPTABLE_PURCHASE' THEN simulated_value_amount_minor = 0"
            " ELSE false END",
            name=op.f("ck_benchmark_mission_simulated_value_matches_outcome"),
        ),
        sa.CheckConstraint(
            "currency ~ '^[A-Z]{3}$'", name=op.f("ck_benchmark_mission_currency_format")
        ),
        sa.CheckConstraint(
            "expected_outcome IN ('PURCHASE_AVAILABLE', 'NO_ACCEPTABLE_PURCHASE')",
            name=op.f("ck_benchmark_mission_expected_outcome_known"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(hard_constraints) = 'array'",
            name=op.f("ck_benchmark_mission_hard_constraints_shape"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(preferences) = 'array'",
            name=op.f("ck_benchmark_mission_preferences_shape"),
        ),
        sa.CheckConstraint(
            "mission_key ~ '^[a-z0-9]+(-[a-z0-9]+)*$'",
            name=op.f("ck_benchmark_mission_mission_key_format"),
        ),
        sa.CheckConstraint(
            "budget_amount_minor > 0", name=op.f("ck_benchmark_mission_budget_positive")
        ),
        sa.CheckConstraint(
            "length(btrim(objective)) > 0", name=op.f("ck_benchmark_mission_objective_not_blank")
        ),
        sa.CheckConstraint("ordinal >= 0", name=op.f("ck_benchmark_mission_ordinal_not_negative")),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_benchmark_mission_quantity_positive")),
        sa.ForeignKeyConstraint(
            ["suite_id"],
            ["benchmark_suite.id"],
            name=op.f("fk_benchmark_mission_suite_id_benchmark_suite"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_benchmark_mission")),
        sa.UniqueConstraint("suite_id", "mission_key", name="uq_benchmark_mission_key"),
        sa.UniqueConstraint("suite_id", "ordinal", name="uq_benchmark_mission_ordinal"),
    )
    op.execute(DEFINITION_GUARD)
    op.execute(ATTACH_SUITE_GUARD)
    op.execute(ATTACH_MISSION_GUARD)


def downgrade() -> None:
    # Dropping a table takes its triggers with it, and DROP is neither an UPDATE nor a DELETE,
    # so the guard does not stand in the way. The function is schema level and has to be
    # dropped by name, otherwise a downgrade leaves an orphan behind and the next upgrade
    # fails on CREATE FUNCTION.
    op.drop_table("benchmark_mission")
    op.drop_table("benchmark_suite")
    op.execute("DROP FUNCTION benchmark_definition_guard()")
