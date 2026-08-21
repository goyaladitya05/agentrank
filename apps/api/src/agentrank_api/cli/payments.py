"""The payment operator commands: what they parse, what they call and what they print.

Every command here is the same three steps. Parse what the operator typed, call one
application service, print what came back. There is no SQL, no lock, no transaction and no
rule about what a payment may do next, because all of that already exists and a second copy of
it inside a command would be a second answer to a question that must have exactly one.

The command names are the important design in this file, and they are chosen so that reading
the name tells an operator whether money can move:

```text
list-unresolved       reads                     nothing moves
show                  reads                     nothing moves
status                reads                     nothing moves
reconcile             asks the provider         money never moves here
reconcile-unresolved  asks about a batch        money never moves here either
resume                sends to the provider     money can move
abandon               decides without asking    money never moves, stock goes back
```

`reconcile` and `resume` being two commands rather than one is the whole point of that table.
An ADMITTED payment is one the provider provably never heard of, so querying it would learn
nothing and completing it means performing the payment. Everywhere else in this system
reconciliation means asking, so a command called reconcile that sometimes charged would be a
trap: an operator running it across a list of stuck payments would find out afterwards which
of them had been sent. They are separate, and the one that can move money is named for it.

`abandon` is the opposite risk and gets the opposite treatment. It ends a payment on a
judgement rather than on evidence, which means it can release stock that a real charge is
standing behind. It requires a machine readable reason, it accepts only a payment that has
already been queried at least once, and it can do nothing else: there is no command here that
sets a status, releases a reservation or marks an attempt failed on its own, because the
domain service owns that transition atomically and splitting it into steps would be offering
an operator the pieces of an inconsistent state.
"""

import argparse
import uuid
from datetime import datetime, timedelta
from typing import Any, TextIO

from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.cli.exits import ExitCode
from agentrank_api.cli.output import write_json
from agentrank_api.config import Settings
from agentrank_api.payments.operations import (
    DEFAULT_EVENT_LIMIT,
    PaymentAuditEntry,
    PaymentOperationResult,
    PaymentOperationsService,
    PaymentOperationView,
    PaymentStatusCounts,
    PaymentSweep,
    UnresolvedPayments,
    classify,
)
from agentrank_api.payments.provider import PaymentProvider, ProviderRecord
from agentrank_api.payments.recovery import (
    AbandonmentReason,
    PaymentRecoveryService,
    validate_operator_note,
)
from agentrank_api.payments.repository import DEFAULT_UNRESOLVED_LIMIT, PaymentOperationRow
from agentrank_api.payments.service import PaymentService

# A version 7 identifier is thirty six characters, and every listing column is sized so that a
# row is one line on an ordinary terminal. Written here rather than inline so the header and
# the rows cannot drift apart.
ID_WIDTH = 36
STATUS_WIDTH = 9
AGE_WIDTH = 9
AMOUNT_WIDTH = 16
HOLD_WIDTH = 11
LABEL_WIDTH = 18
RESULT_WIDTH = 23

MISSING = "-"

# What the provider line says when no provider was involved at all. A constant because three
# commands can produce it and the JSON reads it back to decide whether one was called.
NOT_ASKED = "not asked"


