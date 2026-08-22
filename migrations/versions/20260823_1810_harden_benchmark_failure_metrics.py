"""harden benchmark failure metrics

An executor or model failure used to be classified as HARNESS and stored as ERRORED. ERRORED is
the only mission status with no failure reason, and simulated demand deliberately classifies it
as not measured rather than lost. That meant a buyer that crashed on the missions it could not
solve could improve its reported conversion and its simulated GMV without buying anything.

AGENT_EXECUTION_ERROR is the explicit, trusted-side classification for a buyer that failed to
carry a mission out: its process exited, hung, spoke an unreadable protocol, called a buyer tool
with malformed arguments, or named state it never created. It is a FAILED mission, so it remains
in every suite-fixed denominator and its available demand remains lost.

There is no new column. Failure reasons are constrained text for the primary reason and a JSONB
array for the additional reasons, so both allowlists need to name the new value.

Revision ID: c9a5d4e2b681
Revises: 4d81e0b6fa73
Created: 2026-08-23 18:10:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c9a5d4e2b681"
down_revision: str | None = "4d81e0b6fa73"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PREVIOUS_REASONS = (
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

AGENT_EXECUTION_ERROR = "AGENT_EXECUTION_ERROR"
REASONS = (*PREVIOUS_REASONS, AGENT_EXECUTION_ERROR)

REASON_KNOWN = "failure_reason_known"
ADDITIONAL_KNOWN = "additional_reasons_known"


def _sql_list(reasons: Sequence[str]) -> str:
    return ", ".join(f"'{reason}'" for reason in reasons)


def _json_list(reasons: Sequence[str]) -> str:
    return ", ".join(f'"{reason}"' for reason in reasons)


def _add_allowlists(reasons: Sequence[str]) -> None:
    op.create_check_constraint(
        REASON_KNOWN,
        "benchmark_mission_run",
        f"primary_failure_reason IS NULL OR primary_failure_reason IN ({_sql_list(reasons)})",
    )
    op.create_check_constraint(
        ADDITIONAL_KNOWN,
        "benchmark_mission_run",
        f"additional_failure_reasons <@ '[{_json_list(reasons)}]'::jsonb",
    )


def upgrade() -> None:
    op.drop_constraint(REASON_KNOWN, "benchmark_mission_run", type_="check")
    op.drop_constraint(ADDITIONAL_KNOWN, "benchmark_mission_run", type_="check")
    _add_allowlists(REASONS)


def downgrade() -> None:
    # A prior schema cannot truthfully represent an agent execution failure. Refuse instead of
    # erasing it, exactly as payment migrations refuse to discard an outcome they cannot encode.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM benchmark_mission_run
                WHERE primary_failure_reason = 'AGENT_EXECUTION_ERROR'
                   OR additional_failure_reasons @> '["AGENT_EXECUTION_ERROR"]'::jsonb
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade while AGENT_EXECUTION_ERROR benchmark results exist';
            END IF;
        END;
        $$;
        """
    )
    op.drop_constraint(ADDITIONAL_KNOWN, "benchmark_mission_run", type_="check")
    op.drop_constraint(REASON_KNOWN, "benchmark_mission_run", type_="check")
    _add_allowlists(PREVIOUS_REASONS)
