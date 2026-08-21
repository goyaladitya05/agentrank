"""Spending mandate invariants, asserted against the real schema.

These tests use the repository and the ORM to reach the database, but what is under test
is the database. A mandate is the boundary that decides whether money may move, so every
rule protecting it has a test that tries to break it.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.commerce.models import Merchant
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.mandates.models import MandateStatus
from agentrank_api.mandates.repository import MandateRepository
from agentrank_api.mandates.validation import validate_validity_window

pytestmark = pytest.mark.anyio

NOW = datetime.now(UTC)
HOUR = timedelta(hours=1)


@pytest.fixture
async def merchant(session: AsyncSession) -> Merchant:
    created = await MerchantRepository(session).create(slug="ampere-supply", name="Ampere Supply")
    await session.commit()
    return created


async def test_a_mandate_persists_and_can_be_retrieved(
    session: AsyncSession, merchant: Merchant
) -> None:
    repository = MandateRepository(session)

    created = await repository.create(
        merchant_id=merchant.id,
        max_total_amount_minor=500000,
        currency="INR",
        valid_from=NOW,
        valid_until=NOW + HOUR,
    )
    await session.commit()
    session.expunge_all()

    found = await repository.get(created.id)
    assert found is not None
    assert found.merchant_id == merchant.id
    assert found.max_total_amount_minor == 500000
    assert found.currency == "INR"
    # Null quantity is the documented "no quantity limit", not zero and not one.
    assert found.max_quantity is None
    assert found.status is MandateStatus.ACTIVE
    assert found.revoked_at is None
    assert found.created_at.tzinfo is not None
    assert found.valid_until > found.valid_from


async def test_a_negative_amount_is_rejected(session: AsyncSession, merchant: Merchant) -> None:
    with pytest.raises(IntegrityError):
        await MandateRepository(session).create(
            merchant_id=merchant.id,
            max_total_amount_minor=-1,
            currency="INR",
            valid_from=NOW,
            valid_until=NOW + HOUR,
        )


async def test_a_currency_that_is_not_an_iso_code_is_rejected(
    session: AsyncSession, merchant: Merchant
) -> None:
    with pytest.raises(IntegrityError):
        await MandateRepository(session).create(
            merchant_id=merchant.id,
            max_total_amount_minor=500000,
            currency="inr",
            valid_from=NOW,
            valid_until=NOW + HOUR,
        )


async def test_a_window_that_ends_before_it_starts_is_rejected(
    session: AsyncSession, merchant: Merchant
) -> None:
    """The rule exists in the domain and in the database, and both are asserted.

    The domain refusal is what lets the API answer with a message naming the fields. The
    constraint is what makes the rule true for writers that never touch the domain.
    """
    with pytest.raises(ValueError, match="valid_until"):
        validate_validity_window(NOW, NOW - HOUR)

    with pytest.raises(IntegrityError):
        await MandateRepository(session).create(
            merchant_id=merchant.id,
            max_total_amount_minor=500000,
            currency="INR",
            valid_from=NOW,
            valid_until=NOW - HOUR,
        )


async def test_a_quantity_limit_of_zero_is_rejected(
    session: AsyncSession, merchant: Merchant
) -> None:
    """Null means no limit. Zero would mean a mandate that authorizes nothing buyable."""
    with pytest.raises(IntegrityError):
        await MandateRepository(session).create(
            merchant_id=merchant.id,
            max_total_amount_minor=500000,
            currency="INR",
            max_quantity=0,
            valid_from=NOW,
            valid_until=NOW + HOUR,
        )


async def test_a_mandate_must_belong_to_a_merchant_that_exists(session: AsyncSession) -> None:
    import uuid

    with pytest.raises(IntegrityError):
        await MandateRepository(session).create(
            merchant_id=uuid.uuid7(),
            max_total_amount_minor=500000,
            currency="INR",
            valid_from=NOW,
            valid_until=NOW + HOUR,
        )


async def test_a_merchant_holding_a_mandate_cannot_be_deleted(
    session: AsyncSession, merchant: Merchant
) -> None:
    """RESTRICT, not CASCADE. Authorization history is not catalog data."""
    await MandateRepository(session).create(
        merchant_id=merchant.id,
        max_total_amount_minor=500000,
        currency="INR",
        valid_from=NOW,
        valid_until=NOW + HOUR,
    )
    await session.commit()

    await session.delete(merchant)
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_authorization_fields_cannot_be_edited(
    session: AsyncSession, merchant: Merchant
) -> None:
    """Raising the ceiling on an existing mandate is not an edit, it is a new mandate."""
    mandate = await MandateRepository(session).create(
        merchant_id=merchant.id,
        max_total_amount_minor=500000,
        currency="INR",
        valid_from=NOW,
        valid_until=NOW + HOUR,
    )
    await session.commit()

    mandate.max_total_amount_minor = 10_000_000
    with pytest.raises(DBAPIError, match="immutable"):
        await session.flush()


async def test_a_revoked_mandate_cannot_be_reactivated(
    session: AsyncSession, merchant: Merchant
) -> None:
    repository = MandateRepository(session)
    mandate = await repository.create(
        merchant_id=merchant.id,
        max_total_amount_minor=500000,
        currency="INR",
        valid_from=NOW,
        valid_until=NOW + HOUR,
    )
    await repository.revoke(mandate)
    await session.commit()

    mandate.status = MandateStatus.ACTIVE
    mandate.revoked_at = None
    with pytest.raises(DBAPIError, match="revoked"):
        await session.flush()


async def test_revoking_twice_changes_nothing_the_second_time(
    session: AsyncSession, merchant: Merchant
) -> None:
    repository = MandateRepository(session)
    mandate = await repository.create(
        merchant_id=merchant.id,
        max_total_amount_minor=500000,
        currency="INR",
        valid_from=NOW,
        valid_until=NOW + HOUR,
    )

    assert await repository.revoke(mandate) is True
    revoked_at = mandate.revoked_at
    assert mandate.status is MandateStatus.REVOKED
    assert revoked_at is not None

    # False means nothing changed, which is what tells a caller not to record a second
    # revocation in the audit trail.
    assert await repository.revoke(mandate) is False
    assert mandate.revoked_at == revoked_at
    await session.commit()