def add_commands(parser: argparse.ArgumentParser) -> None:
    """Declare the payment command surface.

    Seven commands, each with its own subparser, each binding itself to its implementation
    with `set_defaults`. That is what lets the runner call `arguments.command` without a
    dispatch table that has to be kept in step with the parser.
    """
    commands = parser.add_subparsers(dest="command_name", required=True)

    listing = commands.add_parser(
        "list-unresolved",
        help="payments that still need attention, oldest first",
        description=(
            "List every payment in ADMITTED, IN_FLIGHT or UNKNOWN, oldest admitted first."
            " Bounded: a limit above the maximum is clamped rather than refused, and the"
            " footer says whether more may exist behind the bound."
        ),
    )
    listing.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_UNRESOLVED_LIMIT,
        help=f"how many payments to list (default {DEFAULT_UNRESOLVED_LIMIT})",
    )
    _add_json(listing)
    listing.set_defaults(command=list_unresolved)

    detail = commands.add_parser(
        "show",
        help="one payment, with its checkout, its hold and a tail of its trail",
        description=(
            "Show one payment in any state. Every field describing it is read from the"
            " authoritative attempt row and its joins. The events beneath are history and"
            " nothing is inferred from their order."
        ),
    )
    detail.add_argument("attempt_id", type=uuid.UUID, help="the payment attempt identifier")
    detail.add_argument(
        "--events",
        type=int,
        default=DEFAULT_EVENT_LIMIT,
        help=f"how many recent events to include (default {DEFAULT_EVENT_LIMIT})",
    )
    _add_json(detail)
    detail.set_defaults(command=show)

    query = commands.add_parser(
        "reconcile",
        help="ask the provider what happened to one unresolved payment",
        description=(
            "Query the provider about one payment and record whatever it says. This never"
            " performs a payment. A payment that has never been dispatched is refused with"
            " payment_not_dispatched: that one needs resume, which can move money."
        ),
    )
    query.add_argument("attempt_id", type=uuid.UUID, help="the payment attempt identifier")
    _add_json(query)
    query.set_defaults(command=reconcile)

    sweep = commands.add_parser(
        "reconcile-unresolved",
        help="reconcile a bounded batch of unresolved payments, once",
        description=(
            "Query the provider about every unresolved payment in the work list, oldest first,"
            " up to the limit. Hand triggered and one shot: nothing schedules this and nothing"
            " repeats it. Payments that have never been dispatched are reported and skipped,"
            " because finishing one of those is a payment rather than a query."
        ),
    )
    sweep.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_UNRESOLVED_LIMIT,
        help=f"how many payments to reconcile (default {DEFAULT_UNRESOLVED_LIMIT})",
    )
    _add_json(sweep)
    sweep.set_defaults(command=reconcile_unresolved)

    dispatch = commands.add_parser(
        "resume",
        help="dispatch a payment that was admitted and never sent. This can move money",
        description=(
            "Complete a payment left ADMITTED by a process that died after admission"
            " committed. ADMITTED means the provider provably never heard of the identity, so"
            " this is safe and it is a payment: it calls the provider through the same"
            " dispatch every buyer payment uses. Only ADMITTED is accepted."
        ),
    )
    dispatch.add_argument("attempt_id", type=uuid.UUID, help="the payment attempt identifier")
    _add_json(dispatch)
    dispatch.set_defaults(command=resume)

    give_up = commands.add_parser(
        "abandon",
        help="end an unresolved payment on a decision rather than on evidence",
        description=(
            "Terminalize one UNKNOWN payment and release its hold. This is not proof that the"
            " payment failed: the provider may later reveal that the money moved, and the"
            " stock will have gone back anyway. Only UNKNOWN is accepted, so the provider has"
            " been asked at least once before anybody gives up on it."
        ),
    )
    give_up.add_argument("attempt_id", type=uuid.UUID, help="the payment attempt identifier")
    give_up.add_argument(
        "--reason",
        type=AbandonmentReason,
        choices=list(AbandonmentReason),
        required=True,
        help="machine readable reason this payment is being given up on",
    )
    give_up.add_argument(
        "--note",
        type=validate_operator_note,
        default=None,
        help="optional short reference, for example incident-123. Never put a secret here",
    )
    _add_json(give_up)
    give_up.set_defaults(command=abandon)

    summary = commands.add_parser(
        "status",
        help="how many payments are in each state",
        description=(
            "Counts per status. The terminal counts are lifetime totals rather than a recent"
            " window, because nothing here defines how long recent is."
        ),
    )
    _add_json(summary)
    summary.set_defaults(command=status)


def _add_json(parser: argparse.ArgumentParser) -> None:
    """The one flag every command shares.

    Declared once because seven identical `add_argument` calls are seven chances for one of
    them to be spelled differently.
    """
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="print one JSON document instead of a table",
    )


