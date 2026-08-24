"""require a re-evaluation to settle no earlier than it started

The launch table already refused a start or a settlement before the request, and it had nothing
to say about the two of them relative to each other, so a row could legally record settling
before it started. That is now impossible rather than merely unlikely.

The application stopped mixing clocks at the same time. `requested_at` has always been the
database's `now()`, while `started_at` and `settled_at` were this process's; skew between two
hosts could therefore turn an ordinary bind into a raw integrity error after a run had already
been created. All three now come from the database's own clock, and this constraint is what
makes the ordering a property of the table rather than of whichever process wrote the row.

Revision ID: f2a5b8c1d3e6
Revises: e1f4a7b9c2d5
Created: 2026-08-25 14:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f2a5b8c1d3e6"
down_revision: str | None = "e1f4a7b9c2d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The bare name, because this repository's naming convention expands it on both sides. Passing
# the expanded name here would have it expanded a second time.
CONSTRAINT = "settle_after_start"


def upgrade() -> None:
    op.create_check_constraint(
        CONSTRAINT,
        "benchmark_reevaluation",
        "settled_at IS NULL OR started_at IS NULL OR settled_at >= started_at",
    )


def downgrade() -> None:
    op.drop_constraint(CONSTRAINT, "benchmark_reevaluation", type_="check")
