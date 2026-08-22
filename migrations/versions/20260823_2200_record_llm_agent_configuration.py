"""record frozen LLM agent configuration

An LLM benchmark run is one stochastic sample.  Its provider/model/prompt/tool/policy
configuration is evidence required to interpret that sample, and is immutable once written.

Revision ID: e1c8b7a6d5f4
Revises: b7f2a3d9e641
Created: 2026-08-23 22:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e1c8b7a6d5f4"
down_revision: str | None = "b7f2a3d9e641"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "benchmark_run",
        sa.Column("agent_configuration", sa.dialects.postgresql.JSONB(), nullable=True),
    )
    op.create_check_constraint(
        "agent_configuration_object",
        "benchmark_run",
        "agent_configuration IS NULL OR jsonb_typeof(agent_configuration) = 'object'",
    )
    op.execute("""
        CREATE FUNCTION benchmark_run_agent_configuration_guard() RETURNS trigger AS $$
        BEGIN
            IF NEW.agent_configuration IS DISTINCT FROM OLD.agent_configuration THEN
                RAISE EXCEPTION 'benchmark run agent configuration is immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER benchmark_run_agent_configuration_guard
        BEFORE UPDATE ON benchmark_run FOR EACH ROW
        EXECUTE FUNCTION benchmark_run_agent_configuration_guard();
    """)


def downgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM benchmark_run WHERE agent_configuration IS NOT NULL) THEN
                RAISE EXCEPTION 'cannot downgrade while frozen agent configurations exist';
            END IF;
        END;
        $$;
    """)
    op.execute("DROP TRIGGER benchmark_run_agent_configuration_guard ON benchmark_run")
    op.execute("DROP FUNCTION benchmark_run_agent_configuration_guard()")
    op.drop_constraint("agent_configuration_object", "benchmark_run", type_="check")
    op.drop_column("benchmark_run", "agent_configuration")