async def list_unresolved(
    session: AsyncSession,
    provider: PaymentProvider,
    arguments: argparse.Namespace,
    out: TextIO,
    settings: Settings,
) -> int:
    """The work list. Reads only, and never reaches the provider it was handed."""
    listing = await PaymentOperationsService(session).list_unresolved(limit=arguments.limit)
    if arguments.as_json:
        write_json(out, _listing_json(listing))
        return ExitCode.OK
    _render_listing(listing, out)
    return ExitCode.OK


async def show(
    session: AsyncSession,
    provider: PaymentProvider,
    arguments: argparse.Namespace,
    out: TextIO,
    settings: Settings,
) -> int:
    """One payment, understandable without joining anything by hand."""
    view = await PaymentOperationsService(session).show(
        arguments.attempt_id, events=arguments.events
    )
    if arguments.as_json:
        write_json(out, _view_json(view))
        return ExitCode.OK
    _render_view(view, out)
    return ExitCode.OK


async def status(
    session: AsyncSession,
    provider: PaymentProvider,
    arguments: argparse.Namespace,
    out: TextIO,
    settings: Settings,
) -> int:
    """Counts per status, so an operator can tell a quiet system from a stuck one."""
    counts = await PaymentOperationsService(session).counts()
    if arguments.as_json:
        write_json(out, _counts_json(counts))
        return ExitCode.OK
    _render_counts(counts, out)
    return ExitCode.OK


async def reconcile(
    session: AsyncSession,
    provider: PaymentProvider,
    arguments: argparse.Namespace,
    out: TextIO,
    settings: Settings,
) -> int:
    """Ask the provider about one payment, and report what that did to it.

    The state is read before and after, so the output can say what moved rather than only
    where things ended up. Both reads are ordinary reads of the authoritative row; neither is
    a lock, and neither decides anything. The reconciliation between them is the kernel's and
    is what actually holds the locks.

    A still UNKNOWN payment afterwards is a zero. The command did what it was asked and the
    provider answered; not knowing is a finding.
    """
    operations = PaymentOperationsService(session)
    # Read first, so a payment that does not exist is a 404 before any provider is involved.
    before = await operations.payment(arguments.attempt_id)
    outcome = await PaymentService(session, provider).reconcile(arguments.attempt_id)
    after = await operations.payment(arguments.attempt_id)

    _report(
        out,
        as_json=arguments.as_json,
        action="reconcile",
        before=before,
        after=after,
        result=classify(outcome),
        provider_action="queried" if outcome.provider_called else NOT_ASKED,
        provider_record=outcome.provider_record,
    )
    return ExitCode.OK


async def reconcile_unresolved(
    session: AsyncSession,
    provider: PaymentProvider,
    arguments: argparse.Namespace,
    out: TextIO,
    settings: Settings,
) -> int:
    """Run one bounded sweep and report every payment in it.

    Zero whether or not anything moved. A sweep in which every payment is still unresolved is a
    sweep that ran and found nothing to learn, and a script that treated that as a failure would
    alert on the ordinary case. A payment the kernel refused is a row in the report rather than
    an exit code, for the same reason: the other payments were still processed and the operator
    still needs to see them.
    """
    swept = await PaymentService(session, provider).reconcile_unresolved(limit=arguments.limit)
    if arguments.as_json:
        write_json(out, _sweep_json(swept))
        return ExitCode.OK
    _render_sweep(swept, out)
    return ExitCode.OK


async def resume(
    session: AsyncSession,
    provider: PaymentProvider,
    arguments: argparse.Namespace,
    out: TextIO,
    settings: Settings,
) -> int:
    """Dispatch a payment that was admitted and never sent. This one can move money.

    It goes through `PaymentService.resume`, which is the same dispatch a buyer's payment
    uses, so only ADMITTED is accepted, IN_FLIGHT is committed before the network call, the
    provider is called with no transaction open and the outcome is recorded in the same locked
    transaction. Nothing here touches a provider directly.
    """
    operations = PaymentOperationsService(session)
    before = await operations.payment(arguments.attempt_id)
    outcome = await PaymentService(session, provider).resume(arguments.attempt_id)
    after = await operations.payment(arguments.attempt_id)

    _report(
        out,
        as_json=arguments.as_json,
        action="resume",
        before=before,
        after=after,
        result=classify(outcome),
        # Sent, not queried. The verb is the difference between the two commands and printing
        # the wrong one here would undo the whole reason they are separate.
        provider_action="sent" if outcome.provider_called else NOT_ASKED,
        provider_record=outcome.provider_record,
    )
    return ExitCode.OK


