"""Merchant API credentials: how one is minted, stored, verified and withdrawn.

The properties worth asserting here are the ones a reader of the code would otherwise have to
take on trust:

- the raw secret is never written anywhere. Not in the credential row, not in an audit payload,
  not in any column of any table
- the token format is parseable and its public half is the credential's own identifier, so
  authentication is a primary key lookup rather than a scan
- one merchant may hold several credentials, and revoking one leaves the others working. That
  is the whole of key rotation, and it either works or the only safe rotation is downtime
- revocation is terminal at the database, not merely in the service

The HTTP side of authentication is `tests/test_merchant_authentication.py`. Nothing here goes
near a route.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.audit.models import ActorType
from agentrank_api.audit.repository import AuditRepository
from agentrank_api.auth.models import MerchantApiCredential
from agentrank_api.auth.repository import MerchantCredentialRepository
from agentrank_api.auth.service import MerchantCredentialService
from agentrank_api.auth.tokens import (
    SECRET_BYTES,
    TokenMarker,
    format_token,
    generate_secret,
    hash_secret,
    parse_token,
    verify_secret,
)
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.errors import NotFoundError

pytestmark = pytest.mark.anyio

DEV = TokenMarker.DEVELOPMENT


async def merchant(session: AsyncSession, slug: str = "ampere-supply") -> uuid.UUID:
    created = await MerchantRepository(session).create(slug=slug, name="Ampere")
    await session.commit()
    return created.id


def test_a_generated_secret_carries_the_stated_entropy() -> None:
    first, second = generate_secret(), generate_secret()

    assert len(first) == SECRET_BYTES * 2
    assert first != second


def test_a_token_round_trips_through_the_parser() -> None:
    credential_id = uuid.uuid7()
    secret = generate_secret()

    parsed = parse_token(format_token(credential_id, secret, marker=DEV))

    assert parsed is not None
    assert parsed.credential_id == credential_id
    assert parsed.secret == secret


def test_a_live_token_and_a_development_token_differ_only_in_the_marker() -> None:
    """The marker is provenance. It is in the string so a leaked key is identifiable."""
    credential_id = uuid.uuid7()
    secret = generate_secret()

    live = format_token(credential_id, secret, marker=TokenMarker.LIVE)
    development = format_token(credential_id, secret, marker=DEV)

    assert live.startswith("ar_live_")
    assert development.startswith("ar_dev_")
    # Both parse, because the marker is a label rather than a check.
    assert parse_token(live) == parse_token(development)


def test_the_marker_only_claims_production_for_production() -> None:
    assert TokenMarker.of("production") is TokenMarker.LIVE
    assert TokenMarker.of("development") is DEV
    assert TokenMarker.of("ci") is DEV


@pytest.mark.parametrize(
    "presented",
    [
        "",
        "not-a-token",
        "Bearer ar_dev_0_0",
        # A well formed shape with the wrong scheme, the wrong marker, a non hexadecimal
        # identifier, a short secret and a long one.
        f"xx_dev_{uuid.uuid7().hex}_{generate_secret()}",
        f"ar_test_{uuid.uuid7().hex}_{generate_secret()}",
        f"ar_dev_{'z' * 32}_{generate_secret()}",
        f"ar_dev_{uuid.uuid7().hex}_{'a' * 10}",
        f"ar_dev_{uuid.uuid7().hex}_{'a' * 128}",
        # The right pieces joined by the wrong separator.
        f"ar_dev_{uuid.uuid7().hex}-{generate_secret()}",
    ],
)
def test_a_malformed_token_parses_to_nothing(presented: str) -> None:
    """Total, so no malformed value reaches the database and none becomes an exception."""
    assert parse_token(presented) is None


def test_a_verifier_states_its_algorithm_and_matches_only_its_own_secret() -> None:
    secret = generate_secret()
    stored = hash_secret(secret)

    assert stored.startswith("sha256:")
    assert secret not in stored
    assert verify_secret(secret, stored) is True
    assert verify_secret(generate_secret(), stored) is False


def test_a_verifier_written_by_an_unknown_algorithm_authenticates_nobody() -> None:
    """The safe direction. An unreadable verifier is a credential that does not work."""
    secret = generate_secret()

    assert verify_secret(secret, f"argon2:{secret}") is False
    assert verify_secret(secret, secret) is False


async def test_issuing_a_credential_stores_a_verifier_and_never_the_secret(
    session: AsyncSession,
) -> None:
    merchant_id = await merchant(session)

    issued = await MerchantCredentialService(session).issue(
        merchant_id=merchant_id, label="local development", marker=DEV
    )

    assert issued.token.startswith("ar_dev_")
    assert issued.credential.merchant_id == merchant_id
    assert issued.credential.is_active is True

    stored = await MerchantCredentialRepository(session).get(issued.credential.id)
    assert stored is not None
    assert stored.secret_hash == hash_secret(parse_token(issued.token).secret)  # type: ignore[union-attr]
    assert issued.token not in stored.secret_hash


async def test_no_column_anywhere_holds_the_raw_secret(session: AsyncSession) -> None:
    """The claim is about the database, so it is asserted against the database.

    Every text and JSON column of every table is searched for the secret half of the token.
    Asserting it about the credential row alone would miss the audit payload, which is the other
    place a careless implementation would put it.
    """
    merchant_id = await merchant(session)
    issued = await MerchantCredentialService(session).issue(
        merchant_id=merchant_id, label="local development", marker=DEV
    )
    parsed = parse_token(issued.token)
    assert parsed is not None

    # Column names come from the catalog rather than from a caller, and the secret is bound as
    # a parameter rather than interpolated.
    columns = (
        await session.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns"
                " WHERE table_schema = 'public'"
                " AND data_type IN ('text', 'character varying', 'jsonb')"
            )
        )
    ).all()
    assert columns

    for table_name, column_name in columns:
        found = (
            await session.execute(
                text(
                    f'SELECT count(*) FROM "{table_name}"'  # noqa: S608
                    f' WHERE "{column_name}"::text LIKE :needle'
                ),
                {"needle": f"%{parsed.secret}%"},
            )
        ).scalar_one()
        assert found == 0, f"{table_name}.{column_name} holds the raw secret"


async def test_issuing_records_that_it_happened_without_recording_the_key(
    session: AsyncSession,
) -> None:
    merchant_id = await merchant(session)

    issued = await MerchantCredentialService(session).issue(
        merchant_id=merchant_id, label="checkout integration", marker=DEV
    )

    events = await AuditRepository(session).list_for_merchant(merchant_id)
    assert [event.event_type for event in events] == ["credential.issued"]
    recorded = events[0]
    assert recorded.actor_type is ActorType.SYSTEM
    assert recorded.resource_type == "merchant_api_credential"
    assert recorded.resource_id == issued.credential.id
    assert recorded.payload == {"label": "checkout integration"}
    # The credential did not authorize its own creation. Somebody with a shell did.
    assert recorded.credential_id is None


async def test_issuing_for_an_unknown_merchant_is_refused_by_name(session: AsyncSession) -> None:
    with pytest.raises(NotFoundError):
        await MerchantCredentialService(session).issue(
            merchant_id=uuid.uuid7(), label="nobody", marker=DEV
        )


@pytest.mark.parametrize("label", ["", "   ", "a" * 101, "two\nlines"])
async def test_an_unusable_label_is_refused(session: AsyncSession, label: str) -> None:
    merchant_id = await merchant(session)

    with pytest.raises(ValueError):
        await MerchantCredentialService(session).issue(
            merchant_id=merchant_id, label=label, marker=DEV
        )


async def test_a_valid_credential_authenticates_as_its_own_merchant(session: AsyncSession) -> None:
    merchant_id = await merchant(session)
    service = MerchantCredentialService(session)
    issued = await service.issue(merchant_id=merchant_id, label="one", marker=DEV)

    principal = await service.authenticate(issued.token)

    assert principal is not None
    assert principal.merchant_id == merchant_id
    assert principal.credential_id == issued.credential.id


async def test_a_wrong_secret_against_a_real_identifier_authenticates_nothing(
    session: AsyncSession,
) -> None:
    merchant_id = await merchant(session)
    service = MerchantCredentialService(session)
    issued = await service.issue(merchant_id=merchant_id, label="one", marker=DEV)
    forged = format_token(issued.credential.id, generate_secret(), marker=DEV)

    assert await service.authenticate(forged) is None


async def test_an_unknown_identifier_authenticates_nothing(session: AsyncSession) -> None:
    unissued = format_token(uuid.uuid7(), generate_secret(), marker=DEV)

    assert await MerchantCredentialService(session).authenticate(unissued) is None


async def test_one_merchant_may_hold_several_credentials_and_rotate_between_them(
    session: AsyncSession,
) -> None:
    """Rotation, which is the reason several credentials are allowed at all."""
    merchant_id = await merchant(session)
    service = MerchantCredentialService(session)
    first = await service.issue(merchant_id=merchant_id, label="old", marker=DEV)
    second = await service.issue(merchant_id=merchant_id, label="new", marker=DEV)

    assert await service.authenticate(first.token) is not None
    assert await service.authenticate(second.token) is not None

    await service.revoke(first.credential.id)

    assert await service.authenticate(first.token) is None
    assert await service.authenticate(second.token) is not None


async def test_revocation_is_idempotent_and_records_one_event(session: AsyncSession) -> None:
    merchant_id = await merchant(session)
    service = MerchantCredentialService(session)
    issued = await service.issue(merchant_id=merchant_id, label="one", marker=DEV)

    first = await service.revoke(issued.credential.id)
    second = await service.revoke(issued.credential.id)

    assert first.changed is True
    assert second.changed is False
    assert second.credential.revoked_at == first.credential.revoked_at
    assert second.credential.is_active is False

    events = await AuditRepository(session).list_for_merchant(merchant_id)
    assert [event.event_type for event in events] == ["credential.issued", "credential.revoked"]


async def test_revoking_an_unknown_credential_is_refused_by_name(session: AsyncSession) -> None:
    with pytest.raises(NotFoundError):
        await MerchantCredentialService(session).revoke(uuid.uuid7())


async def test_a_listing_shows_revoked_credentials_too(session: AsyncSession) -> None:
    """A listing that hid them could not answer "was this key ever ours"."""
    merchant_id = await merchant(session)
    service = MerchantCredentialService(session)
    first = await service.issue(merchant_id=merchant_id, label="old", marker=DEV)
    second = await service.issue(merchant_id=merchant_id, label="new", marker=DEV)
    await service.revoke(first.credential.id)

    listed = await service.list_for_merchant(merchant_id)

    assert [credential.id for credential in listed] == [first.credential.id, second.credential.id]
    assert [credential.is_active for credential in listed] == [False, True]


async def test_a_listing_is_scoped_to_one_merchant(session: AsyncSession) -> None:
    first = await merchant(session, slug="ampere-supply")
    second = await merchant(session, slug="volt-works")
    service = MerchantCredentialService(session)
    mine = await service.issue(merchant_id=first, label="mine", marker=DEV)
    await service.issue(merchant_id=second, label="theirs", marker=DEV)

    listed = await service.list_for_merchant(first)

    assert [credential.id for credential in listed] == [mine.credential.id]


async def test_listing_an_unknown_merchant_is_refused_rather_than_empty(
    session: AsyncSession,
) -> None:
    with pytest.raises(NotFoundError):
        await MerchantCredentialService(session).list_for_merchant(uuid.uuid7())


async def test_the_database_refuses_a_second_credential_with_one_verifier(
    session: AsyncSession,
) -> None:
    """One secret, one credential. Unreachable by chance, and this makes it unreachable by a
    copy."""
    merchant_id = await merchant(session)
    repository = MerchantCredentialRepository(session)
    secret = generate_secret()
    await repository.create(merchant_id=merchant_id, secret_hash=hash_secret(secret), label="one")

    with pytest.raises(IntegrityError):
        await repository.create(
            merchant_id=merchant_id, secret_hash=hash_secret(secret), label="two"
        )
    await session.rollback()


async def test_the_database_refuses_a_verifier_that_is_not_a_labelled_digest(
    session: AsyncSession,
) -> None:
    merchant_id = await merchant(session)

    with pytest.raises(IntegrityError):
        await MerchantCredentialRepository(session).create(
            merchant_id=merchant_id, secret_hash=generate_secret(), label="raw"
        )
    await session.rollback()


@pytest.mark.parametrize(
    "assignment",
    [
        "merchant_id = gen_random_uuid()",
        "secret_hash = 'sha256:" + "0" * 64 + "'",
        "label = 'renamed'",
        "created_at = now()",
    ],
)
async def test_the_database_refuses_every_change_except_revocation(
    session: AsyncSession, assignment: str
) -> None:
    merchant_id = await merchant(session)
    issued = await MerchantCredentialService(session).issue(
        merchant_id=merchant_id, label="one", marker=DEV
    )

    with pytest.raises(DBAPIError):
        # The assignment is a constant from this test, never a caller supplied value.
        await session.execute(
            text(f"UPDATE merchant_api_credential SET {assignment} WHERE id = :id"),  # noqa: S608
            {"id": issued.credential.id},
        )
    await session.rollback()


async def test_the_database_refuses_to_unrevoke_a_credential(session: AsyncSession) -> None:
    """Terminal at the database, not merely in the service."""
    merchant_id = await merchant(session)
    service = MerchantCredentialService(session)
    issued = await service.issue(merchant_id=merchant_id, label="one", marker=DEV)
    await service.revoke(issued.credential.id)

    with pytest.raises(DBAPIError):
        await session.execute(
            text("UPDATE merchant_api_credential SET revoked_at = NULL WHERE id = :id"),
            {"id": issued.credential.id},
        )
    await session.rollback()


async def test_the_authentication_read_never_returns_a_revoked_credential(
    session: AsyncSession,
) -> None:
    """The condition is in the SQL, so revoked and never existed are one answer."""
    merchant_id = await merchant(session)
    service = MerchantCredentialService(session)
    issued = await service.issue(merchant_id=merchant_id, label="one", marker=DEV)
    repository = MerchantCredentialRepository(session)
    await service.revoke(issued.credential.id)

    assert await repository.get_active(issued.credential.id) is None
    # The operator read still finds it, because an operator has to be able to see one.
    found = await repository.get(issued.credential.id)
    assert isinstance(found, MerchantApiCredential)
    assert found.is_active is False
