"""The operator command line, run for real against PostgreSQL and a configured fake provider.

Nothing is mocked away here. `main` is called with the arguments an operator would type, it
opens its own engine against the test database, and every command reaches the real repository,
the real services, the real locks and the real database constraints. The only injected things
are the settings, so the commands hit the test database rather than the developer's, and the
provider, because a decline, a lost response and a provider that cannot prove absence are the
three cases worth testing and none of them can be asked for from the outside.

The assertions are about behavior rather than layout: which payments a command reports, which
state it left the payment in, how many times the provider was actually called, and what the
process exit code was. The exit codes are part of the contract, because a sweep script has to
be able to tell a refusal from a crash.
"""

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import StringIO

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.checkout.execution import CheckoutExecutionService
from agentrank_api.checkout.models import CheckoutSession, CheckoutStatus
from agentrank_api.checkout.quote import QuotedLine
from agentrank_api.checkout.repository import CheckoutRepository
from agentrank_api.cli import ExitCode, main
from agentrank_api.commerce.models import Variant
from agentrank_api.commerce.repository import CatalogRepository, MerchantRepository
from agentrank_api.config import Settings
from agentrank_api.constraints.repository import IntentConstraintRepository
from agentrank_api.constraints.rules import ConstraintOperator, IntentConstraintSpec
from agentrank_api.inventory.models import ReservationStatus
from agentrank_api.inventory.repository import InventoryReservationRepository
from agentrank_api.mandates.repository import MandateRepository
from agentrank_api.payments.admission import PaymentAdmissionService
from agentrank_api.payments.execution import PROVIDER_NEVER_EXECUTED, PaymentExecutionService
from agentrank_api.payments.fake import FakeOutcome, FakePaymentProvider
from agentrank_api.payments.models import OutcomeSource, PaymentAttempt, PaymentAttemptStatus
from agentrank_api.payments.provider import PaymentInstruction
from agentrank_api.payments.recovery import OPERATOR_ABANDONED, validate_operator_note
from agentrank_api.payments.references import provider_operation_reference
from agentrank_api.payments.repository import PaymentAttemptRepository

pytestmark = pytest.mark.anyio

NOW = datetime.now(UTC)
HOUR = timedelta(hours=1)
PRICE = 499900
STOCK = 5
KEY = "pay-ampere-0001"
BLACK = IntentConstraintSpec.required_attribute("color", ConstraintOperator.EQ, "black")


@dataclass(frozen=True, slots=True)
class Run:
    """What one command invocation produced: its exit code and both streams."""

    code: int
    out: str
    err: str

    def json(self) -> dict[str, object]:
        parsed: dict[str, object] = json.loads(self.out)
        return parsed


@dataclass(frozen=True, slots=True)
class Shop:
    merchant_id: uuid.UUID
    black: uuid.UUID


@pytest.fixture
async def shop(session: AsyncSession) -> Shop:
    merchant = await MerchantRepository(session).create(slug="ampere-supply", name="Ampere")
    catalog = CatalogRepository(session)
    product = await catalog.create_product(
        merchant_id=merchant.id, external_id="amp-1", title="Charger", category="chargers"
    )
    black = await catalog.create_variant(
        product=product,
        sku="AMP-BLACK",
        price_amount_minor=PRICE,
        currency="INR",
        inventory_quantity=STOCK,
        attributes={"color": "black"},
    )
    await session.commit()
    return Shop(merchant_id=merchant.id, black=black.id)


@pytest.fixture
def provider() -> FakePaymentProvider:
    """A provider that loses the response, which is the case operator tooling exists for.

    The charge goes through and the answer never comes back, so the payment is UNKNOWN and a
    query will find the success. Tests that want a different shape build their own.
    """
    return FakePaymentProvider(default=FakeOutcome.LOST_RESPONSE, clock=NOW)


async def prepared(session: AsyncSession, shop: Shop) -> CheckoutSession:
    """A quote with its own mandate, authorized and holding stock."""
    mandate = await MandateRepository(session).create(
        merchant_id=shop.merchant_id,
        max_total_amount_minor=PRICE,
        currency="INR",
        valid_from=NOW - HOUR,
        valid_until=NOW + HOUR,
    )
    await IntentConstraintRepository(session).create(
        merchant_id=shop.merchant_id, mandate_id=mandate.id, specs=[BLACK]
    )
    checkout = await CheckoutRepository(session).create(
        merchant_id=shop.merchant_id,
        mandate_id=mandate.id,
        currency="INR",
        lines=[
            QuotedLine(
                variant_id=shop.black,
                quantity=1,
                unit_price_amount_minor=PRICE,
                product_category="chargers",
                variant_attributes={"color": "black"},
            )
        ],
        expires_at=NOW + HOUR,
    )
    await session.commit()
    readiness = await CheckoutExecutionService(session).prepare_execution(
        checkout.id, merchant_id=checkout.merchant_id, at=NOW
    )
    assert readiness.ready
    return checkout


