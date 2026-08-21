"""Cross merchant behavior of the mandate and constraint endpoints.

One claim, asserted from every angle it can be broken from: a merchant API key is worth exactly
one merchant's mandates, and knowing an identifier is worth nothing.

Two merchants exist in every test here, each holding a real credential and a real mandate. What
is checked is what merchant A gets when it asks about merchant B's resources, and specifically
that it gets the same answer it would get for an identifier nobody has ever used. A 403 saying
"that belongs to someone else" would confirm the resource exists, and walking identifiers to
find out which ones are real is exactly the attack a 404 removes.

Denial is also asserted to be silent: no state moves and no audit event is written for a request
that was refused. A refusal that left a trace would be a way to write into another merchant's
history.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from conftest import CredentialIssuer, bearer
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.audit.repository import AuditRepository
from agentrank_api.auth.service import MerchantCredentialService
from agentrank_api.auth.tokens import parse_token
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.config import Settings
from agentrank_api.main import create_app
from agentrank_api.mandates.models import MandateStatus
from agentrank_api.mandates.repository import MandateRepository

pytestmark = pytest.mark.anyio

COMMERCE_URL = "/api/v1/commerce"
MANDATES_URL = f"{COMMERCE_URL}/mandates"
NOW = datetime.now(UTC)
HOUR = timedelta(hours=1)

CONSTRAINTS = {"constraints": [{"kind": "required_attribute", "name": "color", "value": "black"}]}


@dataclass(frozen=True, slots=True)
class Merchant:
    """One merchant, its key and one mandate it holds."""

    merchant_id: uuid.UUID
    mandate_id: uuid.UUID
    token: str


async def build(session: AsyncSession, issue_credential: CredentialIssuer, slug: str) -> Merchant:
    merchant = await MerchantRepository(session).create(slug=slug, name=slug.title())
    mandate = await MandateRepository(session).create(
        merchant_id=merchant.id,
        max_total_amount_minor=500000,
        currency="INR",
        valid_from=NOW,
        valid_until=NOW + HOUR,
    )
    await session.commit()
    token = await issue_credential(merchant.id, slug)
    return Merchant(merchant_id=merchant.id, mandate_id=mandate.id, token=token)


@pytest.fixture
async def alice(session: AsyncSession, issue_credential: CredentialIssuer) -> Merchant:
    return await build(session, issue_credential, "ampere-supply")


@pytest.fixture
async def bob(session: AsyncSession, issue_credential: CredentialIssuer) -> Merchant:
    return await build(session, issue_credential, "volt-works")


def as_alice(catalog_settings: Settings, alice: Merchant) -> TestClient:
    return TestClient(create_app(catalog_settings), headers=bearer(alice.token))


def indistinguishable(foreign: Response, unknown: Response) -> None:
    """A foreign resource and a nonexistent one must answer identically.

    Compared as whole responses rather than as status codes. A body that named the resource
    differently, or an error code that differed, would let a caller tell the two apart just as
    well as a 403 would, and it would do it while looking like it had been handled.

    The identifiers themselves differ and are not compared, because a 404 naming the identifier
    the caller sent is the caller's own input echoed back.
    """
    assert foreign.status_code == 404
    assert unknown.status_code == 404
    assert foreign.json()["error"] == unknown.json()["error"] == "not_found"
    assert foreign.json()["resource"] == unknown.json()["resource"]
    assert foreign.json().keys() == unknown.json().keys()


async def events(session: AsyncSession, merchant_id: uuid.UUID) -> list[str]:
    return [
        event.event_type for event in await AuditRepository(session).list_for_merchant(merchant_id)
    ]


async def test_a_merchant_reads_its_own_mandate(
    catalog_settings: Settings, alice: Merchant
) -> None:
    with as_alice(catalog_settings, alice) as client:
        response = client.get(f"{MANDATES_URL}/{alice.mandate_id}")

    assert response.status_code == 200
    assert response.json()["merchant_id"] == str(alice.merchant_id)


async def test_another_merchants_mandate_does_not_exist(
    catalog_settings: Settings, alice: Merchant, bob: Merchant
) -> None:
    with as_alice(catalog_settings, alice) as client:
        foreign = client.get(f"{MANDATES_URL}/{bob.mandate_id}")
        unknown = client.get(f"{MANDATES_URL}/{uuid.uuid7()}")

    indistinguishable(foreign, unknown)


async def test_another_merchants_mandate_cannot_be_validated(
    catalog_settings: Settings, alice: Merchant, bob: Merchant
) -> None:
    """A validation answer would leak the window and the status without returning the row."""
    with as_alice(catalog_settings, alice) as client:
        foreign = client.get(f"{MANDATES_URL}/{bob.mandate_id}/validation")
        unknown = client.get(f"{MANDATES_URL}/{uuid.uuid7()}/validation")

    indistinguishable(foreign, unknown)


async def test_another_merchants_mandate_cannot_be_revoked(
    catalog_settings: Settings, session: AsyncSession, alice: Merchant, bob: Merchant
) -> None:
    """The most damaging cross merchant write available: withdrawing somebody's authorization.

    Both halves matter. The request is refused, and nothing moved: the mandate is still active,
    it carries no revocation timestamp, and no event was written into merchant B's history.
    """
    before = await events(session, bob.merchant_id)

    with as_alice(catalog_settings, alice) as client:
        foreign = client.post(f"{MANDATES_URL}/{bob.mandate_id}/revoke")
        unknown = client.post(f"{MANDATES_URL}/{uuid.uuid7()}/revoke")

    indistinguishable(foreign, unknown)

    mandate = await MandateRepository(session).get(bob.mandate_id, merchant_id=bob.merchant_id)
    assert mandate is not None
    assert mandate.status is MandateStatus.ACTIVE
    assert mandate.revoked_at is None
    assert await events(session, bob.merchant_id) == before


async def test_another_merchants_mandate_cannot_be_qualified(
    catalog_settings: Settings, session: AsyncSession, alice: Merchant, bob: Merchant
) -> None:
    """Writing the terms of somebody else's authorization, refused and unrecorded."""
    before = await events(session, bob.merchant_id)

    with as_alice(catalog_settings, alice) as client:
        foreign = client.post(f"{MANDATES_URL}/{bob.mandate_id}/constraints", json=CONSTRAINTS)
        unknown = client.post(f"{MANDATES_URL}/{uuid.uuid7()}/constraints", json=CONSTRAINTS)

    indistinguishable(foreign, unknown)
    assert await events(session, bob.merchant_id) == before

    # And merchant B can still qualify it, which is what proves nothing was half written.
    with TestClient(create_app(catalog_settings), headers=bearer(bob.token)) as client:
        assert (
            client.post(
                f"{MANDATES_URL}/{bob.mandate_id}/constraints", json=CONSTRAINTS
            ).status_code
            == 201
        )


