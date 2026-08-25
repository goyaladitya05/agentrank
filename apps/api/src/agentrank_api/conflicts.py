"""Turning a database enforced invariant into the refusal it already had a name for.

Every rule this application states in a service is also stated in the schema, which is the
point: the database is the layer that cannot be bypassed. The service check answers first and
answers well, naming the field and the resource. The constraint answers second, and only when
two callers arrive close enough together that the first check was true for both of them.

Without this module the second answer is a 500 carrying a psycopg error. The request was well
formed, the resources exist, and the state refused it, which is what a 409 means everywhere
else in this codebase. Two concurrent requests should not get two different shapes of answer
to the same question.

Narrow on purpose, in two ways.

Only named constraints appear here, and the map is keyed by names this repository chose
through its own metadata convention. A violation that is not in the map returns None and the
original error propagates, because an unrecognized invariant violation is a bug and a bug
should look like one. Turning every `IntegrityError` into the same 409 would hide real
mistakes behind a plausible refusal.

And nothing PostgreSQL wrote reaches a caller. The constraint name selects a message this
repository authored; it is never included in one. A caller learns which of its own invariants
it violated, not which index enforced it.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.errors import ConflictError


@dataclass(frozen=True, slots=True)
class InvariantConflict:
    """What one named constraint means, in the vocabulary a caller already knows."""

    reason: str
    detail: str
    resource: str


# Each entry is an invariant a service also checks, listed here for the case where two
# callers pass that check at once. A constraint whose violation cannot be explained without
# guessing does not belong here: None and a 500 is the honest answer for one of those.
CONFLICTS: dict[str, InvariantConflict] = {
    # One benchmark suite definition per key and version. The publish service reads the
    # existing version first and refuses a changed definition by name, and two publishes of a
    # brand new version can both read that none exists.
    "uq_benchmark_suite_version": InvariantConflict(
        reason="suite_already_published",
        detail="this benchmark suite key and version are already published",
        resource="benchmark_suite",
    ),
    # One registered benchmark world per fixture key and version. The environment service
    # reads the existing registration first and refuses a changed fixture by name, and two
    # registrations of a brand new version can both read that none exists.
    "uq_benchmark_environment_version": InvariantConflict(
        reason="environment_already_registered",
        detail="this benchmark fixture key and version are already registered",
        resource="benchmark_environment",
    ),
    # At most one benchmark run executes against one merchant. The run service reads the
    # active run first and refuses by name, and two starts racing can both read that there is
    # none. This is the backstop, and it is the layer that holds across processes.
    "uq_benchmark_run_active_merchant": InvariantConflict(
        reason="run_already_active",
        detail="another benchmark run is already executing against this merchant",
        resource="benchmark_run",
    ),
    # One pending evaluation launch per merchant. The launch service takes the merchant's
    # benchmark world lock and reads the pending launch first, so this is reached only by a
    # caller that somehow did not, and it is the layer that holds across processes.
    "uq_benchmark_evaluation_launch_pending_merchant": InvariantConflict(
        reason="evaluation_already_pending",
        detail="an evaluation is already queued or running for this merchant",
        resource="benchmark_evaluation_launch",
    ),
    # One evaluation workspace per merchant, source snapshot and bootstrap configuration. The
    # bootstrap service takes the merchant's benchmark world lock and reads the existing
    # workspace first, so this is reached only by a writer that somehow did not, and it is the
    # layer that holds across processes.
    "uq_merchant_evaluation_workspace_identity": InvariantConflict(
        reason="workspace_already_built",
        detail="this merchant already has an evaluation setup for this source and configuration",
        resource="merchant_evaluation_workspace",
    ),
    # A mandate is qualified once. The service refuses a second constraint set after reading
    # that one exists, and two creations racing can both read that none does.
    "uq_intent_constraint_set_mandate_id": InvariantConflict(
        reason="constraints_already_exist",
        detail="this mandate is already qualified by a constraint set",
        resource="mandate",
    ),
    # One holding reservation per checkout, where holding is ACTIVE or COMMITTED. Unreachable
    # through execution preparation now that it holds the checkout lock, so this is the
    # backstop rather than the mechanism.
    "uq_inventory_reservation_active_checkout": InvariantConflict(
        reason="reservation_already_active",
        detail="this checkout already holds an active reservation",
        resource="checkout",
    ),
    # A reservation cannot be written already expired. Also unreachable now that preparation
    # re-reads the clock after its last lock and refuses before writing.
    "ck_inventory_reservation_expiry_after_creation": InvariantConflict(
        reason="reservation_expired",
        detail="this checkout can no longer hold stock for long enough to be worth holding",
        resource="checkout",
    ),
    # One payment attempt per logical payment operation. Unreachable through payment
    # admission now that it holds the checkout lock and looks the identity up first, so this
    # is the backstop rather than the mechanism.
    "uq_payment_attempt_identity": InvariantConflict(
        reason="payment_already_requested",
        detail="this checkout already has a payment attempt under that idempotency key",
        resource="checkout",
    ),
    # One non terminal payment attempt per mandate. This is what stops two candidate
    # checkouts under one mandate from both reaching a provider, and the admission service
    # checks it under the mandate lock first so that an ordinary refusal names the reason.
    "uq_payment_attempt_mandate_open": InvariantConflict(
        reason="payment_in_progress",
        detail="a payment under this mandate is already in progress",
        resource="mandate",
    ),
    # The single purchase mandate rule. At most one payment attempt under a mandate may ever
    # be SUCCEEDED, and this index is what makes that true rather than merely intended.
    "uq_payment_attempt_mandate_succeeded": InvariantConflict(
        reason="mandate_already_consumed",
        detail="a successful payment has already consumed this mandate",
        resource="mandate",
    ),
    # One successful payment per checkout, by the same mechanism.
    "uq_payment_attempt_checkout_succeeded": InvariantConflict(
        reason="checkout_already_paid",
        detail="this checkout has already been paid",
        resource="checkout",
    ),
}


def conflict_for(error: IntegrityError, *, identifier: str | None = None) -> ConflictError | None:
    """The typed refusal this integrity error means, or None if it does not mean one.

    None is a real answer and the caller has to act on it by re-raising. An integrity error
    this module cannot name is a bug rather than a refusal, and a bug that answers 409 is a
    bug nobody will look at.
    """
    name = _constraint_name(error)
    known = CONFLICTS.get(name) if name is not None else None
    if known is None:
        return None
    return ConflictError(known.reason, known.detail, resource=known.resource, identifier=identifier)


@asynccontextmanager
async def translated_conflicts(
    session: AsyncSession, *, identifier: str | None = None
) -> AsyncIterator[None]:
    """Run a write, and answer a recognized invariant violation as the refusal it is.

    Wrapped around the statement that can raise rather than around a whole operation, so what
    is being translated stays visible at the call site. The transaction is rolled back first:
    after an integrity error PostgreSQL will accept nothing else on it, so rolling back is not
    a choice about what to discard, it is the only thing left to do.

    `identifier` names the resource the refusal is about, which the caller knows and the
    driver diagnostic does not.
    """
    try:
        yield
    except IntegrityError as error:
        await session.rollback()
        conflict = conflict_for(error, identifier=identifier)
        if conflict is None:
            raise
        raise conflict from error


def _constraint_name(error: IntegrityError) -> str | None:
    """The constraint PostgreSQL named, read from the driver diagnostic.

    Read defensively. `orig` is whatever the driver raised, and an error that arrived without
    a diagnostic is simply one this module cannot explain.
    """
    diagnostic = getattr(error.orig, "diag", None)
    name = getattr(diagnostic, "constraint_name", None)
    return name if isinstance(name, str) else None
