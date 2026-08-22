"""harden benchmark run integrity

An independent database review of the Phase 2A schema found that the run tables enforced much
less than their triggers appeared to. This closes what it found.

Points worth knowing when reading this:

- benchmark_mission_run gains suite_id and both its parent foreign keys become composite over
  it. A mission run could previously name a mission from a suite its run never executed, which
  meant a run could carry results for missions it never contained and a report could read an
  oracle from a workload nobody ran. Verified before the fix: the insert succeeded.
- The two lifecycle triggers only ever covered UPDATE. That left three holes and all three were
  reproduced: a mission result could be transitioned after its run was closed, a fresh mission
  run could be inserted into a finished run, and an entire fabricated COMPLETED run with
  SUCCEEDED results could be written as plain INSERTs without a trigger firing at all. A
  transition whitelist that governs only UPDATE does not say "a result must be produced by a
  transition", it says "a result must not be edited into place afterwards", and only the second
  of those was true.
- So both tables gain BEFORE INSERT guards. A mission run is inserted PENDING into a PENDING
  run and in no other shape, which is also what makes the run's shape genuinely fixed before
  execution rather than merely intended to be.
- And both gain BEFORE DELETE guards. The previous revision claimed deletion was held off by
  the RESTRICT references pointing at these rows; that was wrong, because every RESTRICT
  reference on benchmark_mission_run points away from it and holds nothing. A recorded result
  could simply be deleted, and deleting a finished run took all of its results with it. The
  mission run guard detects a legitimate cascade by looking for its parent: during ON DELETE
  CASCADE the parent row is already gone when the child trigger runs, so the cascade passes and
  a standalone delete is refused.
- Three partial indexes cover the RESTRICT probes for variant, checkout and payment. Without
  them every delete of one of those scanned every mission run the merchant had accumulated,
  which was measured with EXPLAIN rather than assumed. The index on merchant_id alone is
  dropped: this table has no direct foreign key to merchant, so no integrity check targets it,
  and every read reaches a mission run through its run.
- The backfill of suite_id disables the mission run guard for the length of one statement,
  because the guard refuses updates to recorded results and a backfill is exactly that. There
  are no rows to move in practice, and a migration that only works on an empty table is not a
  migration.

Revision ID: 08b8b602aa7d
Revises: bc02a36a0a78
Created: 2026-08-22 14:58:11.402238
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "08b8b602aa7d"
down_revision: str | None = "bc02a36a0a78"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BACKFILL_SUITE = """
UPDATE benchmark_mission_run
SET suite_id = benchmark_run.suite_id
FROM benchmark_run
WHERE benchmark_run.id = benchmark_mission_run.run_id
"""

MISSION_RUN_GUARD = """
CREATE OR REPLACE FUNCTION benchmark_mission_run_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
        OR NEW.run_id IS DISTINCT FROM OLD.run_id
        OR NEW.merchant_id IS DISTINCT FROM OLD.merchant_id
        OR NEW.suite_id IS DISTINCT FROM OLD.suite_id
        OR NEW.mission_id IS DISTINCT FROM OLD.mission_id
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION 'benchmark mission run ownership and identity are immutable';
    END IF;

    IF OLD.started_at IS NOT NULL AND NEW.started_at IS DISTINCT FROM OLD.started_at THEN
        RAISE EXCEPTION 'a benchmark mission run start time cannot be moved';
    END IF;

    IF (SELECT status FROM benchmark_run WHERE id = NEW.run_id)
        IN ('COMPLETED', 'ABORTED')
    THEN
        RAISE EXCEPTION 'a mission result cannot be changed once its run has finished';
    END IF;

    IF OLD.status IN ('SUCCEEDED', 'FAILED', 'ABSTAINED', 'ERRORED') THEN
        RAISE EXCEPTION 'a recorded benchmark mission result cannot be changed';
    END IF;

    IF (OLD.status, NEW.status) NOT IN (
        ('PENDING', 'PENDING'),
        ('PENDING', 'RUNNING'),
        ('RUNNING', 'RUNNING'),
        ('RUNNING', 'SUCCEEDED'),
        ('RUNNING', 'FAILED'),
        ('RUNNING', 'ABSTAINED'),
        ('RUNNING', 'ERRORED')
    ) THEN
        RAISE EXCEPTION 'benchmark mission run status cannot go from % to %',
            OLD.status, NEW.status;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

