"""name the launch record for both evaluations it holds and give it a purpose

`benchmark_reevaluation` was written when measuring a published representation again was the
only thing a merchant could ask for. A merchant with no compiler representation at all could not
ask for anything, which left the first measurement of a merchant to an operator shell. This
makes the same record hold both commands rather than adding a second one beside it: the same
request key, the same one-pending-launch rule, the same worker claim and the same settlement
against the run it produced, with `purpose` saying which kind of evaluation was admitted.

The table, its constraints, its indexes and its two trigger functions are renamed rather than
recreated, so every launch a merchant has already made keeps its identity and its history.

Three shape rules move into the database:

- each purpose names exactly the artifact it measures. INITIAL carries a source snapshot and no
  Commerce IR representation and no compiler run; REEVALUATION carries the representation and
  the compiler run behind it and no source snapshot.
- an initial evaluation has no baseline run. A merchant's first evaluation has no before, and
  a column holding one on such a row is the one way this schema could say otherwise.
- `purpose` and `source_snapshot_id` join the frozen identity the update guard refuses to let
  move, for the same reason every other identity column is already there.

Existing rows are re-evaluations and are backfilled as such, which is what they have always
been rather than a default chosen here.

Revision ID: b6c2e9f4a7d1
Revises: a4b7c1d9e2f5
Created: 2026-08-25 19:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b6c2e9f4a7d1"
down_revision: str | None = "a4b7c1d9e2f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_TABLE = "benchmark_reevaluation"
NEW_TABLE = "benchmark_evaluation_launch"

# Every constraint on the table, old suffix to new. Most keep their suffix and change only the
# table name inside the prefix; `buyer_configuration_matches_profile` is shortened as well,
# because the longer table name would push the generated identifier past PostgreSQL's 63
# character limit and leave a silently truncated name nothing could name again.
CONSTRAINTS: tuple[tuple[str, str], ...] = (
    ("pk_benchmark_reevaluation", "pk_benchmark_evaluation_launch"),
    ("uq_benchmark_reevaluation_request", "uq_benchmark_evaluation_launch_request"),
    ("uq_benchmark_reevaluation_run", "uq_benchmark_evaluation_launch_run"),
    (
        "fk_benchmark_reevaluation_merchant_id_merchant",
        "fk_benchmark_evaluation_launch_merchant_id_merchant",
    ),
    (
        "fk_benchmark_reevaluation_suite_id_benchmark_suite",
        "fk_benchmark_evaluation_launch_suite_id_benchmark_suite",
    ),
    (
        "fk_benchmark_reevaluation_representation",
        "fk_benchmark_evaluation_launch_representation",
    ),
    ("fk_benchmark_reevaluation_compiler_run", "fk_benchmark_evaluation_launch_compiler_run"),
    ("fk_benchmark_reevaluation_environment", "fk_benchmark_evaluation_launch_environment"),
    ("fk_benchmark_reevaluation_run", "fk_benchmark_evaluation_launch_run"),
    ("fk_benchmark_reevaluation_baseline", "fk_benchmark_evaluation_launch_baseline"),
    (
        "ck_benchmark_reevaluation_baseline_is_another_run",
        "ck_benchmark_evaluation_launch_baseline_is_another_run",
    ),
    (
        "ck_benchmark_reevaluation_buyer_configuration_matches_profile",
        "ck_benchmark_evaluation_launch_buyer_matches_profile",
    ),
    (
        "ck_benchmark_reevaluation_buyer_configuration_object",
        "ck_benchmark_evaluation_launch_buyer_configuration_object",
    ),
    (
        "ck_benchmark_reevaluation_buyer_digest_format",
        "ck_benchmark_evaluation_launch_buyer_digest_format",
    ),
    (
        "ck_benchmark_reevaluation_buyer_digest_shape",
        "ck_benchmark_evaluation_launch_buyer_digest_shape",
    ),
    (
        "ck_benchmark_reevaluation_buyer_profile_known",
        "ck_benchmark_evaluation_launch_buyer_profile_known",
    ),
    (
        "ck_benchmark_reevaluation_executing_is_not_settled",
        "ck_benchmark_evaluation_launch_executing_is_not_settled",
    ),
    (
        "ck_benchmark_reevaluation_executing_shape",
        "ck_benchmark_evaluation_launch_executing_shape",
    ),
    (
        "ck_benchmark_reevaluation_executor_kind_format",
        "ck_benchmark_evaluation_launch_executor_kind_format",
    ),
    ("ck_benchmark_reevaluation_failure_shape", "ck_benchmark_evaluation_launch_failure_shape"),
    ("ck_benchmark_reevaluation_queued_shape", "ck_benchmark_evaluation_launch_queued_shape"),
    (
        "ck_benchmark_reevaluation_request_key_format",
        "ck_benchmark_evaluation_launch_request_key_format",
    ),
    (
        "ck_benchmark_reevaluation_run_needs_a_start",
        "ck_benchmark_evaluation_launch_run_needs_a_start",
    ),
    (
        "ck_benchmark_reevaluation_settle_after_start",
        "ck_benchmark_evaluation_launch_settle_after_start",
    ),
    ("ck_benchmark_reevaluation_settle_order", "ck_benchmark_evaluation_launch_settle_order"),
    ("ck_benchmark_reevaluation_settled_shape", "ck_benchmark_evaluation_launch_settled_shape"),
    ("ck_benchmark_reevaluation_start_order", "ck_benchmark_evaluation_launch_start_order"),
    ("ck_benchmark_reevaluation_status_known", "ck_benchmark_evaluation_launch_status_known"),
)

# The indexes that are not backed by a constraint. Renaming a constraint renames its index, so
# the two unique constraints above are deliberately absent here.
INDEXES: tuple[tuple[str, str], ...] = (
    (
        "uq_benchmark_reevaluation_pending_merchant",
        "uq_benchmark_evaluation_launch_pending_merchant",
    ),
    (
        "ix_benchmark_reevaluation_baseline_run_id_merchant_id",
        "ix_benchmark_evaluation_launch_baseline_run_id_merchant_id",
    ),
    (
        "ix_benchmark_reevaluation_compiler_run_id_merchant_id",
        "ix_benchmark_evaluation_launch_compiler_run_id_merchant_id",
    ),
    (
        "ix_benchmark_reevaluation_environment_id_merchant_id",
        "ix_benchmark_evaluation_launch_environment_id_merchant_id",
    ),
    ("ix_benchmark_reevaluation_merchant_id", "ix_benchmark_evaluation_launch_merchant_id"),
    (
        "ix_benchmark_reevaluation_representation_id_merchant_id",
        "ix_benchmark_evaluation_launch_representation_id_merchant_id",
    ),
    ("ix_benchmark_reevaluation_suite_id", "ix_benchmark_evaluation_launch_suite_id"),
)

SOURCE_INDEX = "ix_benchmark_evaluation_launch_source_snapshot_id_merchant_id"

GUARD = """
CREATE FUNCTION benchmark_evaluation_launch_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF NEW.id IS DISTINCT FROM OLD.id
            OR NEW.merchant_id IS DISTINCT FROM OLD.merchant_id
            OR NEW.request_key IS DISTINCT FROM OLD.request_key
            OR NEW.purpose IS DISTINCT FROM OLD.purpose
            OR NEW.representation_id IS DISTINCT FROM OLD.representation_id
            OR NEW.compiler_run_id IS DISTINCT FROM OLD.compiler_run_id
            OR NEW.source_snapshot_id IS DISTINCT FROM OLD.source_snapshot_id
            OR NEW.suite_id IS DISTINCT FROM OLD.suite_id
            OR NEW.environment_id IS DISTINCT FROM OLD.environment_id
            OR NEW.buyer_profile IS DISTINCT FROM OLD.buyer_profile
            OR NEW.buyer_configuration IS DISTINCT FROM OLD.buyer_configuration
            OR NEW.buyer_configuration_digest IS DISTINCT FROM OLD.buyer_configuration_digest
            OR NEW.executor_kind IS DISTINCT FROM OLD.executor_kind
            OR NEW.baseline_run_id IS DISTINCT FROM OLD.baseline_run_id
            OR NEW.requested_at IS DISTINCT FROM OLD.requested_at
        THEN
            RAISE EXCEPTION 'benchmark evaluation launch identity is frozen at admission';
        END IF;

        IF OLD.run_id IS NOT NULL AND NEW.run_id IS DISTINCT FROM OLD.run_id THEN
            RAISE EXCEPTION 'a benchmark evaluation launch cannot be moved to another run';
        END IF;

        IF (OLD.status, NEW.status) NOT IN (
            ('QUEUED', 'QUEUED'),
            ('QUEUED', 'EXECUTING'),
            ('QUEUED', 'FAILED'),
            ('EXECUTING', 'EXECUTING'),
            ('EXECUTING', 'COMPLETED'),
            ('EXECUTING', 'FAILED')
        ) THEN
            RAISE EXCEPTION 'benchmark evaluation launch status cannot go from % to %',
                OLD.status, NEW.status;
        END IF;
    ELSIF NEW.status <> 'QUEUED' THEN
        RAISE EXCEPTION 'a benchmark evaluation launch is admitted queued, never written settled';
    END IF;

    IF NEW.status = 'COMPLETED' AND (
        SELECT status FROM benchmark_run WHERE id = NEW.run_id
    ) IS DISTINCT FROM 'COMPLETED' THEN
        RAISE EXCEPTION 'a completed evaluation launch must name a completed benchmark run';
    END IF;

    IF NEW.status = 'FAILED' AND NEW.run_id IS NOT NULL AND (
        SELECT status FROM benchmark_run WHERE id = NEW.run_id
    ) IS DISTINCT FROM 'ABORTED' THEN
        RAISE EXCEPTION 'a failed evaluation launch that names a run must name an aborted one';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

