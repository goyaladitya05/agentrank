"""add merchant benchmark re-evaluation launches

The merchant-facing launch command needs a durable record before any benchmark run exists, so
that a lost HTTP response has an answer, a double submit has one launch, and a worker in another
process has something to claim. That record is `benchmark_reevaluation`.

Three properties are the database's rather than the application's:

- one pending launch per merchant, through a partial unique index over the two non-settled
  statuses. A merchant owns one benchmark world, so a second queued launch is two runs resetting
  each other's shelf rather than two measurements.
- one launch per merchant per request key, which is what makes a repeated or concurrent submit
  the same launch instead of a second run.
- a settled status that agrees with the run it names. COMPLETED requires a COMPLETED run and
  FAILED with a run requires an ABORTED one, checked by a trigger on insert and on update.
  Benchmark run statuses are one way, so agreement when written is agreement forever and this
  status can never drift from the rows it describes.

The lifecycle trigger also whitelists transitions rather than blacklisting them, in the shape
the mission run guard established: QUEUED to EXECUTING, QUEUED to FAILED, EXECUTING to COMPLETED
and EXECUTING to FAILED, and nothing else. Every identity column is immutable after insert, and
a bound run cannot be swapped for another.

Revision ID: e1f4a7b9c2d5
Revises: c9d3e5f7a2b4
Created: 2026-08-25 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e1f4a7b9c2d5"
down_revision: str | None = "c9d3e5f7a2b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PENDING_INDEX = "uq_benchmark_reevaluation_pending_merchant"

GUARD = """
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