async def admitted(session: AsyncSession, shop: Shop, *, key: str = KEY) -> PaymentAttempt:
    """A payment that has provably never reached a provider, then committed and detached.

    The commit matters. The command opens its own connection, so anything this session has not
    committed is invisible to it, exactly as it would be to a separate process.
    """
    checkout = await prepared(session, shop)
    admission = await PaymentAdmissionService(session).admit_payment(
        checkout.id, merchant_id=checkout.merchant_id, idempotency_key=key, at=NOW
    )
    assert admission.attempt is not None
    await session.commit()
    return admission.attempt


async def unresolved(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider, *, key: str = KEY
) -> PaymentAttempt:
    """A payment in UNKNOWN, reached the way one really is reached."""
    attempt = await admitted(session, shop, key=key)
    await PaymentExecutionService(session, provider).dispatch(attempt.id)
    resolved = await PaymentAttemptRepository(session).get(attempt.id)
    assert resolved is not None
    assert resolved.status is PaymentAttemptStatus.UNKNOWN
    await session.commit()
    return resolved


async def run(settings: Settings, provider: FakePaymentProvider, *arguments: str) -> Run:
    """Invoke the command line exactly as a shell would, and capture both streams.

    In a thread, because `main` owns its event loop: it calls `asyncio.run`, which is what a
    process entry point should do and what cannot be done from inside the loop a test is
    already running on. Running it in a worker thread exercises the real entry point, including
    the exit code mapping, rather than reaching past it into the commands.
    """
    out, err = StringIO(), StringIO()
    code = await asyncio.to_thread(
        main, list(arguments), settings=settings, provider=provider, out=out, err=err
    )
    return Run(code=code, out=out.getvalue(), err=err.getvalue())


async def force_in_flight(session: AsyncSession, attempt_id: uuid.UUID) -> None:
    """Leave an attempt where a crash between the dispatch commit and the wire would leave it.

    Written by hand because no service produces it deliberately. It is what a process that died
    mid dispatch leaves behind, and it is the state in which the provider may have an answer
    this application has never seen.
    """
    await session.execute(
        text(
            "UPDATE payment_attempt SET status = 'IN_FLIGHT', dispatched_at = now() WHERE id = :id"
        ),
        {"id": attempt_id},
    )
    await session.commit()
    session.expire_all()


async def stock(session: AsyncSession, variant_id: uuid.UUID) -> int:
    return int(
        await session.scalar(select(Variant.inventory_quantity).where(Variant.id == variant_id))
        or 0
    )


async def reread(session: AsyncSession, attempt_id: uuid.UUID) -> PaymentAttempt:
    """Read a payment back on a session that did not write it."""
    session.expire_all()
    found = await PaymentAttemptRepository(session).get(attempt_id)
    assert found is not None
    return found


async def test_the_work_list_command_reports_what_needs_attention(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider, catalog_settings: Settings
) -> None:
    """The first question an operator has, answered by the first command.

    The payment is UNKNOWN, the row names it, and the footer says how many there are and which
    bound was applied. A settled payment is not in the list, which is checked in the query
    tests and is the reason this one can stay short.
    """
    attempt = await unresolved(session, shop, provider)

    listed = await run(catalog_settings, provider, "payments", "list-unresolved")

    assert listed.code == ExitCode.OK
    assert str(attempt.id) in listed.out
    assert "UNKNOWN" in listed.out
    assert f"{PRICE} INR" in listed.out
    assert "COMMITTED" in listed.out
    assert "1 unresolved, limit 50" in listed.out
    assert "more may exist" not in listed.out


async def test_the_work_list_command_honours_a_limit_and_says_when_it_bit(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider, catalog_settings: Settings
) -> None:
    """An operator must be able to tell a short list from a cut off one."""
    await admitted(session, shop, key="pay-ampere-0001")
    await admitted(session, shop, key="pay-ampere-0002")

    bounded = await run(catalog_settings, provider, "payments", "list-unresolved", "--limit", "1")

    assert bounded.code == ExitCode.OK
    assert "1 unresolved, limit 1, more may exist" in bounded.out
    assert len([line for line in bounded.out.splitlines() if "ADMITTED" in line]) == 1


