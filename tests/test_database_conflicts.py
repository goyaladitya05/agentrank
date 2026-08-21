"""What a caller is told when the database, rather than a service check, refuses a write.

Every rule this application states in a service is also stated in the schema. The service
check answers first and answers better. The constraint answers second, and only when two
callers arrive close enough together that the first check was true for both of them. Before
this, that second answer was a 500 carrying a driver error, which is the wrong shape for a
well formed request against resources that exist.

The race here is forced rather than hoped for, using the same technique as the other
concurrency tests: a transaction holds the conflicting row uncommitted while the second
attempt starts, so the second is provably queued on it.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.conflicts import CONFLICTS, conflict_for
from agentrank_api.constraints.repository import IntentConstraintRepository
from agentrank_api.constraints.rules import ConstraintOperator, IntentConstraintSpec
from agentrank_api.constraints.service import (
    IntentConstraintService,
    NewIntentConstraints,
)
from agentrank_api.database import create_session_factory
from agentrank_api.errors import ConflictError
from agentrank_api.mandates.intent import RequiredAttribute
from agentrank_api.mandates.models import SpendingMandate
from agentrank_api.mandates.repository import MandateRepository

pytestmark = pytest.mark.anyio

CONCURRENCY_TIMEOUT = 30
LOCK_WAIT = 1.0
NOW = datetime.now(UTC)
BLACK = IntentConstraintSpec.required_attribute("color", ConstraintOperator.EQ, "black")


class _Diagnostic:
    """The shape psycopg exposes on an error, reduced to the field this reads."""

    def __init__(self, constraint_name: str | None) -> None:
        self.constraint_name = constraint_name


class _DriverError(Exception):
    def __init__(self, constraint_name: str | None) -> None:
        super().__init__("violation")
        self.diag = _Diagnostic(constraint_name)


def an_integrity_error(constraint_name: str | None) -> IntegrityError:
    return IntegrityError("INSERT", {}, _DriverError(constraint_name))


def test_a_known_constraint_becomes_the_refusal_it_means() -> None:
    conflict = conflict_for(
        an_integrity_error("uq_intent_constraint_set_mandate_id"), identifier="m"
    )

    assert conflict is not None
    assert conflict.reason == "constraints_already_exist"
    assert conflict.resource == "mandate"
    assert conflict.identifier == "m"


def test_every_mapped_constraint_names_the_refusal_a_caller_already_knows() -> None:
    """Most of these are unreachable through the services and mapped anyway.

    The reservation and payment invariants are prevented by locking rather than by this map.
    They are here so that if any of them is ever violated the answer is a refusal a caller can
    act on rather than a driver error.
    """
    for name, reason in (
        ("uq_inventory_reservation_active_checkout", "reservation_already_active"),
        ("ck_inventory_reservation_expiry_after_creation", "reservation_expired"),
        ("uq_payment_attempt_identity", "payment_already_requested"),
        ("uq_payment_attempt_mandate_open", "payment_in_progress"),
        ("uq_payment_attempt_mandate_succeeded", "mandate_already_consumed"),
        ("uq_payment_attempt_checkout_succeeded", "checkout_already_paid"),
    ):
        conflict = conflict_for(an_integrity_error(name))
        assert conflict is not None
        assert conflict.reason == reason


def test_an_unmapped_constraint_is_not_translated() -> None:
    """A violation nobody explained is a bug, and a bug that answers 409 is a bug nobody
    looks at."""
    assert conflict_for(an_integrity_error("uq_merchant_slug")) is None


def test_an_error_with_no_diagnostic_is_not_translated() -> None:
    assert conflict_for(IntegrityError("INSERT", {}, Exception("no diagnostic"))) is None


def test_no_translated_message_repeats_what_the_database_said() -> None:
    """A caller learns which of its own invariants it broke, not which index enforced it."""
    for name in CONFLICTS:
        conflict = conflict_for(an_integrity_error(name))
        assert conflict is not None
        assert name not in conflict.detail
        assert name not in conflict.reason


@pytest.fixture
async def mandate(session: AsyncSession) -> SpendingMandate:
    merchant = await MerchantRepository(session).create(slug="ampere-supply", name="Ampere")
    created = await MandateRepository(session).create(
        merchant_id=merchant.id,
        max_total_amount_minor=499900,
        currency="INR",
        valid_from=NOW,
        valid_until=NOW + timedelta(hours=1),
    )
    await session.commit()
    return created


@pytest.fixture
def factory(catalog_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(catalog_engine)


async def qualify_in_new_session(
    factory: async_sessionmaker[AsyncSession], merchant_id: uuid.UUID, mandate_id: uuid.UUID
) -> None:
    async with factory() as session:
        await IntentConstraintService(session).create_constraints(
            NewIntentConstraints(
                merchant_id=merchant_id,
                mandate_id=mandate_id,
                hard_constraints=(
                    RequiredAttribute(name="color", operator=ConstraintOperator.EQ, value="black"),
                ),
            )
        )


async def test_two_qualifications_racing_produce_a_refusal_not_a_server_error(
    factory: async_sessionmaker[AsyncSession], mandate: SpendingMandate
) -> None:
    """The one case a service check genuinely cannot win on its own.

    A first transaction writes the constraint set and holds it uncommitted. The service in
    the second transaction reads that no set exists, because the first has not committed, and
    goes on to insert. PostgreSQL makes it wait on the unique index, and when the first commits
    the second gets a violation.

    The answer has to be the refusal the read would have given, not a driver error. Reverting
    the translation makes this fail with an IntegrityError.
    """
    async with asyncio.timeout(CONCURRENCY_TIMEOUT):
        async with factory() as first:
            await IntentConstraintRepository(first).create(
                merchant_id=mandate.merchant_id, mandate_id=mandate.id, specs=[BLACK]
            )
            attempt = asyncio.create_task(
                qualify_in_new_session(factory, mandate.merchant_id, mandate.id)
            )
            done, _ = await asyncio.wait({attempt}, timeout=LOCK_WAIT)
            # Queued on the unique index rather than reading around it.
            assert not done
            await first.commit()

        with pytest.raises(ConflictError) as refused:
            await attempt

    assert refused.value.reason == "constraints_already_exist"
    assert refused.value.identifier == str(mandate.id)
