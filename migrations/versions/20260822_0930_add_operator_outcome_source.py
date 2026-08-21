"""add operator outcome source

One check constraint widens by one value, and the reason it is worth a migration of its own is
worth stating.

`payment_attempt.outcome_source` records where the outcome on the row came from. Until now it
could only have come from a provider, through a dispatch or through a query. Operator
abandonment terminalizes an unresolved payment with nothing behind it but a decision, and the
row has to be able to say so: recording OPERATOR as RECONCILIATION would make the authoritative
payment record claim a provider said something no provider said.

This is an ordinary check constraint change rather than an ALTER TYPE, which is exactly why
none of the enumerations in this schema was ever a native PostgreSQL enum.

The downgrade is conditional and refuses rather than corrupting. See `downgrade`.

Revision ID: 4c8de0a1b562
Revises: ab60fc05d747
Created: 2026-08-22 09:30:12.441907
"""

from collections.abc import Sequence

from alembic import op

revision: str = "4c8de0a1b562"
down_revision: str | None = "ab60fc05d747"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "ck_payment_attempt_outcome_source_known"
WITH_OPERATOR = (
    "outcome_source IS NULL OR outcome_source IN ('EXECUTION', 'RECONCILIATION', 'OPERATOR')"
)
WITHOUT_OPERATOR = "outcome_source IS NULL OR outcome_source IN ('EXECUTION', 'RECONCILIATION')"


def upgrade() -> None:
    op.drop_constraint(op.f(CONSTRAINT), "payment_attempt", type_="check")
    op.create_check_constraint(op.f(CONSTRAINT), "payment_attempt", WITH_OPERATOR)


def downgrade() -> None:
    """Narrow the constraint again, or refuse if a row would be falsified by narrowing.

    An abandoned payment cannot be represented by a schema with no OPERATOR value. The two ways
    to force it through would be to rewrite the source as RECONCILIATION, which would claim a
    provider answered when none did, or to delete the row, which would erase a payment somebody
    decided the fate of. Both falsify financial history, so neither is done.

    The refusal is deliberate and states what was found and why. Letting the narrowing fail on
    its own would produce a check constraint violation from PostgreSQL with no explanation of
    what it means or what to do about it.
    """
    abandoned = (
        op.get_bind()
        .exec_driver_sql("SELECT count(*) FROM payment_attempt WHERE outcome_source = 'OPERATOR'")
        .scalar_one()
    )
    if abandoned:
        raise RuntimeError(
            f"this downgrade is not lossless: {abandoned} payment attempt row(s) record"
            " outcome_source = 'OPERATOR', which the previous schema cannot represent."
            " Rewriting the source or deleting the rows would falsify the record of an"
            " operator decision about money, so neither is done automatically. Resolve those"
            " rows deliberately before downgrading."
        )

    op.drop_constraint(op.f(CONSTRAINT), "payment_attempt", type_="check")
    op.create_check_constraint(op.f(CONSTRAINT), "payment_attempt", WITHOUT_OPERATOR)