DELETE_GUARD = """
CREATE FUNCTION benchmark_reevaluation_delete_guard() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'a benchmark re-evaluation is evidence and cannot be deleted';
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    op.create_table(
        "benchmark_reevaluation",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("request_key", sa.String(length=64), nullable=False),
        sa.Column("representation_id", sa.Uuid(), nullable=False),
        sa.Column("compiler_run_id", sa.Uuid(), nullable=False),
        sa.Column("suite_id", sa.Uuid(), nullable=False),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column("buyer_profile", sa.String(length=24), nullable=False),
        sa.Column("buyer_configuration", postgresql.JSONB(), nullable=True),
        sa.Column("buyer_configuration_digest", sa.String(length=71), nullable=True),
        sa.Column("executor_kind", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("baseline_run_id", sa.Uuid(), nullable=True),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("merchant_id", "request_key", name="uq_benchmark_reevaluation_request"),
        sa.UniqueConstraint("run_id", name="uq_benchmark_reevaluation_run"),
        sa.ForeignKeyConstraint(["merchant_id"], ["merchant.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["suite_id"], ["benchmark_suite.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["representation_id", "merchant_id"],
            ["commerce_representation.id", "commerce_representation.merchant_id"],
            name="fk_benchmark_reevaluation_representation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["compiler_run_id", "merchant_id"],
            ["compiler_run.id", "compiler_run.merchant_id"],
            name="fk_benchmark_reevaluation_compiler_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["environment_id", "merchant_id"],
            ["benchmark_environment.id", "benchmark_environment.merchant_id"],
            name="fk_benchmark_reevaluation_environment",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "merchant_id"],
            ["benchmark_run.id", "benchmark_run.merchant_id"],
            name="fk_benchmark_reevaluation_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["baseline_run_id", "merchant_id"],
            ["benchmark_run.id", "benchmark_run.merchant_id"],
            name="fk_benchmark_reevaluation_baseline",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'EXECUTING', 'COMPLETED', 'FAILED')", name="status_known"
        ),
        sa.CheckConstraint(
            "buyer_profile IN ('AI_BUYER', 'REFERENCE_BUYER')", name="buyer_profile_known"
        ),
        sa.CheckConstraint("request_key ~ '^[0-9a-zA-Z_-]{8,64}$'", name="request_key_format"),
        sa.CheckConstraint(
            "executor_kind ~ '^[a-z0-9]+(-[a-z0-9]+)*$'", name="executor_kind_format"
        ),
        sa.CheckConstraint(
            "status <> 'QUEUED' OR (run_id IS NULL AND started_at IS NULL"
            " AND settled_at IS NULL AND failure_code IS NULL)",
            name="queued_shape",
        ),
        sa.CheckConstraint(
            "status NOT IN ('EXECUTING', 'COMPLETED')"
            " OR (run_id IS NOT NULL AND started_at IS NOT NULL AND failure_code IS NULL)",
            name="executing_shape",
        ),
        sa.CheckConstraint(
            "status <> 'EXECUTING' OR settled_at IS NULL", name="executing_is_not_settled"
        ),
        sa.CheckConstraint(
            "(status IN ('COMPLETED', 'FAILED')) = (settled_at IS NOT NULL)", name="settled_shape"
        ),
        sa.CheckConstraint(
            "(status = 'FAILED') = (failure_code IS NOT NULL)", name="failure_shape"
        ),
        sa.CheckConstraint("run_id IS NULL OR started_at IS NOT NULL", name="run_needs_a_start"),
        sa.CheckConstraint("started_at IS NULL OR started_at >= requested_at", name="start_order"),
        sa.CheckConstraint("settled_at IS NULL OR settled_at >= requested_at", name="settle_order"),
        sa.CheckConstraint(
            "baseline_run_id IS NULL OR run_id IS NULL OR baseline_run_id <> run_id",
            name="baseline_is_another_run",
        ),
        sa.CheckConstraint(
            "(buyer_profile = 'AI_BUYER') = (buyer_configuration IS NOT NULL)",
            name="buyer_configuration_matches_profile",
        ),
        sa.CheckConstraint(
            "(buyer_configuration IS NULL) = (buyer_configuration_digest IS NULL)",
            name="buyer_digest_shape",
        ),
        sa.CheckConstraint(
            "buyer_configuration IS NULL OR jsonb_typeof(buyer_configuration) = 'object'",
            name="buyer_configuration_object",
        ),
        sa.CheckConstraint(
            "buyer_configuration_digest IS NULL"
            " OR buyer_configuration_digest ~ '^sha256:[0-9a-f]{64}$'",
            name="buyer_digest_format",
        ),
    )
    op.create_index(
        PENDING_INDEX,
        "benchmark_reevaluation",
        ["merchant_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('QUEUED', 'EXECUTING')"),
    )
    op.create_index(
        "ix_benchmark_reevaluation_merchant_id", "benchmark_reevaluation", ["merchant_id"]
    )
    op.create_index(
        "ix_benchmark_reevaluation_representation_id_merchant_id",
        "benchmark_reevaluation",
        ["representation_id", "merchant_id"],
    )
    op.create_index(
        "ix_benchmark_reevaluation_compiler_run_id_merchant_id",
        "benchmark_reevaluation",
        ["compiler_run_id", "merchant_id"],
    )
    op.create_index("ix_benchmark_reevaluation_suite_id", "benchmark_reevaluation", ["suite_id"])
    op.create_index(
        "ix_benchmark_reevaluation_environment_id_merchant_id",
        "benchmark_reevaluation",
        ["environment_id", "merchant_id"],
    )
    op.create_index(
        "ix_benchmark_reevaluation_baseline_run_id_merchant_id",
        "benchmark_reevaluation",
        ["baseline_run_id", "merchant_id"],
        postgresql_where=sa.text("baseline_run_id IS NOT NULL"),
    )
    op.execute(GUARD)
    op.execute(DELETE_GUARD)
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


def downgrade() -> None:
    op.execute("DROP TRIGGER benchmark_reevaluation_delete_guard ON benchmark_reevaluation")
    op.execute("DROP TRIGGER benchmark_reevaluation_guard ON benchmark_reevaluation")
    op.execute("DROP FUNCTION benchmark_reevaluation_delete_guard()")
    op.execute("DROP FUNCTION benchmark_reevaluation_guard()")
    op.drop_table("benchmark_reevaluation")
