"""add interactive outcome source

One check constraint widens by one value, and the reason it is worth a migration of its own is
the same reason the operator source was.

`payment_attempt.outcome_source` records where the outcome on the row came from. Until now it
could have come from a dispatch, from a query afterwards, or from an operator decision. A
Razorpay Standard Checkout is none of those: no dispatch was ever made by this application, the
customer performed the payment in a browser, and the provider was then asked what it did.

EXECUTION would claim a dispatch that never happened. RECONCILIATION would describe a payment
settled in seconds by a callback as one recovered afterwards from an ambiguous result. Both are
operationally misleading in exactly the way this column exists to prevent, which is why the
value is its own rather than borrowed.

This is an ordinary check constraint change rather than an ALTER TYPE, which is exactly why none
of the enumerations in this schema was ever a native PostgreSQL enum. The column is already
`varchar(16)` and `INTERACTIVE` is eleven characters, so no column is widened.

The downgrade is conditional and refuses rather than corrupting. See `downgrade`.

Revision ID: 5b3f27ad9e14
Revises: 7a1c4e93b8d0
Created: 2026-08-22 07:05:31.902144
"""

from collections.abc import Sequence

from alembic import op

revision: str = "5b3f27ad9e14"
down_revision: str | None = "7a1c4e93b8d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

WIDER = "outcome_source IS NULL OR outcome_source IN ('EXECUTION', 'RECONCILIATION', 'OPERATOR', 'INTERACTIVE')"  # noqa: E501
NARROWER = "outcome_source IS NULL OR outcome_source IN ('EXECUTION', 'RECONCILIATION', 'OPERATOR')"

COUNT_INTERACTIVE = "SELECT count(*) FROM payment_attempt WHERE outcome_source = 'INTERACTIVE'"


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_payment_attempt_outcome_source_known"), "payment_attempt", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_payment_attempt_outcome_source_known"), "payment_attempt", WIDER
    )


def downgrade() -> None:
    """Narrow the constraint, or refuse when the database holds a value it cannot express.

    The same decision the operator source migration made, for the same reason. A payment settled
    through an interactive provider checkout has an outcome and a provenance, and the previous
    constraint has no word for that provenance. The two ways through would be to rewrite the
    source, which would make the authoritative record claim this application dispatched a payment
    it never dispatched, or to delete the rows, which would destroy the record of money that
    moved. Neither is done automatically.

    An empty result narrows cleanly, which is the ordinary case: a deployment that has never
    taken an interactive payment reverses with no ceremony.
    """
    connection = op.get_bind()
    affected = connection.exec_driver_sql(COUNT_INTERACTIVE).scalar_one()
    if affected:
        raise RuntimeError(
            f"this downgrade is not lossless: {affected} payment attempt row(s) record"
            " outcome_source = 'INTERACTIVE', which the previous schema cannot represent."
            " Rewriting the source would claim a dispatch that never happened and deleting the"
            " rows would destroy the record of money that moved, so neither is done"
            " automatically. Resolve those rows deliberately before downgrading."
        )

    op.drop_constraint(
        op.f("ck_payment_attempt_outcome_source_known"), "payment_attempt", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_payment_attempt_outcome_source_known"), "payment_attempt", NARROWER
    )
