"""add ordered benchmark agent evidence

Revision ID: f2d9c8b7a6e5
Revises: e1c8b7a6d5f4
Created: 2026-08-23 23:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f2d9c8b7a6e5"
down_revision: str | None = "e1c8b7a6d5f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_benchmark_mission_run_binding", "benchmark_mission_run", ["id", "run_id", "merchant_id"]
    )
    op.create_table(
        "agent_trace_event",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("mission_run_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(24), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "recorded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("mission_run_id", "sequence", name="uq_agent_trace_event_sequence"),
        sa.UniqueConstraint(
            "id", "mission_run_id", "run_id", "merchant_id", name="uq_agent_trace_event_binding"
        ),
        sa.ForeignKeyConstraint(
            ["mission_run_id", "run_id", "merchant_id"],
            [
                "benchmark_mission_run.id",
                "benchmark_mission_run.run_id",
                "benchmark_mission_run.merchant_id",
            ],
            name="fk_agent_trace_event_mission_run",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("sequence > 0", name="sequence_positive"),
        sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name="payload_object"),
        sa.CheckConstraint(
            "event_type IN ('MODEL_REQUEST', 'MODEL_RESPONSE', 'TOOL_CALL', 'TOOL_RESULT', "
            "'TOOL_ERROR', 'AGENT_FINAL', 'AGENT_ABORT', 'PROVIDER_ERROR')",
            name="event_type_known",
        ),
    )
    op.create_index(
        "ix_agent_trace_event_run_id_merchant_id", "agent_trace_event", ["run_id", "merchant_id"]
    )
    op.create_table(
        "agent_provider_usage",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("mission_run_id", sa.Uuid(), nullable=False),
        sa.Column("trace_event_id", sa.Uuid(), nullable=False),
        sa.Column("invocation_sequence", sa.Integer(), nullable=False),
        sa.Column("measurement_kind", sa.String(24), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("requested_model", sa.String(128), nullable=False),
        sa.Column("actual_model", sa.String(128)),
        sa.Column("provider_request_id", sa.String(256)),
        sa.Column("provider_latency_ms", sa.BigInteger()),
        sa.Column("input_tokens", sa.BigInteger()),
        sa.Column("cached_input_tokens", sa.BigInteger()),
        sa.Column("output_tokens", sa.BigInteger()),
        sa.Column("reasoning_tokens", sa.BigInteger()),
        sa.Column("total_tokens", sa.BigInteger()),
        sa.UniqueConstraint(
            "mission_run_id", "invocation_sequence", name="uq_agent_provider_usage_invocation"
        ),
        sa.UniqueConstraint("trace_event_id", name="uq_agent_provider_usage_trace_event"),
        sa.ForeignKeyConstraint(
            ["trace_event_id", "mission_run_id", "run_id", "merchant_id"],
            [
                "agent_trace_event.id",
                "agent_trace_event.mission_run_id",
                "agent_trace_event.run_id",
                "agent_trace_event.merchant_id",
            ],
            name="fk_agent_provider_usage_trace",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("invocation_sequence > 0", name="invocation_positive"),
        sa.CheckConstraint(
            "measurement_kind IN ('PROVIDER_REPORTED', 'SCRIPTED_FAKE')",
            name="measurement_kind_known",
        ),
        sa.CheckConstraint(
            "provider_latency_ms IS NULL OR provider_latency_ms >= 0", name="latency_nonnegative"
        ),
        sa.CheckConstraint(
            "input_tokens IS NULL OR input_tokens >= 0", name="input_tokens_nonnegative"
        ),
        sa.CheckConstraint(
            "cached_input_tokens IS NULL OR cached_input_tokens >= 0",
            name="cached_tokens_nonnegative",
        ),
        sa.CheckConstraint(
            "output_tokens IS NULL OR output_tokens >= 0", name="output_tokens_nonnegative"
        ),
        sa.CheckConstraint(
            "reasoning_tokens IS NULL OR reasoning_tokens >= 0", name="reasoning_tokens_nonnegative"
        ),
        sa.CheckConstraint(
            "total_tokens IS NULL OR total_tokens >= 0", name="total_tokens_nonnegative"
        ),
    )
    op.execute("""CREATE FUNCTION agent_trace_event_guard() RETURNS trigger AS $$
    BEGIN RAISE EXCEPTION 'agent trace evidence is append only'; END; $$ LANGUAGE plpgsql;
    CREATE TRIGGER agent_trace_event_guard BEFORE UPDATE OR DELETE ON agent_trace_event
    FOR EACH ROW EXECUTE FUNCTION agent_trace_event_guard();""")
    op.execute("""CREATE FUNCTION agent_provider_usage_guard() RETURNS trigger AS $$
    BEGIN RAISE EXCEPTION 'agent provider usage is append only'; END; $$ LANGUAGE plpgsql;
    CREATE TRIGGER agent_provider_usage_guard BEFORE UPDATE OR DELETE ON agent_provider_usage
    FOR EACH ROW EXECUTE FUNCTION agent_provider_usage_guard();""")


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM agent_trace_event) OR EXISTS (SELECT 1 FROM agent_provider_usage)
    THEN RAISE EXCEPTION 'cannot downgrade while benchmark agent evidence exists'; END IF;
    END; $$;""")
    op.execute("DROP TRIGGER agent_provider_usage_guard ON agent_provider_usage")
    op.execute("DROP FUNCTION agent_provider_usage_guard()")
    op.execute("DROP TRIGGER agent_trace_event_guard ON agent_trace_event")
    op.execute("DROP FUNCTION agent_trace_event_guard()")
    op.drop_table("agent_provider_usage")
    op.drop_index("ix_agent_trace_event_run_id_merchant_id", table_name="agent_trace_event")
    op.drop_table("agent_trace_event")
    op.drop_constraint("uq_benchmark_mission_run_binding", "benchmark_mission_run", type_="unique")