DELETE_GUARD = """
CREATE FUNCTION benchmark_evaluation_launch_delete_guard() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'a benchmark evaluation launch is evidence and cannot be deleted';
END;
$$ LANGUAGE plpgsql
"""

OLD_GUARD = """
CREATE FUNCTION benchmark_reevaluation_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF NEW.id IS DISTINCT FROM OLD.id
            OR NEW.merchant_id IS DISTINCT FROM OLD.merchant_id
            OR NEW.request_key IS DISTINCT FROM OLD.request_key
            OR NEW.representation_id IS DISTINCT FROM OLD.representation_id
            OR NEW.compiler_run_id IS DISTINCT FROM OLD.compiler_run_id
            OR NEW.suite_id IS DISTINCT FROM OLD.suite_id
            OR NEW.environment_id IS DISTINCT FROM OLD.environment_id
            OR NEW.buyer_profile IS DISTINCT FROM OLD.buyer_profile
            OR NEW.buyer_configuration IS DISTINCT FROM OLD.buyer_configuration
            OR NEW.buyer_configuration_digest IS DISTINCT FROM OLD.buyer_configuration_digest
            OR NEW.executor_kind IS DISTINCT FROM OLD.executor_kind
            OR NEW.baseline_run_id IS DISTINCT FROM OLD.baseline_run_id
            OR NEW.requested_at IS DISTINCT FROM OLD.requested_at
        THEN
            RAISE EXCEPTION 'benchmark re-evaluation identity is frozen at admission';
        END IF;

        IF OLD.run_id IS NOT NULL AND NEW.run_id IS DISTINCT FROM OLD.run_id THEN
            RAISE EXCEPTION 'a benchmark re-evaluation cannot be moved to another run';
        END IF;

        IF (OLD.status, NEW.status) NOT IN (
            ('QUEUED', 'QUEUED'),
            ('QUEUED', 'EXECUTING'),
            ('QUEUED', 'FAILED'),
            ('EXECUTING', 'EXECUTING'),
            ('EXECUTING', 'COMPLETED'),
            ('EXECUTING', 'FAILED')
        ) THEN
            RAISE EXCEPTION 'benchmark re-evaluation status cannot go from % to %',
                OLD.status, NEW.status;
        END IF;
    ELSIF NEW.status <> 'QUEUED' THEN
        RAISE EXCEPTION 'a benchmark re-evaluation is admitted queued, never written settled';
    END IF;

    IF NEW.status = 'COMPLETED' AND (
        SELECT status FROM benchmark_run WHERE id = NEW.run_id
    ) IS DISTINCT FROM 'COMPLETED' THEN
        RAISE EXCEPTION 'a completed re-evaluation must name a completed benchmark run';
    END IF;

    IF NEW.status = 'FAILED' AND NEW.run_id IS NOT NULL AND (
        SELECT status FROM benchmark_run WHERE id = NEW.run_id
    ) IS DISTINCT FROM 'ABORTED' THEN
        RAISE EXCEPTION 'a failed re-evaluation that names a run must name an aborted one';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

OLD_DELETE_GUARD = """
CREATE FUNCTION benchmark_reevaluation_delete_guard() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'a benchmark re-evaluation is evidence and cannot be deleted';
END;
$$ LANGUAGE plpgsql
"""

# A downgrade cannot express an initial evaluation, so it refuses rather than rewriting one
# into a re-evaluation of a representation it never measured.
REFUSE_INITIAL = """
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM benchmark_evaluation_launch WHERE purpose = 'INITIAL'
    ) THEN
        RAISE EXCEPTION 'initial evaluation launches cannot be represented before this revision';
    END IF;
