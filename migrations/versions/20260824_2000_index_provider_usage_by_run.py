"""index provider usage by run and merchant

Revision ID: f7a1b4c5d6e9
Revises: e6f0a3b4c5d8
Created: 2026-08-24 20:00:00.000000

Diagnostics reads every provider usage row of one run to build its provider health and
experiment sample facts. The table only had the invocation and trace event unique indexes,
neither of which has run_id leftmost, so every run diagnosis scanned all evidence for all
merchants. This index mirrors the one agent_trace_event already carries.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f7a1b4c5d6e9"
down_revision: str | None = "e6f0a3b4c5d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_agent_provider_usage_run_id_merchant_id",
        "agent_provider_usage",
        ["run_id", "merchant_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_provider_usage_run_id_merchant_id", table_name="agent_provider_usage")