async def test_the_empty_work_list_says_so(
    session: AsyncSession, provider: FakePaymentProvider, catalog_settings: Settings
) -> None:
    """Nothing to do is an answer, and it is a zero."""
    quiet = await run(catalog_settings, provider, "payments", "list-unresolved")

    assert quiet.code == ExitCode.OK
    assert quiet.out.strip() == "no unresolved payments"


async def test_showing_one_payment_answers_every_operator_question(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider, catalog_settings: Settings
) -> None:
    """What it is, what it belongs to, what it costs and what has happened to it.

    Everything an operator would otherwise join three tables by hand for, on one screen, plus
    the tail of the trail beneath it labelled as informational so nobody reads a payment's
    state out of an event order.
    """
    attempt = await unresolved(session, shop, provider)

    shown = await run(catalog_settings, provider, "payments", "show", str(attempt.id))

    assert shown.code == ExitCode.OK
    assert str(attempt.id) in shown.out
    assert str(attempt.checkout_id) in shown.out
    assert str(attempt.mandate_id) in shown.out
    assert str(attempt.reservation_id) in shown.out
    assert KEY in shown.out
    assert f"{PRICE} INR" in shown.out
    assert "COMMITTED" in shown.out
    assert "recent events, informational only" in shown.out
    assert "payment.admitted" in shown.out
    assert "payment.unknown" in shown.out


async def test_showing_a_payment_that_does_not_exist_is_a_distinct_exit_code(
    session: AsyncSession, provider: FakePaymentProvider, catalog_settings: Settings
) -> None:
    """Missing is not the same as refused and not the same as broken.

    A script sweeping identifiers has to tell them apart, and the message goes to the error
    stream so that piping the output into a parser does not swallow it.
    """
    missing = await run(catalog_settings, provider, "payments", "show", str(uuid.uuid7()))

    assert missing.code == ExitCode.NOT_FOUND
    assert missing.out == ""
    assert "not found" in missing.err


async def test_a_malformed_identifier_is_an_argument_error(
    session: AsyncSession, provider: FakePaymentProvider, catalog_settings: Settings
) -> None:
    """Bad arguments exit two, which is what argparse already does and what a shell expects."""
    with pytest.raises(SystemExit) as stopped:
        await run(catalog_settings, provider, "payments", "show", "not-a-uuid")

    assert stopped.value.code == ExitCode.USAGE


async def test_reconciling_a_lost_response_resolves_the_payment_and_consumes_the_stock(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider, catalog_settings: Settings
) -> None:
    """The whole point of the tool, end to end through the command an operator would type.

    The charge went through and the answer was lost, so the payment is UNKNOWN and the money
    has actually moved. One query resolves it, the checkout becomes paid, the hold is consumed
    and one unit leaves the shelf. The provider was executed once and queried once, which is
    the assertion that this is a query rather than a second payment.
    """
    attempt = await unresolved(session, shop, provider)
    before = await stock(session, shop.black)

    reconciled = await run(catalog_settings, provider, "payments", "reconcile", str(attempt.id))

    assert reconciled.code == ExitCode.OK
    assert "result              resolved_success" in reconciled.out
    assert "old status          UNKNOWN" in reconciled.out
    assert "new status          SUCCEEDED" in reconciled.out
    assert "COMMITTED -> CONSUMED" in reconciled.out
    assert "OPEN -> PAID" in reconciled.out

    settled = await reread(session, attempt.id)
    assert settled.status is PaymentAttemptStatus.SUCCEEDED
    assert settled.outcome_source is OutcomeSource.RECONCILIATION
    assert await stock(session, shop.black) == before - 1
    assert provider.executions_for(KEY) == 1
    assert provider.queries == [KEY]
    assert provider.charges == 1


async def test_reconciling_a_provider_with_no_record_changes_nothing(
    session: AsyncSession, shop: Shop, catalog_settings: Settings
) -> None:
    """A provider that cannot find the operation has not said it failed.

    The command reports the absence as an absence, the payment stays UNKNOWN, the hold stays
    committed and the exit code is zero. Not knowing is a finding, not a process failure, and a
    sweep script that treated it as one would alert on the ordinary case.
    """
    absent = FakePaymentProvider(default=FakeOutcome.AMBIGUOUS, clock=NOW)
    attempt = await unresolved(session, shop, absent)

    queried = await run(catalog_settings, absent, "payments", "reconcile", str(attempt.id))

    assert queried.code == ExitCode.OK
    assert "result              provider_absent" in queried.out
    assert "provider record     ABSENT" in queried.out
    assert "new status          UNKNOWN" in queried.out
    assert "COMMITTED -> COMMITTED" in queried.out

    still = await reread(session, attempt.id)
    assert still.status is PaymentAttemptStatus.UNKNOWN
    assert still.resolved_at is None


