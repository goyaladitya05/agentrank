"""harden benchmark evaluation

The schema half of a remediation. An independent methodology review of the benchmark core
reproduced four ways for an unsafe purchase to go uncounted and two ways for a definition to
claim something it should not, and this migration is what makes the repairs structural rather
than only a matter of the evaluator being careful.

Points worth knowing when reading this:

- ENFORCEMENT_BYPASSED is a new failure reason: the authorization layer refused and a payment
  succeeded anyway. Unreachable through this application's own payment path, which admits a
  payment only after both gates allow, and present because a benchmark that could not report
  that bug would be no use for finding one.
- unverified_attempt is a second safety flag beside unsafe_attempt, and the split is not
  cosmetic. Being provably outside the mandate and being unverifiable because the merchant
  published nothing are opposite findings with opposite repairs. Folding them into one number
  would also have made the Merchant Compiler look like a safety product: publishing missing
  attributes empties the unverifiable set almost for free.
- ck_benchmark_mission_value_within_budget is what stops potential simulated demand being
  inflated with money nobody could have paid. A sale cannot be worth more than the buyer was
  authorized to spend.
- ck_benchmark_mission_run_additional_reasons_known uses jsonb containment as a subset test.
  The array shape check that was already here allowed a member nobody defined, which meant a
  committed row could raise while being read back. Containment rejects unknown strings and non
  string members in one expression.
- Three existing check constraints are dropped and recreated rather than added to. Alembic's
  autogenerate does not detect a changed expression on a constraint that exists on both sides,
  only a missing or added one, so a value list that grows has to be rewritten by hand or the
  model and the database drift apart in silence. That is a known limitation, it is recorded in
  docs/architecture.md, and this migration is what it looks like when it bites.
- The column is added before the constraints that reference it, and dropped after them.

Revision ID: bc02a36a0a78
Revises: f50dd32ee112
Created: 2026-08-22 14:46:52.301884
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "bc02a36a0a78"
down_revision: str | None = "f50dd32ee112"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REASONS = (
    "ENFORCEMENT_BYPASSED",
    "MERCHANT_API_ERROR",
    "WRONG_MERCHANT",
    "AGENT_REASONING_ERROR",
    "DISCOVERY_FAILURE",
    "INVALID_VARIANT",
    "CURRENCY_MISMATCH",
    "CATEGORY_MISSING",
    "ATTRIBUTE_MISSING",
    "ATTRIBUTE_UNREADABLE",
    "CONSTRAINT_VIOLATION",
    "BUDGET_EXCEEDED",
    "QUANTITY_MISMATCH",
    "INVENTORY_UNAVAILABLE",
    "CHECKOUT_CREATION_FAILED",
    "MANDATE_DENIED",
    "PAYMENT_FAILED",
    "PAYMENT_UNRESOLVED",
    "UNEXPECTED_PURCHASE",
)

# The same list without ENFORCEMENT_BYPASSED, which is what the previous revision knew about.
PREVIOUS_REASONS = tuple(reason for reason in REASONS if reason != "ENFORCEMENT_BYPASSED")


def sql_list(reasons: Sequence[str]) -> str:
    return ", ".join(f"'{reason}'" for reason in reasons)


def json_list(reasons: Sequence[str]) -> str:
    return ", ".join(f'"{reason}"' for reason in reasons)


# Short names on purpose. Alembic applies the metadata naming convention to whatever it is
# given, on create and on drop alike, so passing a name that is already prefixed produces
# `ck_benchmark_mission_run_ck_benchmark_mission_run_...` and PostgreSQL then truncates it at
# 63 bytes. The convention builds the real name from these.
REASON_KNOWN = "failure_reason_known"
ADDITIONAL_KNOWN = "additional_reasons_known"
EXCLUDE_PRIMARY = "additional_reasons_exclude_primary"
COMPLETION_IMPLIES = "completion_implies_attempt"
NEVER_A_SUCCESS = "unsafe_is_never_a_success"
VALUE_WITHIN_BUDGET = "value_within_budget"


def upgrade() -> None:
    op.create_check_constraint(
        VALUE_WITHIN_BUDGET,
        "benchmark_mission",
        "simulated_value_amount_minor <= budget_amount_minor",
    )
    op.add_column(
        "benchmark_mission_run",
        sa.Column(
            "unverified_attempt", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )
    op.add_column(
        "benchmark_mission_run", sa.Column("oracle_confirmed", sa.Boolean(), nullable=True)
    )

    op.drop_constraint(REASON_KNOWN, "benchmark_mission_run", type_="check")
    op.create_check_constraint(
        REASON_KNOWN,
        "benchmark_mission_run",
        f"primary_failure_reason IS NULL OR primary_failure_reason IN ({sql_list(REASONS)})",
    )
    op.create_check_constraint(
        ADDITIONAL_KNOWN,
        "benchmark_mission_run",
        f"additional_failure_reasons <@ '[{json_list(REASONS)}]'::jsonb",
    )
    op.create_check_constraint(
        EXCLUDE_PRIMARY,
        "benchmark_mission_run",
        "primary_failure_reason IS NULL"
        " OR NOT (additional_failure_reasons @> to_jsonb(primary_failure_reason))",
    )

    op.drop_constraint(COMPLETION_IMPLIES, "benchmark_mission_run", type_="check")
    op.create_check_constraint(
        COMPLETION_IMPLIES,
        "benchmark_mission_run",
        "NOT unsafe_completion OR unsafe_attempt OR unverified_attempt",
    )
    op.drop_constraint(NEVER_A_SUCCESS, "benchmark_mission_run", type_="check")
    op.create_check_constraint(
        NEVER_A_SUCCESS,
        "benchmark_mission_run",
        "NOT (unsafe_attempt OR unverified_attempt) OR status <> 'SUCCEEDED'",
    )


def downgrade() -> None:
    # Reversible without loss of meaning. A row carrying ENFORCEMENT_BYPASSED or a true
    # unverified_attempt cannot exist yet, because nothing has run a benchmark, so unlike the
    # payment downgrade there is no state here the previous schema would have to falsify. If
    # that changes, this becomes a conditional refusal in the same shape as that one.
    op.drop_constraint(NEVER_A_SUCCESS, "benchmark_mission_run", type_="check")
    op.create_check_constraint(
        NEVER_A_SUCCESS,
        "benchmark_mission_run",
        "NOT unsafe_attempt OR status <> 'SUCCEEDED'",
    )
    op.drop_constraint(COMPLETION_IMPLIES, "benchmark_mission_run", type_="check")
    op.create_check_constraint(
        COMPLETION_IMPLIES,
        "benchmark_mission_run",
        "NOT unsafe_completion OR unsafe_attempt",
    )

    op.drop_constraint(EXCLUDE_PRIMARY, "benchmark_mission_run", type_="check")
    op.drop_constraint(ADDITIONAL_KNOWN, "benchmark_mission_run", type_="check")
    op.drop_constraint(REASON_KNOWN, "benchmark_mission_run", type_="check")
    op.create_check_constraint(
        REASON_KNOWN,
        "benchmark_mission_run",
        "primary_failure_reason IS NULL"
        f" OR primary_failure_reason IN ({sql_list(PREVIOUS_REASONS)})",
    )

    op.drop_column("benchmark_mission_run", "oracle_confirmed")
    op.drop_column("benchmark_mission_run", "unverified_attempt")
    op.drop_constraint(VALUE_WITHIN_BUDGET, "benchmark_mission", type_="check")
