"""bind benchmark credentials to their running benchmark run

An active benchmark environment owns one merchant's ground truth.  Its loopback worker needs
to create mandates, quotes, reservations and payments, while every ordinary credential and
operator recovery path must be refused.  This nullable binding is the durable evidence that a
credential was minted for that particular run and merchant.

The composite foreign key proves the run belongs to the credential's merchant.  The credential
guard continues to make the binding immutable after issuance, so an ordinary credential cannot
be promoted into a benchmark credential later.

Revision ID: b7f2a3d9e641
Revises: c9a5d4e2b681
Created: 2026-08-23 20:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7f2a3d9e641"
down_revision: str | None = "c9a5d4e2b681"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CREDENTIAL_GUARD = """
CREATE OR REPLACE FUNCTION merchant_api_credential_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
        OR NEW.merchant_id IS DISTINCT FROM OLD.merchant_id
        OR NEW.benchmark_run_id IS DISTINCT FROM OLD.benchmark_run_id
        OR NEW.secret_hash IS DISTINCT FROM OLD.secret_hash
        OR NEW.label IS DISTINCT FROM OLD.label
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION 'merchant api credential fields are immutable';
    END IF;

    IF OLD.revoked_at IS NOT NULL
        AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at
    THEN
        RAISE EXCEPTION 'a revoked merchant api credential cannot be changed';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

PREVIOUS_CREDENTIAL_GUARD = """
CREATE OR REPLACE FUNCTION merchant_api_credential_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
        OR NEW.merchant_id IS DISTINCT FROM OLD.merchant_id
        OR NEW.secret_hash IS DISTINCT FROM OLD.secret_hash
        OR NEW.label IS DISTINCT FROM OLD.label
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION 'merchant api credential fields are immutable';
    END IF;

    IF OLD.revoked_at IS NOT NULL
        AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at
    THEN
        RAISE EXCEPTION 'a revoked merchant api credential cannot be changed';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_benchmark_run_merchant_binding", "benchmark_run", ["id", "merchant_id"]
    )
    op.add_column(
        "merchant_api_credential",
        sa.Column("benchmark_run_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_merchant_api_credential_benchmark_run"),
        "merchant_api_credential",
        "benchmark_run",
        ["benchmark_run_id", "merchant_id"],
        ["id", "merchant_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        op.f("ix_merchant_api_credential_benchmark_run_id"),
        "merchant_api_credential",
        ["benchmark_run_id"],
        unique=False,
    )
    op.execute(CREDENTIAL_GUARD)


def downgrade() -> None:
    # The older schema has no way to say that a historical credential was benchmark scoped.
    # Refuse rather than silently making those credentials ordinary in the restored database.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM merchant_api_credential WHERE benchmark_run_id IS NOT NULL
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade while benchmark-bound credentials exist';
            END IF;
        END;
        $$;
        """
    )
    op.execute(PREVIOUS_CREDENTIAL_GUARD)
    op.drop_index(
        op.f("ix_merchant_api_credential_benchmark_run_id"),
        table_name="merchant_api_credential",
    )
    op.drop_constraint(
        op.f("fk_merchant_api_credential_benchmark_run"),
        "merchant_api_credential",
        type_="foreignkey",
    )
    op.drop_column("merchant_api_credential", "benchmark_run_id")
    op.drop_constraint("uq_benchmark_run_merchant_binding", "benchmark_run", type_="unique")