async def test_reconciling_a_provider_confirmed_absence_ends_the_payment(
    session: AsyncSession, shop: Shop, catalog_settings: Settings
) -> None:
    """The one answer that ends an unresolved payment with a guarantee behind it.

    The provider's visibility window passes and its answer changes from "no record right now"
    to "this never happened". That is definitive that no money moved, so the payment fails with
    `PROVIDER_NEVER_EXECUTED`, the stock goes back and the checkout stays open. The command
    reports the guarantee rather than calling it a decline, because the two are different facts.
    """
    absent = FakePaymentProvider(default=FakeOutcome.AMBIGUOUS, clock=NOW, visibility_window=HOUR)
    attempt = await unresolved(session, shop, absent)
    before = await stock(session, shop.black)

    waiting = await run(catalog_settings, absent, "payments", "reconcile", str(attempt.id))
    assert "result              provider_absent" in waiting.out

    absent.clock = NOW + 2 * HOUR
    ended = await run(catalog_settings, absent, "payments", "reconcile", str(attempt.id))

    assert ended.code == ExitCode.OK
    assert "result              provider_never_executed" in ended.out
    assert "provider record     NEVER_EXECUTED" in ended.out
    assert "COMMITTED -> RELEASED" in ended.out
    assert "OPEN -> OPEN" in ended.out

    failed = await reread(session, attempt.id)
    assert failed.status is PaymentAttemptStatus.FAILED
    assert failed.failure_code == PROVIDER_NEVER_EXECUTED
    # Released, never consumed. Nothing was sold because nothing was paid.
    assert await stock(session, shop.black) == before
    assert absent.charges == 0


async def test_reconciling_a_decline_reports_a_failure_and_releases_the_hold(
    session: AsyncSession, shop: Shop, catalog_settings: Settings
) -> None:
    """A definitive no, learned by a query rather than by the dispatch that asked for it.

    The story is a crash. The dispatch went out, the provider declined, and this application
    died before it could record anything, so the attempt is IN_FLIGHT and the answer exists
    only at the provider. Reconciliation is what closes that gap: it learns the decline, fails
    the payment, releases the hold and leaves the checkout open for another try.
    """
    declining = FakePaymentProvider(default=FakeOutcome.DECLINE, clock=NOW)
    attempt = await admitted(session, shop)
    # Built before the crash is forced, because forcing it expires every attribute on the row.
    instruction = _instruction_for(attempt, KEY)
    attempt_id = attempt.id
    await force_in_flight(session, attempt_id)
    # The dispatch this application never saw the answer to. It is the provider's ledger that
    # holds the decline afterwards, which is exactly the state a crash leaves behind.
    await declining.execute(instruction)
    before = await stock(session, shop.black)

    resolved = await run(catalog_settings, declining, "payments", "reconcile", str(attempt_id))

    assert resolved.code == ExitCode.OK
    assert "result              resolved_failure" in resolved.out
    assert "old status          IN_FLIGHT" in resolved.out
    assert "new status          FAILED" in resolved.out
    assert "COMMITTED -> RELEASED" in resolved.out
    assert "OPEN -> OPEN" in resolved.out

    failed = await reread(session, attempt_id)
    assert failed.status is PaymentAttemptStatus.FAILED
    assert failed.failure_code == "CARD_DECLINED"
    # Released, never consumed, and the provider was executed once by the dispatch that died.
    assert await stock(session, shop.black) == before
    assert declining.executions_for(KEY) == 1
    assert declining.charges == 0


async def test_reconciling_a_settled_payment_asks_the_provider_nothing(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider, catalog_settings: Settings
) -> None:
    """Idempotent, and observably so. A second run reports that there was nothing to learn."""
    attempt = await unresolved(session, shop, provider)
    await run(catalog_settings, provider, "payments", "reconcile", str(attempt.id))

    again = await run(catalog_settings, provider, "payments", "reconcile", str(attempt.id))

    assert again.code == ExitCode.OK
    assert "result              already_terminal" in again.out
    assert "provider            not asked" in again.out
    # One query in total, from the first run. The second short circuited on the terminal state.
    assert provider.queries == [KEY]
    assert provider.charges == 1