PREVIOUS_MISSION_RUN_GUARD = """
CREATE OR REPLACE FUNCTION benchmark_mission_run_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
        OR NEW.run_id IS DISTINCT FROM OLD.run_id
        OR NEW.merchant_id IS DISTINCT FROM OLD.merchant_id
        OR NEW.mission_id IS DISTINCT FROM OLD.mission_id
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION 'benchmark mission run ownership and identity are immutable';
    END IF;

    IF OLD.started_at IS NOT NULL AND NEW.started_at IS DISTINCT FROM OLD.started_at THEN
        RAISE EXCEPTION 'a benchmark mission run start time cannot be moved';
    END IF;

    IF OLD.status IN ('SUCCEEDED', 'FAILED', 'ABSTAINED', 'ERRORED') THEN
        RAISE EXCEPTION 'a recorded benchmark mission result cannot be changed';
    END IF;

    IF (OLD.status, NEW.status) NOT IN (
        ('PENDING', 'PENDING'),
        ('PENDING', 'RUNNING'),
        ('RUNNING', 'RUNNING'),
        ('RUNNING', 'SUCCEEDED'),
        ('RUNNING', 'FAILED'),
        ('RUNNING', 'ABSTAINED'),
        ('RUNNING', 'ERRORED')
    ) THEN
        RAISE EXCEPTION 'benchmark mission run status cannot go from % to %',
            OLD.status, NEW.status;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

MISSION_RUN_INSERT_GUARD = """
CREATE FUNCTION benchmark_mission_run_insert_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.status <> 'PENDING' THEN
        RAISE EXCEPTION 'a benchmark mission result is produced by a transition, not written';
    END IF;

    IF (SELECT status FROM benchmark_run WHERE id = NEW.run_id) <> 'PENDING' THEN
        RAISE EXCEPTION 'a benchmark run takes its missions before it starts';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

MISSION_RUN_DELETE_GUARD = """
CREATE FUNCTION benchmark_mission_run_delete_guard() RETURNS trigger AS $$
BEGIN
    -- Present means this is a standalone delete. During ON DELETE CASCADE the parent row is
    -- already gone by the time this runs, so a legitimate cascade passes and removing one
    -- result out from under a run that still exists does not.
    IF EXISTS (SELECT 1 FROM benchmark_run WHERE id = OLD.run_id) THEN
        RAISE EXCEPTION 'a recorded benchmark mission result cannot be deleted';
    END IF;

    RETURN OLD;
END;
$$ LANGUAGE plpgsql
"""

RUN_DELETE_GUARD = """
CREATE FUNCTION benchmark_run_delete_guard() RETURNS trigger AS $$
BEGIN
    IF OLD.status IN ('COMPLETED', 'ABORTED') THEN
        RAISE EXCEPTION 'a finished benchmark run cannot be deleted';
    END IF;

    RETURN OLD;
END;
$$ LANGUAGE plpgsql
"""

ATTACH = (
    "CREATE TRIGGER benchmark_mission_run_insert_guard BEFORE INSERT ON benchmark_mission_run"
    " FOR EACH ROW EXECUTE FUNCTION benchmark_mission_run_insert_guard()",
    "CREATE TRIGGER benchmark_mission_run_delete_guard BEFORE DELETE ON benchmark_mission_run"
    " FOR EACH ROW EXECUTE FUNCTION benchmark_mission_run_delete_guard()",
    "CREATE TRIGGER benchmark_run_delete_guard BEFORE DELETE ON benchmark_run"
    " FOR EACH ROW EXECUTE FUNCTION benchmark_run_delete_guard()",
)

VARIANT_INDEX = "ix_benchmark_mission_run_selected_variant_id_merchant_id"
CHECKOUT_INDEX = "ix_benchmark_mission_run_checkout_id_merchant_id"
PAYMENT_INDEX = "ix_benchmark_mission_run_payment_attempt_id_merchant_id"
MERCHANT_INDEX = "ix_benchmark_mission_run_merchant_id"