async def test_another_merchants_constraints_cannot_be_read(
    catalog_settings: Settings, alice: Merchant, bob: Merchant
) -> None:
    with TestClient(create_app(catalog_settings), headers=bearer(bob.token)) as client:
        assert (
            client.post(
                f"{MANDATES_URL}/{bob.mandate_id}/constraints", json=CONSTRAINTS
            ).status_code
            == 201
        )

    with as_alice(catalog_settings, alice) as client:
        foreign = client.get(f"{MANDATES_URL}/{bob.mandate_id}/constraints")
        unknown = client.get(f"{MANDATES_URL}/{uuid.uuid7()}/constraints")

    indistinguishable(foreign, unknown)


async def test_a_qualified_mandate_and_an_unqualified_one_answer_alike_across_merchants(
    catalog_settings: Settings, alice: Merchant, bob: Merchant
) -> None:
    """Merchant B has constraints and merchant A must not be able to learn even that.

    Without this, a caller could tell a qualified mandate from an unqualified one by comparing
    the two 404s, and whether a buyer has stated requirements is itself information.
    """
    with TestClient(create_app(catalog_settings), headers=bearer(bob.token)) as client:
        client.post(f"{MANDATES_URL}/{bob.mandate_id}/constraints", json=CONSTRAINTS)

    with as_alice(catalog_settings, alice) as client:
        qualified = client.get(f"{MANDATES_URL}/{bob.mandate_id}/constraints")
        # Alice's own mandate has no constraint set at all.
        unqualified = client.get(f"{MANDATES_URL}/{alice.mandate_id}/constraints")

    assert qualified.status_code == unqualified.status_code == 404
    assert qualified.json()["resource"] == unqualified.json()["resource"] == "intent_constraints"


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "", {"max_total_amount_minor": 1, "currency": "INR"}),
        ("get", "/{mandate}", None),
        ("get", "/{mandate}/validation", None),
        ("post", "/{mandate}/revoke", None),
        ("post", "/{mandate}/constraints", CONSTRAINTS),
        ("get", "/{mandate}/constraints", None),
    ],
)
async def test_every_mandate_operation_refuses_an_anonymous_caller(
    catalog_settings: Settings,
    alice: Merchant,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    """Enumerated rather than assumed. A route added without the dependency fails here."""
    url = f"{MANDATES_URL}{path.format(mandate=alice.mandate_id)}"

    with TestClient(create_app(catalog_settings)) as client:
        response = client.request(method.upper(), url, json=body)

    assert response.status_code == 401
    assert response.json()["error"] == "unauthenticated"


async def test_a_revoked_credential_can_no_longer_reach_a_mandate(
    catalog_settings: Settings, session: AsyncSession, alice: Merchant
) -> None:
    """Revocation over HTTP, on a real route rather than on a probe."""
    parsed = parse_token(alice.token)
    assert parsed is not None

    with as_alice(catalog_settings, alice) as client:
        assert client.get(f"{MANDATES_URL}/{alice.mandate_id}").status_code == 200

        await MerchantCredentialService(session).revoke(parsed.credential_id)

        refused = client.get(f"{MANDATES_URL}/{alice.mandate_id}")

    assert refused.status_code == 401