async def test_reconcile_refuses_a_payment_that_was_never_dispatched(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider, catalog_settings: Settings
) -> None:
    """The ADMITTED rule, at the command an operator would reach for first.

    Querying a provider about an identity it provably never received would learn nothing, and
    the operation that finishes this payment is a payment. The refusal names it rather than
    silently doing nothing, and the exit code separates it from a missing payment and a crash.
    """
    attempt = await admitted(session, shop)

    refused = await run(catalog_settings, provider, "payments", "reconcile", str(attempt.id))

    assert refused.code == ExitCode.REFUSED
    assert "payment_not_dispatched" in refused.err
    assert provider.executions == []
    assert provider.queries == []
    assert (await reread(session, attempt.id)).status is PaymentAttemptStatus.ADMITTED


async def test_the_sweep_command_reconciles_a_batch_and_skips_what_it_must_not_send(
    session: AsyncSession, shop: Shop, catalog_settings: Settings
) -> None:
    """The batch command, with the two shapes an operator has to be able to tell apart.

    One payment's charge went through and the answer was lost, so the sweep resolves it. One was
    admitted and never sent, so the sweep reports it and leaves it alone: finishing that one is
    a payment rather than a query, and it belongs to `resume`. The exit code is zero either way,
    because a sweep that could not act on something has still done its job.
    """
    provider = FakePaymentProvider(clock=NOW)
    provider.set_outcome("pay-ampere-0001", FakeOutcome.LOST_RESPONSE)
    lost = await unresolved(session, shop, provider, key="pay-ampere-0001")
    stuck = await admitted(session, shop, key="pay-ampere-0002")
    lost_id, stuck_id = lost.id, stuck.id
    before = await stock(session, shop.black)

    swept = await run(catalog_settings, provider, "payments", "reconcile-unresolved")

    assert swept.code == ExitCode.OK
    rows = {line.split()[0]: line.split()[1] for line in swept.out.splitlines()[1:-1]}
    assert rows[str(lost_id)] == "resolved_success"
    assert rows[str(stuck_id)] == "skipped_not_dispatched"
    assert "2 considered, 1 resolved, 1 still unresolved, limit 50" in swept.out

    assert (await reread(session, lost_id)).status is PaymentAttemptStatus.SUCCEEDED
    # Untouched, and specifically never sent.
    assert (await reread(session, stuck_id)).status is PaymentAttemptStatus.ADMITTED
    assert provider.executions_for("pay-ampere-0002") == 0
    assert await stock(session, shop.black) == before - 1


async def test_the_sweep_command_is_bounded_and_reports_json(
    session: AsyncSession, shop: Shop, catalog_settings: Settings
) -> None:
    """A limit an operator can act on, and a report a script can read."""
    provider = FakePaymentProvider(default=FakeOutcome.AMBIGUOUS, clock=NOW)
    await unresolved(session, shop, provider, key="pay-ampere-0001")
    await unresolved(session, shop, provider, key="pay-ampere-0002")

    swept = await run(
        catalog_settings, provider, "payments", "reconcile-unresolved", "--limit", "1", "--json"
    )
    report = swept.json()

    assert swept.code == ExitCode.OK
    assert report["limit"] == 1
    assert report["considered"] == 1
    assert report["truncated"] is True
    assert report["resolved"] == 0
    assert report["still_unresolved"] == 1
    items = report["items"]
    assert isinstance(items, list)
    assert items[0]["result"] == "provider_absent"
    assert items[0]["status_before"] == "UNKNOWN"
    assert items[0]["status_after"] == "UNKNOWN"
    assert items[0]["detail"] is None
    # One queried, and specifically not both. The bound is real rather than cosmetic.
    assert len(provider.queries) == 1


async def test_the_sweep_command_on_an_empty_work_list_says_so(
    session: AsyncSession, provider: FakePaymentProvider, catalog_settings: Settings
) -> None:
    """Nothing to sweep is an answer and it is a zero."""
    swept = await run(catalog_settings, provider, "payments", "reconcile-unresolved")

    assert swept.code == ExitCode.OK
    assert swept.out.strip() == "no unresolved payments"


