"""baseline

Empty on purpose. Phase 0 has no domain tables, and inventing one to make the migration
look busy would be worse than an honest no-op. This revision anchors the chain so that
every later migration has a stable base, and running it creates the alembic_version table
that records where a database stands.

Revision ID: 47e8b9946f4c
Revises: none
Created: 2026-08-21 15:17:44.428507
"""

from collections.abc import Sequence

revision: str = "47e8b9946f4c"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