async def abandon(
    session: AsyncSession,
    provider: PaymentProvider,
    arguments: argparse.Namespace,
    out: TextIO,
    settings: Settings,
) -> int:
    """Give up on one unresolved payment, atomically, through the domain service.

    The provider handed to this command is deliberately unused and cannot be reached from
    here: the recovery service holds no provider at all, which is what makes an operation that
    ends a payment without asking anybody structurally incapable of asking.

    A repeat is reported as already abandoned rather than as a second decision, and it releases
    nothing twice.
    """
    operations = PaymentOperationsService(session)
    before = await operations.payment(arguments.attempt_id)
    outcome = await PaymentRecoveryService(session).abandon_payment_attempt(
        arguments.attempt_id, reason=arguments.reason, note=arguments.note
    )
    after = await operations.payment(arguments.attempt_id)

    _report(
        out,
        as_json=arguments.as_json,
        action="abandon",
        before=before,
        after=after,
        result=(
            PaymentOperationResult.ABANDONED
            if outcome.changed
            else PaymentOperationResult.ALREADY_ABANDONED
        ),
        provider_action=NOT_ASKED,
        provider_record=None,
    )
    return ExitCode.OK


def _report(
    out: TextIO,
    *,
    as_json: bool,
    action: str,
    before: PaymentOperationRow,
    after: PaymentOperationRow,
    result: PaymentOperationResult,
    provider_action: str,
    provider_record: ProviderRecord | None,
) -> None:
    """Say what one operation did, in the same six facts whichever operation it was.

    The attempt, what it was, what it is, what the provider was asked and answered, and what
    happened to the hold and to the quote. One shape for every mutation, because an operator
    reading three commands' output should not have to learn three layouts.

    `provider_action` is a verb rather than a boolean, and the distinction is the same one the
    command names carry. A reconciliation queried; a resume sent. Reporting both as "called"
    would lose exactly the fact an operator reading a recovery afterwards most needs, which is
    whether that line is a lookup or a payment.
    """
    if as_json:
        write_json(
            out,
            {
                "attempt_id": str(after.attempt_id),
                "action": action,
                "result": result.value,
                "changed": before.status is not after.status,
                "provider_called": provider_action != NOT_ASKED,
                "provider_action": provider_action,
                "provider_record": None if provider_record is None else provider_record.value,
                "before": _transition_json(before),
                "after": _transition_json(after),
            },
        )
        return

    _field(out, "attempt", str(after.attempt_id))
    _field(out, "action", action)
    _field(out, "result", result.value)
    _field(out, "old status", before.status.value)
    _field(out, "new status", after.status.value)
    _field(out, "provider", provider_action)
    _field(out, "provider record", MISSING if provider_record is None else provider_record.value)
    _field(
        out, "reservation", f"{before.reservation_status.value} -> {after.reservation_status.value}"
    )
    _field(out, "checkout", f"{before.checkout_status.value} -> {after.checkout_status.value}")
    _field(out, "failure code", after.failure_code or MISSING)
    _field(out, "provider reference", after.provider_reference or MISSING)


def _render_listing(listing: UnresolvedPayments, out: TextIO) -> None:
    """One line per payment, and a footer that says whether the bound hid anything."""
    if not listing.payments:
        print("no unresolved payments", file=out)
        return

    print(
        f"{'attempt':<{ID_WIDTH}}  {'status':<{STATUS_WIDTH}}  {'age':>{AGE_WIDTH}}"
        f"  {'amount':>{AMOUNT_WIDTH}}  {'reservation':<{HOLD_WIDTH}}  checkout",
        file=out,
    )
    for payment in listing.payments:
        print(
            f"{payment.attempt_id!s:<{ID_WIDTH}}  {payment.status.value:<{STATUS_WIDTH}}"
            f"  {_duration(payment.age(listing.observed_at)):>{AGE_WIDTH}}"
            f"  {_money(payment):>{AMOUNT_WIDTH}}"
            f"  {payment.reservation_status.value:<{HOLD_WIDTH}}"
            f"  {payment.checkout_status.value}",
            file=out,
        )

    footer = f"{len(listing.payments)} unresolved, limit {listing.limit}"
    if listing.truncated:
        footer = f"{footer}, more may exist"
    print(footer, file=out)