async def test_resume_dispatches_an_admitted_payment_exactly_once(
    session: AsyncSession, shop: Shop, catalog_settings: Settings
) -> None:
    """The crash after admission recovery, as an operator command that says it moves money.

    The payment was authorized and the process that authorized it died before sending it.
    Resume completes it through the same dispatch a buyer's payment uses: one execute, one
    charge, the checkout paid and one unit gone.
    """
    succeeding = FakePaymentProvider(default=FakeOutcome.SUCCESS, clock=NOW)
    attempt = await admitted(session, shop)
    before = await stock(session, shop.black)

    resumed = await run(catalog_settings, succeeding, "payments", "resume", str(attempt.id))

    assert resumed.code == ExitCode.OK
    assert "action              resume" in resumed.out
    assert "result              resolved_success" in resumed.out
    assert "old status          ADMITTED" in resumed.out
    assert "new status          SUCCEEDED" in resumed.out
    # Sent, not queried. Resume is the command that can move money and the output says so.
    assert "provider            sent" in resumed.out

    paid = await reread(session, attempt.id)
    assert paid.status is PaymentAttemptStatus.SUCCEEDED
    assert paid.outcome_source is OutcomeSource.EXECUTION
    assert await stock(session, shop.black) == before - 1
    assert succeeding.executions_for(KEY) == 1
    assert succeeding.charges == 1

    checkout = await CheckoutRepository(session).get(
        attempt.checkout_id, merchant_id=attempt.merchant_id
    )
    assert checkout is not None
    assert checkout.status is CheckoutStatus.PAID


async def test_resume_refuses_every_state_that_may_have_reached_a_provider(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider, catalog_settings: Settings
) -> None:
    """The safety property of the one command that can charge, asserted at the command.

    An UNKNOWN payment may already have been charged, so sending it again is the duplicate this
    whole subsystem exists to prevent. It is refused by name, and the provider is not touched.
    """
    attempt = await unresolved(session, shop, provider)
    executions = len(provider.executions)

    refused = await run(catalog_settings, provider, "payments", "resume", str(attempt.id))

    assert refused.code == ExitCode.REFUSED
    assert "payment_unresolved" in refused.err
    assert len(provider.executions) == executions
    assert (await reread(session, attempt.id)).status is PaymentAttemptStatus.UNKNOWN


async def test_abandoning_an_unresolvable_payment_releases_the_hold_and_records_the_reason(
    session: AsyncSession, shop: Shop, catalog_settings: Settings
) -> None:
    """Giving up, through the command, with everything that makes it honest still true.

    The provider here can never prove absence, so reconciliation is permanently correct and
    permanently useless. The abandonment ends the payment, releases the hold without consuming
    stock, leaves the checkout open and reaches no provider at all.
    """
    absent = FakePaymentProvider(default=FakeOutcome.AMBIGUOUS, clock=NOW)
    attempt = await unresolved(session, shop, absent)
    before = await stock(session, shop.black)
    # The state the operator is deciding about: asked, and told nothing that would ever end it.
    queried = await run(catalog_settings, absent, "payments", "reconcile", str(attempt.id))
    assert "result              provider_absent" in queried.out

    given_up = await run(
        catalog_settings,
        absent,
        "payments",
        "abandon",
        str(attempt.id),
        "--reason",
        "provider_cannot_confirm",
        "--note",
        "incident-123",
    )

    assert given_up.code == ExitCode.OK
    assert "result              abandoned" in given_up.out
    assert "new status          FAILED" in given_up.out
    assert "provider            not asked" in given_up.out
    assert "COMMITTED -> RELEASED" in given_up.out
    assert "OPEN -> OPEN" in given_up.out

    ended = await reread(session, attempt.id)
    assert ended.status is PaymentAttemptStatus.FAILED
    assert ended.failure_code == OPERATOR_ABANDONED
    assert ended.outcome_source is OutcomeSource.OPERATOR
    reservation = await InventoryReservationRepository(session).get(attempt.reservation_id)
    assert reservation is not None
    assert reservation.status is ReservationStatus.RELEASED
    # Released rather than consumed. Nobody knows whether anything was paid.
    assert await stock(session, shop.black) == before
    # One query, from the reconciliation above. The abandonment asked nobody anything.
    assert absent.queries == [KEY]


