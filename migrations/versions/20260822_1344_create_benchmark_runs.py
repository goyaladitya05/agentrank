"""create benchmark runs

What was measured, for one merchant, against one published suite. Points worth knowing when
reading this:

- benchmark_run is merchant owned, unlike the suite it executes. uq_benchmark_run_ownership is
  not a rule but a target: benchmark_mission_run is bound through (run_id, merchant_id), so a
  mission result cannot be attributed to a merchant the run does not belong to.
- benchmark_mission_run carries three optional commerce references, each bound through a
  composite foreign key that includes merchant_id. That is what makes merchant isolation
  structural in both directions: a benchmark result cannot name another merchant's variant,
  quote or payment, whatever the application passes. PostgreSQL does not enforce a composite
  foreign key when any of its columns is null, which is the behavior wanted here: a mission that
  selected nothing has no reference to check.
- uq_payment_attempt_ownership is added to payment_attempt for the same reason
  uq_payment_attempt_binding was added for razorpay_checkout. It is a target, not a rule, and
  changes no row: (id, merchant_id) is already unique because id alone is. PostgreSQL requires a
  unique constraint on exactly the referenced columns, and the wider four column one cannot
  serve.
- The run to mission run foreign key is CASCADE, unlike every other reference here. A mission
  run has no meaning without its run, exactly as a checkout line has none without its quote.
  Every other reference is RESTRICT, because a benchmark result pointing at a definition or a
  commerce row that no longer exists is a hole in the record of what was measured.
- ck_benchmark_mission_run_failure_reason_matches_status is what keeps status and reason
  coherent without merging them. A success with a reason and a failure without one are both
  incoherent. ABSTAINED takes either, because a correct abstention has nothing to explain, and
  that is the whole reason it is a separate status.
- The two unsafe columns are constrained rather than trusted. An escape implies an attempt, and
  neither may sit on a mission that succeeded, because success requires full compliance and an
  unsafe success would be the benchmark contradicting its own definition of safe.
- Two triggers, one per table, holding the lifecycles. Both are transition whitelists rather
  than blacklists of terminal values, so a status added later is refused until somebody places
  it, and both refuse any change to ownership or identity columns. A benchmark result must not
  be quietly re-attributed or re-classified after the fact.
- Neither trigger refuses DELETE, matching checkout_session and payment_attempt. Deletion is
  held off by the RESTRICT references pointing at these rows and by nothing pointing away from
  them, and refusing it here would stop the run to mission run cascade from ever working.

Revision ID: f50dd32ee112
Revises: a9c07ae31e5e
Created: 2026-08-22 13:44:21.225098
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f50dd32ee112"
down_revision: str | None = "a9c07ae31e5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REASON_VALUES = (
    "'MERCHANT_API_ERROR', 'WRONG_MERCHANT', 'AGENT_REASONING_ERROR', 'DISCOVERY_FAILURE',"
    " 'INVALID_VARIANT', 'CURRENCY_MISMATCH', 'CATEGORY_MISSING', 'ATTRIBUTE_MISSING',"
    " 'ATTRIBUTE_UNREADABLE', 'CONSTRAINT_VIOLATION', 'BUDGET_EXCEEDED', 'QUANTITY_MISMATCH',"
    " 'INVENTORY_UNAVAILABLE', 'CHECKOUT_CREATION_FAILED', 'MANDATE_DENIED', 'PAYMENT_FAILED',"
    " 'PAYMENT_UNRESOLVED', 'UNEXPECTED_PURCHASE'"
)

RUN_GUARD = """
CREATE FUNCTION benchmark_run_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
        OR NEW.merchant_id IS DISTINCT FROM OLD.merchant_id
        OR NEW.suite_id IS DISTINCT FROM OLD.suite_id
        OR NEW.representation_label IS DISTINCT FROM OLD.representation_label
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

ATTACH_RUN_GUARD = """
CREATE TRIGGER benchmark_run_guard
BEFORE UPDATE ON benchmark_run
FOR EACH ROW EXECUTE FUNCTION benchmark_run_guard()
"""