def _render_sweep(swept: PaymentSweep, out: TextIO) -> None:
    """One line per payment considered, then what the whole pass amounted to."""
    if not swept.items:
        print("no unresolved payments", file=out)
        return

    print(
        f"{'attempt':<{ID_WIDTH}}  {'result':<{RESULT_WIDTH}}  transition",
        file=out,
    )
    for item in swept.items:
        after = MISSING if item.status_after is None else item.status_after.value
        transition = f"{item.status_before.value} -> {after}"
        if item.detail is not None:
            transition = f"{transition}  {item.detail}"
        print(
            f"{item.attempt_id!s:<{ID_WIDTH}}  {item.result.value:<{RESULT_WIDTH}}  {transition}",
            file=out,
        )

    footer = (
        f"{len(swept.items)} considered, {swept.resolved} resolved,"
        f" {swept.still_unresolved} still unresolved, limit {swept.limit}"
    )
    if swept.truncated:
        footer = f"{footer}, more may exist"
    print(footer, file=out)


def _render_view(view: PaymentOperationView, out: TextIO) -> None:
    """Every field of one payment, then its recent trail beneath a blank line."""
    payment = view.payment
    _field(out, "attempt", str(payment.attempt_id))
    _field(out, "status", payment.status.value)
    _field(
        out, "outcome source", payment.outcome_source.value if payment.outcome_source else MISSING
    )
    _field(out, "amount", _money(payment))
    _field(out, "merchant", str(payment.merchant_id))
    _field(out, "checkout", f"{payment.checkout_id} {payment.checkout_status.value}")
    _field(out, "mandate", str(payment.mandate_id))
    _field(out, "reservation", f"{payment.reservation_id} {payment.reservation_status.value}")
    _field(out, "idempotency key", payment.idempotency_key)
    _field(out, "provider reference", payment.provider_reference or MISSING)
    _field(out, "failure code", payment.failure_code or MISSING)
    _field(out, "created", _instant(payment.created_at))
    _field(out, "dispatched", _instant(payment.dispatched_at))
    _field(out, "resolved", _instant(payment.resolved_at))
    _field(out, "age", _duration(payment.age(view.observed_at)))

    if not view.events:
        return
    print("", file=out)
    print("recent events, informational only", file=out)
    for event in view.events:
        print(
            f"  {_instant(event.occurred_at)}  {event.event_type:<26}  {event.actor_type.value}",
            file=out,
        )


def _render_counts(counts: PaymentStatusCounts, out: TextIO) -> None:
    """Counts per status, with the totals labelled for what they are."""
    for payment_status, count in counts.counts.items():
        _field(out, payment_status.value.lower(), str(count))
    print("", file=out)
    print(f"{counts.unresolved} unresolved, terminal counts are lifetime totals", file=out)


def _field(out: TextIO, label: str, value: str) -> None:
    """One labelled line, padded so a column of them reads as a column."""
    print(f"{label:<{LABEL_WIDTH}}  {value}", file=out)


def _listing_json(listing: UnresolvedPayments) -> dict[str, Any]:
    return {
        "observed_at": listing.observed_at.isoformat(),
        "limit": listing.limit,
        "truncated": listing.truncated,
        "count": len(listing.payments),
        "payments": [_payment_json(payment, listing.observed_at) for payment in listing.payments],
    }


def _view_json(view: PaymentOperationView) -> dict[str, Any]:
    return {
        "observed_at": view.observed_at.isoformat(),
        "payment": _payment_json(view.payment, view.observed_at),
        "events": [_event_json(event) for event in view.events],
    }