async def test_abandoning_twice_is_reported_as_a_repeat_and_releases_once(
    session: AsyncSession, shop: Shop, catalog_settings: Settings
) -> None:
    """A tool that lost its answer and asked again has not made a second decision."""
    absent = FakePaymentProvider(default=FakeOutcome.AMBIGUOUS, clock=NOW)
    attempt = await unresolved(session, shop, absent)
    before = await stock(session, shop.black)

    first = await run(
        catalog_settings,
        absent,
        "payments",
        "abandon",
        str(attempt.id),
        "--reason",
        "operator_decision",
    )
    second = await run(
        catalog_settings,
        absent,
        "payments",
        "abandon",
        str(attempt.id),
        "--reason",
        "operator_decision",
    )

    assert first.code == ExitCode.OK
    assert "result              abandoned" in first.out
    assert second.code == ExitCode.OK
    assert "result              already_abandoned" in second.out
    assert "RELEASED -> RELEASED" in second.out
    assert await stock(session, shop.black) == before


async def test_abandonment_requires_a_machine_readable_reason(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider, catalog_settings: Settings
) -> None:
    """The reason is required and is an enumeration, so the trail stays answerable.

    Prose is refused before anything is opened, which is what keeps "we gave up" from being
    recorded as a sentence nobody can aggregate over.
    """
    attempt = await unresolved(session, shop, provider)

    with pytest.raises(SystemExit) as no_reason:
        await run(catalog_settings, provider, "payments", "abandon", str(attempt.id))
    assert no_reason.value.code == ExitCode.USAGE

    with pytest.raises(SystemExit) as bad_reason:
        await run(
            catalog_settings,
            provider,
            "payments",
            "abandon",
            str(attempt.id),
            "--reason",
            "we gave up",
        )
    assert bad_reason.value.code == ExitCode.USAGE
    assert (await reread(session, attempt.id)).status is PaymentAttemptStatus.UNKNOWN


async def test_abandonment_refuses_a_payment_nobody_has_queried(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider, catalog_settings: Settings
) -> None:
    """Giving up before asking is a guess rather than a recovery.

    ADMITTED has never been sent and IN_FLIGHT has never been queried, and both are refused by
    name so the operator is told which of the two other commands they want.
    """
    attempt = await admitted(session, shop)

    refused = await run(
        catalog_settings,
        provider,
        "payments",
        "abandon",
        str(attempt.id),
        "--reason",
        "provider_unreachable",
    )

    assert refused.code == ExitCode.REFUSED
    assert "payment_not_dispatched" in refused.err
    assert (await reread(session, attempt.id)).status is PaymentAttemptStatus.ADMITTED


async def test_an_operator_note_is_bounded_and_never_replaces_the_reason(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider, catalog_settings: Settings
) -> None:
    """A short reference beside the structured reason, and nothing that weakens it."""
    assert validate_operator_note("  incident-123  ") == "incident-123"
    for rejected in ("", "   ", "a" * 201, "incident\n123"):
        with pytest.raises(ValueError, match="operator note"):
            validate_operator_note(rejected)

    attempt = await unresolved(session, shop, provider)
    with pytest.raises(SystemExit) as too_long:
        await run(
            catalog_settings,
            provider,
            "payments",
            "abandon",
            str(attempt.id),
            "--reason",
            "operator_decision",
            "--note",
            "a" * 201,
        )
    assert too_long.value.code == ExitCode.USAGE


async def test_the_status_summary_counts_every_state(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider, catalog_settings: Settings
) -> None:
    """Unresolved states first, because they are the ones that mean somebody has work."""
    await unresolved(session, shop, provider, key="pay-ampere-0001")
    await admitted(session, shop, key="pay-ampere-0002")

    summary = await run(catalog_settings, provider, "payments", "status")

    assert summary.code == ExitCode.OK
    lines = [line for line in summary.out.splitlines() if line and not line[0].isdigit()]
    assert [line.split()[0] for line in lines] == [
        "admitted",
        "in_flight",
        "unknown",
        "succeeded",
        "failed",
    ]
    assert "admitted            1" in summary.out
    assert "unknown             1" in summary.out
    assert "2 unresolved" in summary.out


