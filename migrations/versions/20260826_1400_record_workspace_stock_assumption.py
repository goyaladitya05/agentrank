"""record what a workspace assumed about stock

A source document can now say that something is in stock without saying how many there are, and
an evaluation world still has to hold an exact number of units. The number it holds for such a
line is a simulation parameter rather than merchant evidence, and until now the only trace of it
on a workspace row was inside `configuration_digest`, which proves two workspaces differ and
tells nobody what they differ about.

Two columns, both nullable, and nothing is backfilled.

`configuration` is the frozen bootstrap configuration as a document rather than as a digest, so
a merchant reading their own setup can see the mission budget and the assumed stock depth it was
built under. `stock_assumption` names the lines whose depth AgentRank supplied, so a simulated
number is visible as a simulated number beside the SKU it was supplied for.

Null means "generated before this build recorded it" and never "none". A workspace built earlier
was built from a source document in which every variant carried an exact quantity, because that
was the only thing a source document could carry, so no earlier world contains a simulated depth.
Writing a document into those rows to say so would be this migration asserting something about a
generation it did not observe.

Revision ID: d4a7c1e93b62
Revises: e5b7c93af142
Created: 2026-08-26 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d4a7c1e93b62"
down_revision: str | None = "e5b7c93af142"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "merchant_evaluation_workspace"

# The immutability trigger fires BEFORE UPDATE, so adding a column is fine and writing into one
# afterwards is not. Both columns are therefore written by the INSERT that creates a workspace,
# and rows that predate them keep saying nothing rather than being corrected.
GUARD = "merchant_evaluation_workspace_guard"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("configuration", postgresql.JSONB(), nullable=True))
    op.add_column(TABLE, sa.Column("stock_assumption", postgresql.JSONB(), nullable=True))
    op.create_check_constraint(
        "configuration_object",
        TABLE,
        "configuration IS NULL OR jsonb_typeof(configuration) = 'object'",
    )
    op.create_check_constraint(
        "stock_assumption_object",
        TABLE,
        "stock_assumption IS NULL OR jsonb_typeof(stock_assumption) = 'object'",
    )


def downgrade() -> None:
    op.drop_constraint(op.f(f"ck_{TABLE}_stock_assumption_object"), TABLE, type_="check")
    op.drop_constraint(op.f(f"ck_{TABLE}_configuration_object"), TABLE, type_="check")
    op.drop_column(TABLE, "stock_assumption")
    op.drop_column(TABLE, "configuration")