END
$$
"""

PURPOSE_SHAPE = (
    "(purpose = 'INITIAL' AND representation_id IS NULL AND compiler_run_id IS NULL"
    " AND source_snapshot_id IS NOT NULL)"
    " OR (purpose = 'REEVALUATION' AND representation_id IS NOT NULL"
    " AND compiler_run_id IS NOT NULL AND source_snapshot_id IS NULL)"
)


def upgrade() -> None:
    op.execute("DROP TRIGGER benchmark_reevaluation_delete_guard ON benchmark_reevaluation")
    op.execute("DROP TRIGGER benchmark_reevaluation_guard ON benchmark_reevaluation")
    op.execute("DROP FUNCTION benchmark_reevaluation_delete_guard()")
    op.execute("DROP FUNCTION benchmark_reevaluation_guard()")

    op.rename_table(OLD_TABLE, NEW_TABLE)
    for old, new in CONSTRAINTS:
        op.execute(f"ALTER TABLE {NEW_TABLE} RENAME CONSTRAINT {old} TO {new}")
    for old, new in INDEXES:
        op.execute(f"ALTER INDEX {old} RENAME TO {new}")

    op.add_column(NEW_TABLE, sa.Column("purpose", sa.String(length=16), nullable=True))
    op.add_column(NEW_TABLE, sa.Column("source_snapshot_id", sa.Uuid(), nullable=True))
    # Every launch that exists was a request to measure a published representation again, so
    # this states what those rows already are rather than choosing a default for them.
    op.execute("UPDATE benchmark_evaluation_launch SET purpose = 'REEVALUATION'")
    op.alter_column(NEW_TABLE, "purpose", nullable=False)
    op.alter_column(NEW_TABLE, "representation_id", existing_type=sa.Uuid(), nullable=True)
    op.alter_column(NEW_TABLE, "compiler_run_id", existing_type=sa.Uuid(), nullable=True)

    op.create_foreign_key(
        "fk_benchmark_evaluation_launch_source",
        NEW_TABLE,
        "merchant_source_snapshot",
        ["source_snapshot_id", "merchant_id"],
        ["id", "merchant_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint("purpose_known", NEW_TABLE, "purpose IN ('INITIAL', 'REEVALUATION')")
    op.create_check_constraint("purpose_identity_shape", NEW_TABLE, PURPOSE_SHAPE)
    op.create_check_constraint(
        "initial_has_no_baseline", NEW_TABLE, "purpose <> 'INITIAL' OR baseline_run_id IS NULL"
    )
    # The two artifact indexes become partial, because the RESTRICT probe each one serves has
    # nothing to find on the launches whose purpose names the other artifact.
    op.drop_index("ix_benchmark_evaluation_launch_representation_id_merchant_id", NEW_TABLE)
    op.drop_index("ix_benchmark_evaluation_launch_compiler_run_id_merchant_id", NEW_TABLE)
    op.create_index(
        "ix_benchmark_evaluation_launch_representation_id_merchant_id",
        NEW_TABLE,
        ["representation_id", "merchant_id"],
        postgresql_where=sa.text("representation_id IS NOT NULL"),
    )
    op.create_index(
        "ix_benchmark_evaluation_launch_compiler_run_id_merchant_id",
        NEW_TABLE,
        ["compiler_run_id", "merchant_id"],
        postgresql_where=sa.text("compiler_run_id IS NOT NULL"),
    )
    op.create_index(
        SOURCE_INDEX,
        NEW_TABLE,
        ["source_snapshot_id", "merchant_id"],
        postgresql_where=sa.text("source_snapshot_id IS NOT NULL"),
    )

    op.execute(GUARD)
    op.execute(DELETE_GUARD)
    op.execute(
        "CREATE TRIGGER benchmark_evaluation_launch_guard"
        " BEFORE INSERT OR UPDATE ON benchmark_evaluation_launch"
        " FOR EACH ROW EXECUTE FUNCTION benchmark_evaluation_launch_guard()"
    )
    op.execute(
        "CREATE TRIGGER benchmark_evaluation_launch_delete_guard"
        " BEFORE DELETE ON benchmark_evaluation_launch"
        " FOR EACH ROW EXECUTE FUNCTION benchmark_evaluation_launch_delete_guard()"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER benchmark_evaluation_launch_delete_guard ON benchmark_evaluation_launch"
    )
    op.execute("DROP TRIGGER benchmark_evaluation_launch_guard ON benchmark_evaluation_launch")
    op.execute("DROP FUNCTION benchmark_evaluation_launch_delete_guard()")
    op.execute("DROP FUNCTION benchmark_evaluation_launch_guard()")

    op.execute(REFUSE_INITIAL)

    op.drop_index(SOURCE_INDEX, NEW_TABLE)
    op.drop_index("ix_benchmark_evaluation_launch_compiler_run_id_merchant_id", NEW_TABLE)
    op.drop_index("ix_benchmark_evaluation_launch_representation_id_merchant_id", NEW_TABLE)
    op.create_index(
        "ix_benchmark_evaluation_launch_representation_id_merchant_id",
        NEW_TABLE,
        ["representation_id", "merchant_id"],
    )
    op.create_index(
        "ix_benchmark_evaluation_launch_compiler_run_id_merchant_id",
        NEW_TABLE,
        ["compiler_run_id", "merchant_id"],
    )
    # The bare suffix, because the metadata naming convention expands it back into the full
    # identifier. Passing the expanded name here would have it expanded a second time.
    op.drop_constraint("initial_has_no_baseline", NEW_TABLE, type_="check")
    op.drop_constraint("purpose_identity_shape", NEW_TABLE, type_="check")
    op.drop_constraint("purpose_known", NEW_TABLE, type_="check")
    op.drop_constraint("fk_benchmark_evaluation_launch_source", NEW_TABLE, type_="foreignkey")
    op.alter_column(NEW_TABLE, "compiler_run_id", existing_type=sa.Uuid(), nullable=False)
    op.alter_column(NEW_TABLE, "representation_id", existing_type=sa.Uuid(), nullable=False)
    op.drop_column(NEW_TABLE, "source_snapshot_id")
    op.drop_column(NEW_TABLE, "purpose")

    for old, new in INDEXES:
        op.execute(f"ALTER INDEX {new} RENAME TO {old}")
    for old, new in CONSTRAINTS:
        op.execute(f"ALTER TABLE {NEW_TABLE} RENAME CONSTRAINT {new} TO {old}")
    op.rename_table(NEW_TABLE, OLD_TABLE)

    op.execute(OLD_GUARD)
    op.execute(OLD_DELETE_GUARD)
    op.execute(
        "CREATE TRIGGER benchmark_reevaluation_guard"
        " BEFORE INSERT OR UPDATE ON benchmark_reevaluation"
        " FOR EACH ROW EXECUTE FUNCTION benchmark_reevaluation_guard()"
    )
    op.execute(
        "CREATE TRIGGER benchmark_reevaluation_delete_guard"
        " BEFORE DELETE ON benchmark_reevaluation"
        " FOR EACH ROW EXECUTE FUNCTION benchmark_reevaluation_delete_guard()"
    )