MISSION_RUN_GUARD = """
CREATE FUNCTION benchmark_mission_run_guard() RETURNS trigger AS $$
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

ATTACH_MISSION_RUN_GUARD = """
CREATE TRIGGER benchmark_mission_run_guard
BEFORE UPDATE ON benchmark_mission_run
FOR EACH ROW EXECUTE FUNCTION benchmark_mission_run_guard()
"""


def upgrade() -> None:
    op.create_table(
        "benchmark_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("suite_id", sa.Uuid(), nullable=False),
        sa.Column("representation_label", sa.String(length=100), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "RUNNING",
                "COMPLETED",
                "ABORTED",
                name="benchmark_run_status",
                native_enum=False,
                create_constraint=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status = 'PENDING') = (started_at IS NULL)",
            name=op.f("ck_benchmark_run_started_at_matches"),
        ),
        sa.CheckConstraint(
            "(status IN ('COMPLETED', 'ABORTED')) = (completed_at IS NOT NULL)",
            name=op.f("ck_benchmark_run_completed_at_matches"),
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'COMPLETED', 'ABORTED')",
            name=op.f("ck_benchmark_run_status_known"),
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR completed_at >= started_at",
            name=op.f("ck_benchmark_run_completion_after_start"),
        ),
        sa.CheckConstraint(
            "representation_label IS NULL OR length(btrim(representation_label)) > 0",
            name=op.f("ck_benchmark_run_representation_label_not_blank"),
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchant.id"],
            name=op.f("fk_benchmark_run_merchant_id_merchant"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["suite_id"],
            ["benchmark_suite.id"],
            name=op.f("fk_benchmark_run_suite_id_benchmark_suite"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_benchmark_run")),
        sa.UniqueConstraint("id", "merchant_id", name="uq_benchmark_run_ownership"),
    )
    op.create_index(
        op.f("ix_benchmark_run_merchant_id"), "benchmark_run", ["merchant_id"], unique=False
    )
    op.create_index(op.f("ix_benchmark_run_suite_id"), "benchmark_run", ["suite_id"], unique=False)
    # Created before the table that references it. PostgreSQL refuses a foreign key whose target
    # columns carry no unique constraint yet.
    op.create_unique_constraint(
        "uq_payment_attempt_ownership", "payment_attempt", ["id", "merchant_id"]
    )
    op.create_table(
        "benchmark_mission_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("mission_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "RUNNING",
                "SUCCEEDED",
                "FAILED",
                "ABSTAINED",
                "ERRORED",
                name="benchmark_mission_run_status",
                native_enum=False,
                create_constraint=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column(
            "primary_failure_reason",
            sa.Enum(
                "MERCHANT_API_ERROR",
                "WRONG_MERCHANT",
                "AGENT_REASONING_ERROR",
                "DISCOVERY_FAILURE",
                "INVALID_VARIANT",
                "CURRENCY_MISMATCH",
                "CATEGORY_MISSING",
                "ATTRIBUTE_MISSING",
                "ATTRIBUTE_UNREADABLE",
                "CONSTRAINT_VIOLATION",
                "BUDGET_EXCEEDED",
                "QUANTITY_MISMATCH",
                "INVENTORY_UNAVAILABLE",
                "CHECKOUT_CREATION_FAILED",
                "MANDATE_DENIED",
                "PAYMENT_FAILED",
                "PAYMENT_UNRESOLVED",
                "UNEXPECTED_PURCHASE",
                name="benchmark_failure_reason",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=True,
        ),
        sa.Column(
            "additional_failure_reasons",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("unsafe_attempt", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "unsafe_completion", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("selected_variant_id", sa.Uuid(), nullable=True),
        sa.Column("selected_quantity", sa.Integer(), nullable=True),
        sa.Column("checkout_id", sa.Uuid(), nullable=True),
        sa.Column("payment_attempt_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status = 'PENDING') = (started_at IS NULL)",
            name=op.f("ck_benchmark_mission_run_started_at_matches"),
        ),
        sa.CheckConstraint(
            "(status IN ('ABSTAINED', 'ERRORED', 'FAILED', 'SUCCEEDED'))"
            " = (completed_at IS NOT NULL)",
            name=op.f("ck_benchmark_mission_run_completed_at_matches"),
        ),
        sa.CheckConstraint(
            "CASE status"
            " WHEN 'SUCCEEDED' THEN primary_failure_reason IS NULL"
            " WHEN 'FAILED' THEN primary_failure_reason IS NOT NULL"
            " WHEN 'ERRORED' THEN primary_failure_reason IS NULL"
            " WHEN 'PENDING' THEN primary_failure_reason IS NULL"
            " WHEN 'RUNNING' THEN primary_failure_reason IS NULL"
            " ELSE true END",
            name=op.f("ck_benchmark_mission_run_failure_reason_matches_status"),
        ),
        sa.CheckConstraint(
            "NOT unsafe_attempt OR status <> 'SUCCEEDED'",
            name=op.f("ck_benchmark_mission_run_unsafe_is_never_a_success"),
        ),
        sa.CheckConstraint(
            "NOT unsafe_completion OR status = 'FAILED'",
            name=op.f("ck_benchmark_mission_run_escape_is_a_failure"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(additional_failure_reasons) = 'array'",
            name=op.f("ck_benchmark_mission_run_additional_reasons_shape"),
        ),
        sa.CheckConstraint(
            f"primary_failure_reason IS NULL OR primary_failure_reason IN ({REASON_VALUES})",
            name=op.f("ck_benchmark_mission_run_failure_reason_known"),
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'ABSTAINED', 'ERRORED')",
            name=op.f("ck_benchmark_mission_run_status_known"),
        ),
        sa.CheckConstraint(
            "(selected_variant_id IS NULL) = (selected_quantity IS NULL)",
            name=op.f("ck_benchmark_mission_run_selection_shape"),
        ),
        sa.CheckConstraint(
            "NOT unsafe_completion OR unsafe_attempt",
            name=op.f("ck_benchmark_mission_run_completion_implies_attempt"),
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR completed_at >= started_at",
            name=op.f("ck_benchmark_mission_run_completion_after_start"),
        ),
        sa.CheckConstraint(
            "primary_failure_reason IS NOT NULL"
            " OR jsonb_array_length(additional_failure_reasons) = 0",
            name=op.f("ck_benchmark_mission_run_additional_reasons_need_a_primary"),
        ),
        sa.CheckConstraint(
            "selected_quantity IS NULL OR selected_quantity > 0",
            name=op.f("ck_benchmark_mission_run_quantity_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["checkout_id", "merchant_id"],
            ["checkout_session.id", "checkout_session.merchant_id"],
            name="fk_benchmark_mission_run_checkout",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["mission_id"],
            ["benchmark_mission.id"],
            name="fk_benchmark_mission_run_mission",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["payment_attempt_id", "merchant_id"],
            ["payment_attempt.id", "payment_attempt.merchant_id"],
            name="fk_benchmark_mission_run_payment",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "merchant_id"],
            ["benchmark_run.id", "benchmark_run.merchant_id"],
            name="fk_benchmark_mission_run_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["selected_variant_id", "merchant_id"],
            ["variant.id", "variant.merchant_id"],
            name="fk_benchmark_mission_run_variant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_benchmark_mission_run")),
        sa.UniqueConstraint("run_id", "mission_id", name="uq_benchmark_mission_run_mission"),
    )
    op.create_index(
        op.f("ix_benchmark_mission_run_merchant_id"),
        "benchmark_mission_run",
        ["merchant_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_benchmark_mission_run_mission_id"),
        "benchmark_mission_run",
        ["mission_id"],
        unique=False,
    )
    op.execute(RUN_GUARD)
    op.execute(ATTACH_RUN_GUARD)
    op.execute(MISSION_RUN_GUARD)
    op.execute(ATTACH_MISSION_RUN_GUARD)


def downgrade() -> None:
    # Dropping a table takes its trigger with it. The two functions are schema level and have to
    # be dropped by name, otherwise a downgrade leaves orphans behind and the next upgrade fails
    # on CREATE FUNCTION.
    #
    # Nothing here is conditionally irreversible. A benchmark run is a measurement, and dropping
    # the tables that hold measurements loses measurements rather than falsifying a financial
    # record, so this downgrade does not refuse the way the payment one does.
    op.drop_index(op.f("ix_benchmark_mission_run_mission_id"), table_name="benchmark_mission_run")
    op.drop_index(op.f("ix_benchmark_mission_run_merchant_id"), table_name="benchmark_mission_run")
    op.drop_table("benchmark_mission_run")
    # After the referencing table is gone, for the same reason it was created before it.
    op.drop_constraint("uq_payment_attempt_ownership", "payment_attempt", type_="unique")
    op.drop_index(op.f("ix_benchmark_run_suite_id"), table_name="benchmark_run")
    op.drop_index(op.f("ix_benchmark_run_merchant_id"), table_name="benchmark_run")
    op.drop_table("benchmark_run")
    op.execute("DROP FUNCTION benchmark_mission_run_guard()")
    op.execute("DROP FUNCTION benchmark_run_guard()")
