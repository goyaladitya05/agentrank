"""guard the compiler publication lineage on insert as well as update

The lineage guard added in b8c2d4e6f0a1 fires BEFORE UPDATE, which is the path the publish
service takes: a run is inserted with no representation and claims one later. That leaves the
guard reachable only by callers who behave, because a row inserted already naming somebody
else's representation never meets it. The rule is about the row rather than about the statement
that produced it, so the trigger now fires on both.

Nothing about the guard's logic changes, and no data migration is needed: any row that could
exist under the old trigger already satisfies the new one.

Revision ID: c9d3e5f7a2b4
Revises: b8c2d4e6f0a1
Created: 2026-08-24 23:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c9d3e5f7a2b4"
down_revision: str | None = "b8c2d4e6f0a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DROP TRIGGER compiler_publication_lineage_guard ON compiler_run")
    op.execute("""CREATE TRIGGER compiler_publication_lineage_guard
    BEFORE INSERT OR UPDATE ON compiler_run
    FOR EACH ROW EXECUTE FUNCTION compiler_publication_lineage_guard();""")


def downgrade() -> None:
    op.execute("DROP TRIGGER compiler_publication_lineage_guard ON compiler_run")
    op.execute("""CREATE TRIGGER compiler_publication_lineage_guard
    BEFORE UPDATE ON compiler_run
    FOR EACH ROW EXECUTE FUNCTION compiler_publication_lineage_guard();""")
