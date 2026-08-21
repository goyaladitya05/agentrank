"""The credential command line, run for real against PostgreSQL.

Nothing is mocked away. `main` is called with the arguments an operator would type, it opens its
own engine against the test database, and every command reaches the real service, the real
repository and the real database constraints. The only injected thing is the settings, so the
commands hit the test database rather than the developer's.

Three claims are worth the file:

- a key minted here actually authenticates over HTTP. A provisioning tool that produced tokens
  the API rejected would pass every test written about the tool alone
- the secret appears exactly once, in the output of `create`, and nowhere else. Not in `list`,
  not in `revoke`, not in the row it wrote
- revoking is terminal and idempotent, and the second run is a zero rather than a refusal
"""

import asyncio
import json
import uuid
from dataclasses import dataclass
from io import StringIO

import pytest
from conftest import bearer
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.audit.repository import AuditRepository
from agentrank_api.cli import ExitCode, main
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.config import Settings
from agentrank_api.main import create_app
from agentrank_api.payments.fake import FakePaymentProvider

pytestmark = pytest.mark.anyio

SLUG = "ampere-supply"
PRODUCTS_URL = "/api/v1/commerce/products/search"


@dataclass(frozen=True, slots=True)
class Run:
    """What one command invocation produced: its exit code and both streams."""

    code: int
    out: str
    err: str

    def json(self) -> dict[str, object]:
        parsed: dict[str, object] = json.loads(self.out)
        return parsed


async def run(settings: Settings, *arguments: str) -> Run:
    """Invoke the command line exactly as a shell would, and capture both streams.

    In a thread, because `main` owns its event loop: it calls `asyncio.run`, which is what a
    process entry point should do and what cannot be done from inside the loop a test is already
    running on. Running it in a worker thread exercises the real entry point, including the exit
    code mapping, rather than reaching past it into the commands.

    A provider is passed because the runner hands one to every command. These commands never
    touch it, which is the point of the shared signature.

    `SystemExit` is caught and turned back into a code, because that is what a shell sees.
    argparse exits the process rather than returning when the arguments are wrong, and a test
    that let that escape would be asserting about Python rather than about the command.
    """
    out, err = StringIO(), StringIO()

    def invoke() -> int:
        try:
            return main(
                list(arguments),
                settings=settings,
                provider=FakePaymentProvider(),
                out=out,
                err=err,
            )
        except SystemExit as stopped:
            return int(stopped.code or 0)

    code = await asyncio.to_thread(invoke)
    return Run(code=code, out=out.getvalue(), err=err.getvalue())


@pytest.fixture
async def merchant_id(session: AsyncSession) -> uuid.UUID:
    merchant = await MerchantRepository(session).create(slug=SLUG, name="Ampere Supply")
    await session.commit()
    return merchant.id


async def mint(settings: Settings, label: str = "local") -> dict[str, object]:
    issued = await run(
        settings, "credentials", "create", "--merchant-slug", SLUG, "--label", label, "--json"
    )
    assert issued.code == ExitCode.OK
    return issued.json()


async def test_a_created_key_authenticates_over_http(
    catalog_settings: Settings, merchant_id: uuid.UUID
) -> None:
    """The claim the whole command exists for, and the one a unit test could not make.

    A tool that minted tokens the API rejected would look perfectly healthy from the inside.
    """
    created = await mint(catalog_settings)
    token = str(created["token"])

    with TestClient(create_app(catalog_settings), headers=bearer(token)) as client:
        response = client.post(PRODUCTS_URL, json={"query": "charger"})

    assert response.status_code == 200
    assert created["merchant_id"] == str(merchant_id)


