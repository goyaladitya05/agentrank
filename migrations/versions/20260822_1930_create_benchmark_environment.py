"""create benchmark environment

Which merchant a benchmark may overwrite, and which authored world a run was measured against.
Points worth knowing when reading this:

- benchmark_environment is the fail closed identity for a destructive operation. Preparing a
  world rewrites a merchant's catalog and releases the stock its missions were holding, and the
  environment service refuses to do either for a merchant with no row here. The table is the
  registration; there is no flag on merchant and no argument that stands in for one.
- uq_benchmark_environment_version makes a fixture key and version identify one world globally.
  A fixture names the merchant it describes, exactly as a suite names the merchant it was
  authored against, so the same version applied to two merchants would be two worlds claiming
  one identity.
- uq_benchmark_environment_binding is not a rule but a target. benchmark_run reaches this table
  through (environment_id, merchant_id), so a run cannot claim a world belonging to another
  merchant, whatever the application passes.
- benchmark_run.environment_id is nullable, and PostgreSQL skips a composite foreign key when
  any of its columns is null, which is the behavior wanted here: a run against a merchant nobody
  registered simply has no world to check. Null means the run was not executed against a
  registered world and never that the target was fine.
- The environment guard refuses UPDATE and DELETE outright, reusing the function published
  benchmark definitions already use. A registered world is what a historical run says it was
  measured against, so re-pointing one would rewrite what every earlier run means. Registering a
  new fixture version writes a new row and leaves the old one interpretable.
- environment_id joins the run guard's immutable list in the same migration. A target that can
  be rewritten after the fact is not a pin.

Revision ID: 3d4b17c9e5aa
Revises: 68ad857681f9
Created: 2026-08-22 19:30:11.402118
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3d4b17c9e5aa"
down_revision: str | None = "68ad857681f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KEY_PATTERN = "^[a-z0-9]+(-[a-z0-9]+)*$"
HASH_PATTERN = "^sha256:[0-9a-f]{64}$"

ATTACH_ENVIRONMENT_GUARD = """
CREATE TRIGGER benchmark_environment_definition_guard
BEFORE UPDATE OR DELETE ON benchmark_environment
FOR EACH ROW EXECUTE FUNCTION benchmark_definition_guard()
"""

RUN_GUARD = """
CREATE OR REPLACE FUNCTION benchmark_run_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
        OR NEW.merchant_id IS DISTINCT FROM OLD.merchant_id
        OR NEW.suite_id IS DISTINCT FROM OLD.suite_id
        OR NEW.environment_id IS DISTINCT FROM OLD.environment_id
        OR NEW.representation_label IS DISTINCT FROM OLD.representation_label
        OR NEW.catalog_hash IS DISTINCT FROM OLD.catalog_hash
        OR NEW.evaluator_version IS DISTINCT FROM OLD.evaluator_version
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION 'benchmark run ownership and identity are immutable';
    END IF;

    IF OLD.started_at IS NOT NULL AND NEW.started_at IS DISTINCT FROM OLD.started_at THEN
        RAISE EXCEPTION 'a benchmark run start time cannot be moved';
    END IF;

    IF OLD.status IN ('COMPLETED', 'ABORTED') THEN
        RAISE EXCEPTION 'a % benchmark run cannot be changed', lower(OLD.status);
    END IF;

    IF (OLD.status, NEW.status) NOT IN (
        ('PENDING', 'PENDING'),
        ('PENDING', 'RUNNING'),
        ('RUNNING', 'RUNNING'),
        ('RUNNING', 'COMPLETED'),
        ('RUNNING', 'ABORTED')
    ) THEN
        RAISE EXCEPTION 'benchmark run status cannot go from % to %', OLD.status, NEW.status;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

PREVIOUS_RUN_GUARD = RUN_GUARD.replace(
    "        OR NEW.environment_id IS DISTINCT FROM OLD.environment_id\n", ""
)


def upgrade() -> None:
    op.create_table(
        "benchmark_environment",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("fixture_key", sa.String(length=64), nullable=False),
        sa.Column("fixture_version", sa.Integer(), nullable=False),
        sa.Column("fixture_hash", sa.String(length=71), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"fixture_key ~ '{KEY_PATTERN}'",
            name=op.f("ck_benchmark_environment_fixture_key_format"),
        ),
        sa.CheckConstraint(
            "fixture_version > 0",
            name=op.f("ck_benchmark_environment_fixture_version_positive"),
        ),
        sa.CheckConstraint(
            f"fixture_hash ~ '{HASH_PATTERN}'",
            name=op.f("ck_benchmark_environment_fixture_hash_format"),
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchant.id"],
            name=op.f("fk_benchmark_environment_merchant_id_merchant"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_benchmark_environment")),
        sa.UniqueConstraint(
            "fixture_key", "fixture_version", name="uq_benchmark_environment_version"
        ),
        sa.UniqueConstraint("id", "merchant_id", name="uq_benchmark_environment_binding"),
    )
    op.create_index(
        op.f("ix_benchmark_environment_merchant_id"),
        "benchmark_environment",
        ["merchant_id"],
    )
    op.execute(ATTACH_ENVIRONMENT_GUARD)

    op.add_column("benchmark_run", sa.Column("environment_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_benchmark_run_environment",
        "benchmark_run",
        "benchmark_environment",
        ["environment_id", "merchant_id"],
        ["id", "merchant_id"],
        ondelete="RESTRICT",
    )
    op.execute(RUN_GUARD)


def downgrade() -> None:
    # Dropping the world reference loses the record of which authored target a historical run
    # was measured against, which is a real loss and not a reversible one. It is allowed rather
    # than refused for the same reason the catalog pin's downgrade is: the alternative to a run
    # with no target identity is no run at all.
    op.execute(PREVIOUS_RUN_GUARD)
    op.drop_constraint("fk_benchmark_run_environment", "benchmark_run", type_="foreignkey")
    op.drop_column("benchmark_run", "environment_id")
    # Dropping a table takes its trigger with it, and DROP is neither an UPDATE nor a DELETE, so
    # the guard does not stand in the way. The function is shared with the definition tables and
    # is deliberately left in place.
    op.drop_index(op.f("ix_benchmark_environment_merchant_id"), table_name="benchmark_environment")
    op.drop_table("benchmark_environment")