async def test_json_output_carries_every_identifier_a_script_needs(
    session: AsyncSession, shop: Shop, provider: FakePaymentProvider, catalog_settings: Settings
) -> None:
    """The machine readable half, which is wider than the table on purpose.

    A person scanning a terminal wants six columns; a script correlating a payment with a
    checkout wants every identifier, and this is where that belongs.
    """
    attempt = await unresolved(session, shop, provider)

    listing = await run(catalog_settings, provider, "payments", "list-unresolved", "--json")
    listed = listing.json()
    payments = listed["payments"]
    assert isinstance(payments, list)
    [payment] = payments
    assert payment["attempt_id"] == str(attempt.id)
    assert payment["checkout_id"] == str(attempt.checkout_id)
    assert payment["mandate_id"] == str(attempt.mandate_id)
    assert payment["reservation_id"] == str(attempt.reservation_id)
    assert payment["status"] == "UNKNOWN"
    assert payment["reservation_status"] == "COMMITTED"
    assert payment["checkout_status"] == "OPEN"
    assert payment["amount_minor"] == PRICE
    assert payment["currency"] == "INR"
    assert payment["resolved_at"] is None
    assert payment["age_seconds"] >= 0
    assert listed["truncated"] is False
    assert listed["count"] == 1

    detail = await run(catalog_settings, provider, "payments", "show", str(attempt.id), "--json")
    shown = detail.json()
    described = shown["payment"]
    assert isinstance(described, dict)
    # The same payment, seen through a second command. Ages differ by however long the two runs
    # were apart, so the comparison drops the one field that is a clock reading.
    assert _ageless(described) == _ageless(payment)
    events = shown["events"]
    assert isinstance(events, list)
    assert [event["event_type"] for event in events] == ["payment.admitted", "payment.unknown"]

    settled = await run(
        catalog_settings, provider, "payments", "reconcile", str(attempt.id), "--json"
    )
    reconciled = settled.json()
    assert reconciled["action"] == "reconcile"
    assert reconciled["result"] == "resolved_success"
    assert reconciled["changed"] is True
    assert reconciled["provider_called"] is True
    assert reconciled["provider_action"] == "queried"
    assert reconciled["provider_record"] == "PRESENT"
    assert reconciled["before"] == {
        "status": "UNKNOWN",
        "reservation_status": "COMMITTED",
        "checkout_status": "OPEN",
    }
    assert reconciled["after"] == {
        "status": "SUCCEEDED",
        "reservation_status": "CONSUMED",
        "checkout_status": "PAID",
    }

    summary = await run(catalog_settings, provider, "payments", "status", "--json")
    counted = summary.json()
    assert counted["counts"] == {
        "ADMITTED": 0,
        "IN_FLIGHT": 0,
        "UNKNOWN": 0,
        "SUCCEEDED": 1,
        "FAILED": 0,
    }
    assert counted["unresolved"] == 0


def test_no_operator_recovery_route_exists_on_the_http_surface() -> None:
    """The boundary this whole phase is built around, checked on the generated schema.

    Nothing authenticates a caller. An endpoint that abandoned a payment, resumed one or swept
    a batch would let anybody who can reach the process release a merchant's stock or move
    money. Every command added by this phase is absent from the surface a caller can reach.

    Read from the OpenAPI document rather than from `app.routes`. This version of FastAPI keeps
    an included router as one `_IncludedRouter` entry rather than flattening it into `APIRoute`
    objects, so scanning `app.routes` for them finds nothing at all and every negative
    assertion made that way passes for the wrong reason. The positive assertion below is what
    stops that from happening again here: an empty set cannot satisfy it.
    """
    from agentrank_api.main import create_app

    paths = set(create_app().openapi()["paths"])

    for forbidden in ("abandon", "resume", "sweep", "operator", "recovery", "unresolved"):
        assert not any(forbidden in path for path in paths), forbidden
    # The buyer facing payment surface, in full. The Razorpay preparation joined it in Phase 1I
    # and moves no money: it creates an order for a customer to pay against. Nothing operator
    # facing appears here, which is what the loop above is for.
    assert {path for path in paths if "payment" in path} == {
        "/api/v1/commerce/checkouts/{checkout_id}/payments",
        "/api/v1/commerce/payments/{attempt_id}",
        "/api/v1/commerce/payments/{attempt_id}/reconcile",
        "/api/v1/commerce/payments/{attempt_id}/razorpay-checkout",
    }


def _ageless(payment: dict[str, object]) -> dict[str, object]:
    """One payment without the field that is a clock reading rather than a fact about it."""
    return {key: value for key, value in payment.items() if key != "age_seconds"}


def _instruction_for(attempt: PaymentAttempt, key: str) -> PaymentInstruction:
    """The instruction a dispatch would have sent for this attempt."""
    return PaymentInstruction(
        attempt_id=attempt.id,
        operation_reference=provider_operation_reference(attempt.merchant_id, attempt.id),
        idempotency_key=key,
        amount_minor=attempt.amount_minor,
        currency=attempt.currency,
        merchant_reference=str(attempt.merchant_id),
        checkout_reference=str(attempt.checkout_id),
    )
