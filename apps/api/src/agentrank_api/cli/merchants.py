"""Provisioning a merchant, which is the first thing an operator does and the only thing
AgentRank has no other way to do.

Every other artifact in this product is created by the merchant themselves through the console:
their source, their evaluation setup, their compiler runs, their reviews, their evaluations. A
merchant row is not, because there is no public signup and the credential that would authenticate
one does not exist until somebody with a shell issues it.

Until this existed the only command that created a merchant was `benchmark seed`, which also
registers an authored benchmark world and materializes an authored catalog. That is right for
VoltEdge and wrong for a real merchant: a merchant who arrives with an authored world already
registered is refused an evaluation setup of their own, by name, because AgentRank will not
silently replace a world an operator put there. So provisioning a merchant who will import their
own pages meant either editing the database by hand or seeding a world they then had to work
around, and neither is a bootstrap path.

Two commands and nothing else. Creating a merchant is a slug and a display name; listing them is
how an operator finds a slug for every other command. Neither writes a product, a price, a
catalog, a world or a credential: a credential is `credentials create`, which is a separate
command because issuing a secret is a separate act.
"""

import argparse
from typing import Any, TextIO

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentrank_api.auth.models import MerchantApiCredential
from agentrank_api.cli.exits import ExitCode
from agentrank_api.cli.output import write_json
from agentrank_api.commerce.models import Merchant
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.config import Settings
from agentrank_api.payments.provider import PaymentProvider

# How many merchants a listing reads at once. An operator looking for a slug needs the ones that
# exist, and a private beta has tens rather than thousands; the bound is here so the command
# stays a diagnostic rather than becoming a report.
MAX_LISTED = 200

SLUG_WIDTH = 32
NAME_WIDTH = 32


def add_commands(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="command_name", required=True)
    creating = commands.add_parser(
        "create",
        help="create a merchant, with no catalog, no world and no credential",
        description=(
            "Provision one merchant. This writes a merchant row and nothing else: no products,"
            " no prices, no benchmark world and no API key. The merchant supplies their own"
            " catalog by importing their public pages or by submitting a source document, and"
            " their evaluation setup is generated from that. Issue their key separately with"
            " `credentials create --merchant-slug <slug>`."
        ),
    )
    creating.add_argument("--merchant-slug", required=True, help="the merchant's stable slug")
    creating.add_argument("--name", required=True, help="the merchant's display name")
    creating.add_argument("--json", dest="as_json", action="store_true")
    creating.set_defaults(command=create)

    listing = commands.add_parser(
        "list",
        help="every merchant this deployment holds",
        description=(
            "Read the merchants that exist, with how many API credentials each one has open."
            " This is where the slug every other operator command takes comes from."
        ),
    )
    listing.add_argument("--json", dest="as_json", action="store_true")
    listing.set_defaults(command=index)


async def create(
    session: AsyncSession,
    sessions: async_sessionmaker[AsyncSession],
    provider: PaymentProvider,
    arguments: argparse.Namespace,
    out: TextIO,
    settings: Settings,
) -> int:
    """Create one merchant, or report the one that already has this slug.

    Idempotent by slug rather than refusing, because a provisioning command an operator may have
    already run is one they should be able to run again. `created` says which happened, so a
    script can tell a fresh provisioning from a repeat.
    """
    del sessions, provider, settings
    merchants = MerchantRepository(session)
    existing = await merchants.get_by_slug(arguments.merchant_slug)
    created = existing is None
    if existing is None:
        try:
            existing = await merchants.create(slug=arguments.merchant_slug, name=arguments.name)
            await session.commit()
        except IntegrityError:
            # A concurrent operator, or a slug this schema refuses. Reading it back separates
            # the two: a row that appeared is the first, and none is the second.
            await session.rollback()
            existing = await merchants.get_by_slug(arguments.merchant_slug)
            created = False
            if existing is None:
                print(
                    f"refused     {arguments.merchant_slug!r} is not a merchant slug this schema"
                    " accepts",
                    file=out,
                )
                return ExitCode.REFUSED

    payload = {
        "merchant_id": str(existing.id),
        "merchant_slug": existing.slug,
        "name": existing.name,
        "created": created,
    }
    if arguments.as_json:
        write_json(out, payload)
        return ExitCode.OK
    print(f"merchant    {payload['merchant_slug']}  {payload['merchant_id']}", file=out)
    print(f"name        {payload['name']}", file=out)
    print(
        "created     yes" if created else "created     no, this merchant already existed", file=out
    )
    print("", file=out)
    print(
        "next        issue their console key with"
        f" `credentials create --merchant-slug {payload['merchant_slug']} --label console`",
        file=out,
    )
    return ExitCode.OK


async def index(
    session: AsyncSession,
    sessions: async_sessionmaker[AsyncSession],
    provider: PaymentProvider,
    arguments: argparse.Namespace,
    out: TextIO,
    settings: Settings,
) -> int:
    """Every merchant, with how many credentials each has that have not been revoked."""
    del sessions, provider, settings
    open_credentials = (
        select(
            MerchantApiCredential.merchant_id.label("merchant_id"),
            func.count().label("open"),
        )
        .where(MerchantApiCredential.revoked_at.is_(None))
        .group_by(MerchantApiCredential.merchant_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(Merchant, func.coalesce(open_credentials.c.open, 0))
            .outerjoin(open_credentials, open_credentials.c.merchant_id == Merchant.id)
            .order_by(Merchant.slug)
            .limit(MAX_LISTED)
        )
    ).all()
    total = await session.scalar(select(func.count()).select_from(Merchant))
    payload: dict[str, Any] = {
        # What exists, beside what is shown. "Which merchants exist" is a question this command
        # is the only answer to, and at more than `MAX_LISTED` a capped list is a wrong answer
        # that looks like a complete one.
        "total": int(total or 0),
        "merchants": [
            {
                "merchant_id": str(merchant.id),
                "merchant_slug": merchant.slug,
                "name": merchant.name,
                "open_credentials": int(open_count),
            }
            for merchant, open_count in rows
        ],
    }
    if arguments.as_json:
        write_json(out, payload)
        return ExitCode.OK
    if not payload["merchants"]:
        print("no merchants have been provisioned", file=out)
        return ExitCode.OK
    print(f"{'slug':<{SLUG_WIDTH}}  {'name':<{NAME_WIDTH}}  open keys", file=out)
    for entry in payload["merchants"]:
        print(
            f"{entry['merchant_slug']!s:<{SLUG_WIDTH}}  {entry['name']!s:<{NAME_WIDTH}}"
            f"  {entry['open_credentials']}",
            file=out,
        )
    shown = len(payload["merchants"])
    counted = payload["total"]
    print(
        f"\n{shown} merchant(s)" + ("" if shown == counted else f", showing {shown} of {counted}"),
        file=out,
    )
    return ExitCode.OK