async def test_a_created_key_is_printed_once_and_stored_nowhere(
    catalog_settings: Settings, session: AsyncSession, merchant_id: uuid.UUID
) -> None:
    """The secret exists in the output of one command and in no column of any table."""
    created = await mint(catalog_settings)
    secret = str(created["token"]).rsplit("_", maxsplit=1)[1]

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
        # Names come from the catalog, never from a caller, and the secret is a parameter.
        found = (
            await session.execute(
                text(
                    f'SELECT count(*) FROM "{table_name}"'  # noqa: S608
                    f' WHERE "{column_name}"::text LIKE :needle'
                ),
                {"needle": f"%{secret}%"},
            )
        ).scalar_one()
        assert found == 0, f"{table_name}.{column_name} holds the raw secret"


async def test_the_plain_output_warns_that_the_key_is_shown_once(
    catalog_settings: Settings, merchant_id: uuid.UUID
) -> None:
    issued = await run(
        catalog_settings, "credentials", "create", "--merchant-slug", SLUG, "--label", "local"
    )

    assert issued.code == ExitCode.OK
    assert "ar_dev_" in issued.out
    assert "only time this key is shown" in issued.out


async def test_a_key_can_be_minted_by_identifier_as_well_as_by_slug(
    catalog_settings: Settings, merchant_id: uuid.UUID
) -> None:
    issued = await run(
        catalog_settings,
        "credentials",
        "create",
        "--merchant-id",
        str(merchant_id),
        "--label",
        "by identifier",
        "--json",
    )

    assert issued.code == ExitCode.OK
    assert issued.json()["merchant_id"] == str(merchant_id)


async def test_naming_a_merchant_twice_or_not_at_all_is_a_usage_error(
    catalog_settings: Settings, merchant_id: uuid.UUID
) -> None:
    """Two explicit flags rather than one that guesses, and argparse enforces the choice."""
    neither = await run(catalog_settings, "credentials", "create", "--label", "x")
    both = await run(
        catalog_settings,
        "credentials",
        "create",
        "--merchant-id",
        str(merchant_id),
        "--merchant-slug",
        SLUG,
        "--label",
        "x",
    )

    assert neither.code == ExitCode.USAGE
    assert both.code == ExitCode.USAGE


async def test_an_unknown_merchant_is_a_not_found_exit(catalog_settings: Settings) -> None:
    by_slug = await run(
        catalog_settings, "credentials", "create", "--merchant-slug", "nobody", "--label", "x"
    )
    by_id = await run(
        catalog_settings,
        "credentials",
        "create",
        "--merchant-id",
        str(uuid.uuid7()),
        "--label",
        "x",
    )

    assert by_slug.code == ExitCode.NOT_FOUND
    assert by_id.code == ExitCode.NOT_FOUND
    assert "not found" in by_slug.err


async def test_a_blank_label_is_refused_before_anything_is_written(
    catalog_settings: Settings, merchant_id: uuid.UUID
) -> None:
    """A label is required because a listing nobody can read is a listing nobody can act on.

    Both refusals are usage errors rather than crashes. The label is validated by argparse using
    the same function the service applies, so an operator who typed a blank one is told so.
    """
    blank = await run(
        catalog_settings, "credentials", "create", "--merchant-slug", SLUG, "--label", "   "
    )
    unprintable = await run(
        catalog_settings, "credentials", "create", "--merchant-slug", SLUG, "--label", "two\nlines"
    )
    missing = await run(catalog_settings, "credentials", "create", "--merchant-slug", SLUG)

    assert blank.code == ExitCode.USAGE
    assert unprintable.code == ExitCode.USAGE
    assert missing.code == ExitCode.USAGE

    listed = await run(catalog_settings, "credentials", "list", "--merchant-slug", SLUG, "--json")
    assert listed.json()["count"] == 0