def upgrade() -> None:
    op.add_column("benchmark_mission_run", sa.Column("suite_id", sa.Uuid(), nullable=True))
    # The guard refuses updates to recorded results, and a backfill is exactly that.
    op.execute("ALTER TABLE benchmark_mission_run DISABLE TRIGGER benchmark_mission_run_guard")
    op.execute(BACKFILL_SUITE)
    op.execute("ALTER TABLE benchmark_mission_run ENABLE TRIGGER benchmark_mission_run_guard")
    op.alter_column("benchmark_mission_run", "suite_id", nullable=False)

    op.create_unique_constraint(
        "uq_benchmark_mission_suite", "benchmark_mission", ["id", "suite_id"]
    )
    op.create_unique_constraint(
        "uq_benchmark_run_binding", "benchmark_run", ["id", "merchant_id", "suite_id"]
    )

    op.drop_constraint("fk_benchmark_mission_run_run", "benchmark_mission_run", type_="foreignkey")
    op.drop_constraint(
        "fk_benchmark_mission_run_mission", "benchmark_mission_run", type_="foreignkey"
    )
    op.drop_constraint("uq_benchmark_run_ownership", "benchmark_run", type_="unique")
    op.create_foreign_key(
        "fk_benchmark_mission_run_run",
        "benchmark_mission_run",
        "benchmark_run",
        ["run_id", "merchant_id", "suite_id"],
        ["id", "merchant_id", "suite_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_benchmark_mission_run_mission",
        "benchmark_mission_run",
        "benchmark_mission",
        ["mission_id", "suite_id"],
        ["id", "suite_id"],
        ondelete="RESTRICT",
    )

    op.create_index(
        VARIANT_INDEX,
        "benchmark_mission_run",
        ["selected_variant_id", "merchant_id"],
        unique=False,
        postgresql_where=sa.text("selected_variant_id IS NOT NULL"),
    )
    op.create_index(
        CHECKOUT_INDEX,
        "benchmark_mission_run",
        ["checkout_id", "merchant_id"],
        unique=False,
        postgresql_where=sa.text("checkout_id IS NOT NULL"),
    )
    op.create_index(
        PAYMENT_INDEX,
        "benchmark_mission_run",
        ["payment_attempt_id", "merchant_id"],
        unique=False,
        postgresql_where=sa.text("payment_attempt_id IS NOT NULL"),
    )
    op.drop_index(MERCHANT_INDEX, table_name="benchmark_mission_run")

    op.execute(MISSION_RUN_GUARD)
    op.execute(MISSION_RUN_INSERT_GUARD)
    op.execute(MISSION_RUN_DELETE_GUARD)
    op.execute(RUN_DELETE_GUARD)
    for statement in ATTACH:
        op.execute(statement)


def downgrade() -> None:
    # Reversible without loss of meaning: dropping suite_id loses a denormalized copy of a
    # column the run still carries, and the guards it removes only ever refused writes.
    op.execute("DROP TRIGGER benchmark_run_delete_guard ON benchmark_run")
    op.execute("DROP TRIGGER benchmark_mission_run_delete_guard ON benchmark_mission_run")
    op.execute("DROP TRIGGER benchmark_mission_run_insert_guard ON benchmark_mission_run")
    op.execute("DROP FUNCTION benchmark_run_delete_guard()")
    op.execute("DROP FUNCTION benchmark_mission_run_delete_guard()")
    op.execute("DROP FUNCTION benchmark_mission_run_insert_guard()")
    op.execute(PREVIOUS_MISSION_RUN_GUARD)

    op.create_index(MERCHANT_INDEX, "benchmark_mission_run", ["merchant_id"], unique=False)
    op.drop_index(PAYMENT_INDEX, table_name="benchmark_mission_run")
    op.drop_index(CHECKOUT_INDEX, table_name="benchmark_mission_run")
    op.drop_index(VARIANT_INDEX, table_name="benchmark_mission_run")

    op.drop_constraint(
        "fk_benchmark_mission_run_mission", "benchmark_mission_run", type_="foreignkey"
    )
    op.drop_constraint("fk_benchmark_mission_run_run", "benchmark_mission_run", type_="foreignkey")
    op.create_unique_constraint(
        "uq_benchmark_run_ownership", "benchmark_run", ["id", "merchant_id"]
    )
    op.create_foreign_key(
        "fk_benchmark_mission_run_run",
        "benchmark_mission_run",
        "benchmark_run",
        ["run_id", "merchant_id"],
        ["id", "merchant_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_benchmark_mission_run_mission",
        "benchmark_mission_run",
        "benchmark_mission",
        ["mission_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.drop_constraint("uq_benchmark_run_binding", "benchmark_run", type_="unique")
    op.drop_constraint("uq_benchmark_mission_suite", "benchmark_mission", type_="unique")
    op.drop_column("benchmark_mission_run", "suite_id")
