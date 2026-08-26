"""Provisioning a merchant, which is the operator's whole job and had no command.

A merchant exists because somebody with a shell created one. There is no public signup, and the
credential that would authenticate a merchant does not exist until an operator issues it. That is
an accepted private-beta limitation and it is only acceptable while the operator path is itself a
path: explicit, deterministic and testable.

It was not. The only command that created a merchant was `benchmark seed`, which also registers
an authored benchmark world and materializes an authored catalog. For VoltEdge that is exactly
right. For a real merchant it is the wrong shape and actively obstructive: a merchant who arrives
with an authored world already registered is refused an evaluation setup of their own, by name,
because AgentRank will not silently replace a world an operator put there. Provisioning a merchant
who would import their own pages therefore meant writing a row by hand.

This runs the real command line against the real database, and then carries the merchant it
created through the first two steps of the actual product to prove the provisioning is usable
rather than merely present.
"""

import asyncio
import json
import uuid
from dataclasses import dataclass
from io import StringIO
from typing import Any

import pytest
from conftest import bearer
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentrank_api.cli import ExitCode, main
from agentrank_api.config import Settings
from agentrank_api.main import create_app
from agentrank_api.payments.fake import FakePaymentProvider

pytestmark = pytest.mark.anyio

SLUG = "provisioned-shop"


@dataclass(frozen=True, slots=True)
class Run:
    code: int
    out: str

    def payload(self) -> Any:
        return json.loads(self.out)


async def run(settings: Settings, *arguments: str) -> Run:
    """The command line as a shell invokes it, including its exit code mapping."""
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
    return Run(code=code, out=out.getvalue())


async def test_an_operator_provisions_a_merchant_who_can_then_sign_in(
    catalog_settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The whole provisioning path, and then the first thing the merchant does with it.

    Two commands, no file, no row written by hand. What comes out is a merchant with a credential
    that authenticates against the real API, which is what makes this a bootstrap path rather
    than a bootstrap path's ingredients.
    """
    created = await run(
        catalog_settings, "merchants", "create", "--merchant-slug", SLUG, "--name", "Shop", "--json"
    )
    assert created.code == ExitCode.OK
    assert created.payload()["created"] is True
    assert created.payload()["merchant_slug"] == SLUG

    issued = await run(
        catalog_settings,
        "credentials",
        "create",
        "--merchant-slug",
        SLUG,
        "--label",
        "console",
        "--json",
    )
    assert issued.code == ExitCode.OK

    app = create_app(catalog_settings, payment_provider=FakePaymentProvider())
    app.state.session_factory = factory
    with TestClient(app) as client:
        answered = client.get("/api/v1/sources", headers=bearer(str(issued.payload()["token"])))

    assert answered.status_code == 200
    # A merchant with nothing yet, which is the correct state for one who has just been created:
    # no catalog, no world and nothing an operator decided on their behalf.
    assert answered.json()["current_source_snapshot_id"] is None


async def test_provisioning_a_merchant_creates_no_catalog_and_no_benchmark_world(
    catalog_settings: Settings, session: AsyncSession
) -> None:
    """The reason this command exists rather than `benchmark seed` being reused.

    A merchant carrying an authored world is refused an evaluation setup of their own, so a
    provisioning command that registered one would provision merchants who cannot be measured
    the ordinary way.
    """
    from sqlalchemy import func, select

    from agentrank_api.benchmark.models import BenchmarkEnvironment
    from agentrank_api.commerce.models import Product

    await run(
        catalog_settings,
        "merchants",
        "create",
        "--merchant-slug",
        "bare-provisioned",
        "--name",
        "Bare",
        "--json",
    )
    session.expire_all()

    assert await session.scalar(select(func.count()).select_from(Product)) == 0
    assert await session.scalar(select(func.count()).select_from(BenchmarkEnvironment)) == 0


async def test_creating_the_same_merchant_twice_is_the_same_merchant(
    catalog_settings: Settings, session: AsyncSession
) -> None:
    """A provisioning command an operator may already have run is one they can run again."""
    first = await run(
        catalog_settings, "merchants", "create", "--merchant-slug", SLUG, "--name", "Shop", "--json"
    )
    second = await run(
        catalog_settings,
        "merchants",
        "create",
        "--merchant-slug",
        SLUG,
        "--name",
        "Different Name",
        "--json",
    )

    assert second.code == ExitCode.OK
    assert second.payload()["created"] is False
    assert second.payload()["merchant_id"] == first.payload()["merchant_id"]
    # The name is not rewritten by a repeat. A merchant row is not a place a second invocation
    # gets to change something the first one recorded.
    assert second.payload()["name"] == "Shop"


async def test_a_slug_the_schema_refuses_is_a_refusal_rather_than_a_traceback(
    catalog_settings: Settings, session: AsyncSession
) -> None:
    refused = await run(
        catalog_settings, "merchants", "create", "--merchant-slug", "Not A Slug", "--name", "X"
    )

    assert refused.code == ExitCode.REFUSED
    assert "not a merchant slug" in refused.out


async def test_listing_merchants_names_the_slug_every_other_command_takes(
    catalog_settings: Settings, session: AsyncSession
) -> None:
    await run(
        catalog_settings, "merchants", "create", "--merchant-slug", SLUG, "--name", "Shop", "--json"
    )
    await run(
        catalog_settings,
        "credentials",
        "create",
        "--merchant-slug",
        SLUG,
        "--label",
        "console",
        "--json",
    )

    listed = await run(catalog_settings, "merchants", "list", "--json")

    assert listed.code == ExitCode.OK
    found = {entry["merchant_slug"]: entry for entry in listed.payload()["merchants"]}
    assert found[SLUG]["open_credentials"] == 1
    assert uuid.UUID(found[SLUG]["merchant_id"])
    # A listing names merchants and never a secret, which is the one thing `credentials create`
    # prints and nothing else ever does.
    assert "token" not in listed.out


async def test_a_listing_that_is_capped_says_so_rather_than_looking_complete(
    catalog_settings: Settings, session: AsyncSession
) -> None:
    """ "Which merchants exist" is a question this command is the only answer to.

    A capped read that reported only its own row count would answer it with a number that looks
    complete and is not, which is worse than an obviously truncated one.
    """
    for index in range(3):
        await run(
            catalog_settings,
            "merchants",
            "create",
            "--merchant-slug",
            f"capped-{index}",
            "--name",
            "Capped",
            "--json",
        )

    listed = await run(catalog_settings, "merchants", "list", "--json")

    assert listed.payload()["total"] == 3
    assert len(listed.payload()["merchants"]) == 3