def _sweep_json(swept: PaymentSweep) -> dict[str, Any]:
    return {
        "observed_at": swept.observed_at.isoformat(),
        "limit": swept.limit,
        "truncated": swept.truncated,
        "considered": len(swept.items),
        "resolved": swept.resolved,
        "still_unresolved": swept.still_unresolved,
        "items": [
            {
                "attempt_id": str(item.attempt_id),
                "result": item.result.value,
                "status_before": item.status_before.value,
                "status_after": None if item.status_after is None else item.status_after.value,
                "detail": item.detail,
            }
            for item in swept.items
        ],
    }


def _counts_json(counts: PaymentStatusCounts) -> dict[str, Any]:
    return {
        "observed_at": counts.observed_at.isoformat(),
        "unresolved": counts.unresolved,
        "counts": {status.value: count for status, count in counts.counts.items()},
    }


def _payment_json(payment: PaymentOperationRow, observed_at: datetime) -> dict[str, Any]:
    """One payment as a script sees it, with every identifier and both statuses.

    Deliberately wider than the table. A person scanning a terminal wants six columns; a script
    correlating a payment with a checkout wants every identifier, and the JSON is where that
    belongs rather than in a line nobody can read.
    """
    return {
        "attempt_id": str(payment.attempt_id),
        "status": payment.status.value,
        "merchant_id": str(payment.merchant_id),
        "checkout_id": str(payment.checkout_id),
        "checkout_status": payment.checkout_status.value,
        "mandate_id": str(payment.mandate_id),
        "reservation_id": str(payment.reservation_id),
        "reservation_status": payment.reservation_status.value,
        "idempotency_key": payment.idempotency_key,
        "amount_minor": payment.amount_minor,
        "currency": payment.currency,
        "provider_reference": payment.provider_reference,
        "failure_code": payment.failure_code,
        "outcome_source": None if payment.outcome_source is None else payment.outcome_source.value,
        "created_at": payment.created_at.isoformat(),
        "dispatched_at": _isoformat(payment.dispatched_at),
        "resolved_at": _isoformat(payment.resolved_at),
        "age_seconds": int(payment.age(observed_at).total_seconds()),
    }


def _transition_json(payment: PaymentOperationRow) -> dict[str, Any]:
    """The three statuses a mutation can move, and nothing else.

    Narrower than `_payment_json` on purpose. What a caller wants from a before and an after is
    the difference, and repeating every immutable identifier twice would bury it.
    """
    return {
        "status": payment.status.value,
        "reservation_status": payment.reservation_status.value,
        "checkout_status": payment.checkout_status.value,
    }


def _event_json(event: PaymentAuditEntry) -> dict[str, Any]:
    return {
        "occurred_at": event.occurred_at.isoformat(),
        "event_type": event.event_type,
        "actor_type": event.actor_type.value,
        "payload": dict(event.payload),
    }


def _money(payment: PaymentOperationRow) -> str:
    """Minor units and the currency, never a decimal.

    Money is an integer count of minor units everywhere in this system, and a command that
    printed 4999.00 would be the first place it stopped being one. An operator comparing this
    against a provider dashboard needs the number the provider was actually given.
    """
    return f"{payment.amount_minor} {payment.currency}"


def _instant(value: datetime | None) -> str:
    """An ISO instant to the second, or a dash when there is none.

    To the second because microseconds make a column unreadable and no operator decision turns
    on them. A dash rather than a blank so an empty column is visibly empty.
    """
    return MISSING if value is None else value.isoformat(timespec="seconds")


def _isoformat(value: datetime | None) -> str | None:
    """The JSON form of the same thing, where absent is null rather than a dash."""
    return None if value is None else value.isoformat()


def _duration(delta: timedelta) -> str:
    """How long, in the largest two units that are not zero.

    Compact because it is a column. Clamped at zero because a clock is a clock: nothing useful
    is said by a negative age, and the database instant this is measured from should make one
    impossible anyway.
    """
    seconds = max(int(delta.total_seconds()), 0)
    days, rest = divmod(seconds, 86_400)
    hours, rest = divmod(rest, 3_600)
    minutes, remainder = divmod(rest, 60)
    if days:
        return f"{days}d{hours:02d}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{remainder:02d}s"
    return f"{remainder}s"