async def test_a_listing_shows_every_key_and_no_secret(
    catalog_settings: Settings, merchant_id: uuid.UUID
) -> None:
    first = await mint(catalog_settings, "old")
    second = await mint(catalog_settings, "new")

    listed = await run(catalog_settings, "credentials", "list", "--merchant-slug", SLUG, "--json")

    body = listed.json()
    assert body["count"] == 2
    credentials = body["credentials"]
    assert isinstance(credentials, list)
    assert [entry["credential_id"] for entry in credentials] == [
        first["credential_id"],
        second["credential_id"],
    ]
    assert [entry["label"] for entry in credentials] == ["old", "new"]
    assert [entry["status"] for entry in credentials] == ["ACTIVE", "ACTIVE"]
    # Nothing in a listing is a secret, and nothing in it could be: none is stored.
    assert str(first["token"]) not in listed.out
    assert "token" not in listed.out


async def test_a_listing_is_scoped_to_one_merchant(
    catalog_settings: Settings, session: AsyncSession, merchant_id: uuid.UUID
) -> None:
    await MerchantRepository(session).create(slug="volt-works", name="Volt")
    await session.commit()
    await mint(catalog_settings, "mine")

    theirs = await run(
        catalog_settings, "credentials", "list", "--merchant-slug", "volt-works", "--json"
    )

    assert theirs.json()["count"] == 0


async def test_revoking_stops_a_key_working_and_leaves_the_others_alone(
    catalog_settings: Settings, merchant_id: uuid.UUID
) -> None:
    """Rotation from the operator's side: mint the replacement, then withdraw the old one."""
    old = await mint(catalog_settings, "old")
    new = await mint(catalog_settings, "new")

    withdrawn = await run(
        catalog_settings, "credentials", "revoke", str(old["credential_id"]), "--json"
    )

    assert withdrawn.code == ExitCode.OK
    assert withdrawn.json()["changed"] is True
    assert withdrawn.json()["status"] == "REVOKED"

    with TestClient(create_app(catalog_settings)) as client:
        refused = client.post(PRODUCTS_URL, json={"query": "x"}, headers=bearer(str(old["token"])))
        accepted = client.post(PRODUCTS_URL, json={"query": "x"}, headers=bearer(str(new["token"])))

    assert refused.status_code == 401
    assert accepted.status_code == 200


async def test_revoking_twice_is_a_zero_that_says_nothing_changed(
    catalog_settings: Settings, merchant_id: uuid.UUID
) -> None:
    issued = await mint(catalog_settings)

    first = await run(
        catalog_settings, "credentials", "revoke", str(issued["credential_id"]), "--json"
    )
    second = await run(
        catalog_settings, "credentials", "revoke", str(issued["credential_id"]), "--json"
    )

    assert first.code == second.code == ExitCode.OK
    assert first.json()["changed"] is True
    assert second.json()["changed"] is False
    assert second.json()["revoked_at"] == first.json()["revoked_at"]


async def test_the_plain_revocation_output_says_whether_it_changed_anything(
    catalog_settings: Settings, merchant_id: uuid.UUID
) -> None:
    issued = await mint(catalog_settings)

    first = await run(catalog_settings, "credentials", "revoke", str(issued["credential_id"]))
    second = await run(catalog_settings, "credentials", "revoke", str(issued["credential_id"]))

    assert "changed     yes" in first.out
    assert "already revoked" in second.out


async def test_revoking_an_unknown_credential_is_a_not_found_exit(
    catalog_settings: Settings,
) -> None:
    withdrawn = await run(catalog_settings, "credentials", "revoke", str(uuid.uuid7()))

    assert withdrawn.code == ExitCode.NOT_FOUND
    assert "not found" in withdrawn.err


async def test_provisioning_is_recorded_in_the_audit_trail(
    catalog_settings: Settings, session: AsyncSession, merchant_id: uuid.UUID
) -> None:
    """Issuing and withdrawing a key are both things that happened to a merchant."""
    issued = await mint(catalog_settings)
    await run(catalog_settings, "credentials", "revoke", str(issued["credential_id"]))

    events = await AuditRepository(session).list_for_merchant(merchant_id)

    assert [event.event_type for event in events] == ["credential.issued", "credential.revoked"]
    # An operator is not a credential, so nothing here claims one authorized it.
    assert [event.credential_id for event in events] == [None, None]
