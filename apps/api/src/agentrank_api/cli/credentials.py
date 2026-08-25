"""The merchant credential operator commands, and why they are commands rather than routes.

Provisioning is the one capability that must never be reachable over the network. An endpoint
that issued a merchant API key would be an endpoint that could issue one for any merchant, and
nothing could authenticate the caller asking for it, because a credential is the thing that
makes authentication possible in the first place. That is a bootstrapping problem with exactly
one honest answer: the first credential comes from somebody with a shell.

So there are three commands, all local, all delegating to `MerchantCredentialService`:

```text
create           mints a key and prints it once      the only place a secret ever exists
list             shows what a merchant holds         never shows a secret, none is stored
revoke           withdraws one key, terminally       takes effect on the next request
sessions         shows a merchant's open consoles    no verifier, because none is stored
revoke-sessions  signs a merchant out everywhere     leaves their API keys working
purge-sessions   deletes settled session rows        bounded, and never an open one
```

The three session commands exist because a durable browser session is a thing an operator has to
be able to act on. Signing a merchant out of every console is not the same as revoking their key:
one ends browser access and leaves their integrations working, the other ends both. And a table
of sessions that only grows is a table nobody pruned, so there is a bounded delete for the rows
that stopped being usable long enough ago to be uninteresting.

`create` is the only command in this application that prints a secret. It prints it once, to
stdout, and nothing can produce it again: the row holds a SHA-256 verifier and there is no
application path from a verifier back to a token. An operator who loses one issues another and
revokes the first, which is the same thing they would do if it leaked.

Run it through the repository environment:

```bash
uv run python -m agentrank_api.cli credentials create --merchant-slug ampere-supply --label local
uv run python -m agentrank_api.cli credentials list --merchant-slug ampere-supply
uv run python -m agentrank_api.cli credentials revoke <credential-id>
```

The trust boundary is the same one the payment commands have and it is the same argument: to
run these you must already be able to run this repository's code against this repository's
database, and anybody who can do that could write the session by hand. That argument holds only
while the surface is local. See docs/security.md.

Operator identity is still not recorded. `credential.issued` and `credential.revoked` are
attributed to `SYSTEM`, because this application acted on somebody's instruction and it does not
know whose. Nothing here reads a Unix username and calls it authentication.
"""

import argparse
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, TextIO

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentrank_api.auth.console import ConsoleSessionService
from agentrank_api.auth.models import MerchantApiCredential, MerchantConsoleSession
from agentrank_api.auth.service import MerchantCredentialService, validate_label
from agentrank_api.auth.tokens import TokenMarker
from agentrank_api.cli.exits import ExitCode
from agentrank_api.cli.output import write_json
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.config import Settings
from agentrank_api.errors import NotFoundError
from agentrank_api.payments.provider import PaymentProvider

# A version 7 identifier is thirty six characters. The other widths are chosen so that a row is
# one line on an ordinary terminal, and they live here rather than inline so that the header and
# the rows cannot drift apart.
ID_WIDTH = 36
STATUS_WIDTH = 8
LABEL_WIDTH = 24
STAMP_WIDTH = 20

ACTIVE = "ACTIVE"
REVOKED = "REVOKED"

# What a column prints when there is nothing to print. The same character the payment listing
# uses, so two tables from one tool do not disagree about what absence looks like.
MISSING = "-"

# What `create` prints above the secret. A key is useless to its holder if they do not realise
# this is the only time they will see it, and a warning after the value has already scrolled
# past is a warning nobody reads.
ONCE_ONLY = "This is the only time this key is shown. Nothing stores it and nothing can recover it."

# How long a settled console session stays readable before cleanup will delete it, and how many
# one invocation removes. A week because the question a settled session answers is "why was this
# merchant signed out", which is asked within days or not at all; a bounded batch because this is
# an operator command and an unbounded delete is not something to type by accident.
DEFAULT_PURGE_DAYS = 7
DEFAULT_PURGE_LIMIT = 1000


def add_commands(parser: argparse.ArgumentParser) -> None:
    """Declare the credential command surface.

    Three commands, each with its own subparser, each binding itself to its implementation with
    `set_defaults`, exactly as the payment commands do.
    """
    commands = parser.add_subparsers(dest="command_name", required=True)

    minting = commands.add_parser(
        "create",
        help="issue a merchant API key and print it once. This is the only time it is shown",
        description=(
            "Mint one credential for one merchant. The secret is generated here, printed"
            " once, and stored only as a one way verifier. There is no command that shows it"
            " again, because no row holds it."
        ),
    )
    _add_merchant(minting)
    minting.add_argument(
        "--label",
        required=True,
        # Validated by argparse, using the same function the service applies, so a blank or
        # unprintable label is a usage error the operator can fix rather than a traceback. The
        # service checks it again: this is the message, that is the rule.
        type=validate_label,
        help="what this key is for, so a listing can be acted on later",
    )
    _add_json(minting)
    minting.set_defaults(command=create)

    listing = commands.add_parser(
        "list",
        help="every credential a merchant holds, revoked ones included",
        description=(
            "List one merchant's credentials, oldest first, revoked ones included. No secret"
            " appears: none is stored. Revoked keys are shown because the first question asked"
            " about a leak is whether the key was ever ours."
        ),
    )
    _add_merchant(listing)
    _add_json(listing)
    listing.set_defaults(command=index)

    withdrawal = commands.add_parser(
        "revoke",
        help="withdraw one credential. Terminal, and effective on the next request",
        description=(
            "Revoke one credential by identifier. Terminal: there is no command that restores"
            " one and the database refuses the update. Idempotent: revoking an already revoked"
            " credential reports that nothing changed and moves no timestamp."
        ),
    )
    withdrawal.add_argument(
        "credential_id", type=uuid.UUID, help="the credential identifier, from list"
    )
    _add_json(withdrawal)
    withdrawal.set_defaults(command=revoke)

    open_sessions = commands.add_parser(
        "sessions",
        help="the console browser sessions one merchant currently holds",
        description=(
            "List a merchant's open console sessions, oldest first. No verifier appears: none"
            " is stored, exactly as no key is. What is shown is when each session was opened,"
            " when it expires and which credential opened it, which is what an operator needs"
            " to decide whether to end them."
        ),
    )
    _add_merchant(open_sessions)
    _add_json(open_sessions)
    open_sessions.set_defaults(command=sessions)

    signing_out = commands.add_parser(
        "revoke-sessions",
        help="close every console session one merchant holds. Their API keys keep working",
        description=(
            "Revoke every open console session for one merchant. Distinct from revoking a"
            " credential: this ends browser access and leaves the merchant's integrations"
            " authenticating. Idempotent, and it reports how many sessions it closed."
        ),
    )
    _add_merchant(signing_out)
    _add_json(signing_out)
    signing_out.set_defaults(command=revoke_sessions)

    pruning = commands.add_parser(
        "purge-sessions",
        help="delete console sessions that stopped being usable long enough ago",
        description=(
            "Delete expired and revoked console sessions older than a cutoff. Bounded on both"
            " sides: the cutoff keeps a recently ended session readable while somebody is still"
            " working out why a merchant was signed out, and the limit keeps one invocation from"
            " being an unbounded delete. An open session is never touched, and nothing outside"
            " this table is deleted."
        ),
    )
    pruning.add_argument(
        "--older-than-days",
        type=int,
        default=DEFAULT_PURGE_DAYS,
        help=(
            "how long a session must have been settled to be deleted"
            f" (default {DEFAULT_PURGE_DAYS})"
        ),
    )
    pruning.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_PURGE_LIMIT,
        help=f"the most rows one invocation deletes (default {DEFAULT_PURGE_LIMIT})",
    )
    _add_json(pruning)
    pruning.set_defaults(command=purge_sessions)


def _add_merchant(parser: argparse.ArgumentParser) -> None:
    """Name a merchant, by identifier or by slug, and exactly one of the two.

    Two explicit flags rather than one argument that guesses. A slug is lowercase alphanumeric
    with dashes and so is the printed form of a UUID, so a positional argument accepting either
    would have to decide what `550e8400-e29b-41d4-a716-446655440000` means, and a provisioning
    command is the wrong place for a heuristic.

    The slug exists because it is what a developer has in hand after `make seed-dev`, and asking
    them to copy an identifier out of one command to paste into another is friction with no
    safety behind it. Authentication itself never touches a slug: the token carries a credential
    identifier and nothing else.
    """
    naming = parser.add_mutually_exclusive_group(required=True)
    naming.add_argument("--merchant-id", type=uuid.UUID, help="the merchant identifier")
    naming.add_argument("--merchant-slug", help="the merchant slug, as seeding prints it")


def _add_json(parser: argparse.ArgumentParser) -> None:
    """The one flag every command shares, declared once."""
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="print one JSON document instead of a table",
    )


async def _resolve_merchant(session: AsyncSession, arguments: argparse.Namespace) -> uuid.UUID:
    """Turn whichever of the two flags was given into a merchant identifier.

    A slug that names nobody is a `NotFoundError` naming the slug, so a typo is a clear
    refusal with exit code 3 rather than a command that quietly provisions nothing.
    """
    if arguments.merchant_id is not None:
        return uuid.UUID(str(arguments.merchant_id))

    merchant = await MerchantRepository(session).get_by_slug(arguments.merchant_slug)
    if merchant is None:
        raise NotFoundError("merchant", str(arguments.merchant_slug))
    return merchant.id


async def create(
    session: AsyncSession,
    sessions: async_sessionmaker[AsyncSession],
    provider: PaymentProvider,
    arguments: argparse.Namespace,
    out: TextIO,
    settings: Settings,
) -> int:
    """Mint one credential and print the token, once.

    The environment marker in the token comes from `settings.environment` rather than from a
    flag, so an operator cannot mint a key that claims to be from an environment it is not. It
    is a label either way and nothing verifies it, but a label a caller could choose would be a
    label worth nothing at all.
    """
    merchant_id = await _resolve_merchant(session, arguments)
    issued = await MerchantCredentialService(session).issue(
        merchant_id=merchant_id,
        label=arguments.label,
        marker=TokenMarker.of(settings.environment),
    )

    if arguments.as_json:
        write_json(
            out,
            {
                "credential_id": str(issued.credential.id),
                "merchant_id": str(merchant_id),
                "label": issued.credential.label,
                "created_at": issued.credential.created_at.isoformat(),
                "token": issued.token,
                "notice": ONCE_ONLY,
            },
        )
        return ExitCode.OK

    print(f"credential  {issued.credential.id}", file=out)
    print(f"merchant    {merchant_id}", file=out)
    print(f"label       {issued.credential.label}", file=out)
    print("", file=out)
    print(f"token       {issued.token}", file=out)
    print("", file=out)
    print(ONCE_ONLY, file=out)
    return ExitCode.OK


async def index(
    session: AsyncSession,
    sessions: async_sessionmaker[AsyncSession],
    provider: PaymentProvider,
    arguments: argparse.Namespace,
    out: TextIO,
    settings: Settings,
) -> int:
    """Every credential one merchant holds. Named `index` because `list` is a builtin."""
    merchant_id = await _resolve_merchant(session, arguments)
    credentials = await MerchantCredentialService(session).list_for_merchant(merchant_id)

    if arguments.as_json:
        write_json(
            out,
            {
                "merchant_id": str(merchant_id),
                "count": len(credentials),
                "credentials": [_credential_json(credential) for credential in credentials],
            },
        )
        return ExitCode.OK

    header = (
        f"{'CREDENTIAL':<{ID_WIDTH}}  {'STATUS':<{STATUS_WIDTH}}"
        f"  {'LABEL':<{LABEL_WIDTH}}  {'CREATED':<{STAMP_WIDTH}}  REVOKED"
    )
    print(header, file=out)
    for credential in credentials:
        print(
            f"{credential.id!s:<{ID_WIDTH}}"
            f"  {_status(credential):<{STATUS_WIDTH}}"
            f"  {credential.label[:LABEL_WIDTH]:<{LABEL_WIDTH}}"
            f"  {_stamp(credential.created_at):<{STAMP_WIDTH}}"
            f"  {_stamp(credential.revoked_at)}",
            file=out,
        )
    print(f"\n{len(credentials)} credential(s)", file=out)
    return ExitCode.OK


async def revoke(
    session: AsyncSession,
    sessions: async_sessionmaker[AsyncSession],
    provider: PaymentProvider,
    arguments: argparse.Namespace,
    out: TextIO,
    settings: Settings,
) -> int:
    """Withdraw one credential, and say whether this call is what withdrew it.

    A repeat is a zero, not a refusal. The operator asked for the credential to be unusable and
    it is unusable; that a previous run is what made it so is a fact worth printing rather than
    an error worth exiting on.
    """
    outcome = await MerchantCredentialService(session).revoke(arguments.credential_id)

    if arguments.as_json:
        write_json(
            out,
            _credential_json(outcome.credential)
            | {
                "merchant_id": str(outcome.credential.merchant_id),
                "changed": outcome.changed,
            },
        )
        return ExitCode.OK

    print(f"credential  {outcome.credential.id}", file=out)
    print(f"merchant    {outcome.credential.merchant_id}", file=out)
    print(f"label       {outcome.credential.label}", file=out)
    print(f"status      {_status(outcome.credential)}", file=out)
    print(f"revoked     {_stamp(outcome.credential.revoked_at)}", file=out)
    print(
        "changed     yes" if outcome.changed else "changed     no, it was already revoked",
        file=out,
    )
    return ExitCode.OK


def _credential_json(credential: MerchantApiCredential) -> dict[str, Any]:
    """One credential as plain values, and deliberately without a secret.

    There is nothing to omit here: the row does not hold one. Saying so in a comment matters
    because a reader checking whether this leaks a key should find the answer in one place.
    """
    return {
        "credential_id": str(credential.id),
        "label": credential.label,
        "status": _status(credential),
        "created_at": credential.created_at.isoformat(),
        "revoked_at": _iso(credential.revoked_at),
    }


def _status(credential: MerchantApiCredential) -> str:
    return ACTIVE if credential.is_active else REVOKED


def _stamp(moment: datetime | None) -> str:
    """A timestamp to the second in UTC, or a dash.

    Rendered rather than truncated. An ISO string with an offset is twenty five characters and
    a column that clipped it would print `22:31:17+`, which reads as a timezone that got cut in
    half and is worse than no offset at all. These columns are stored `timestamptz`, so
    converting to UTC and saying `Z` is exact rather than a simplification. The `--json` output
    carries the full offset for anything that has to parse it.
    """
    if moment is None:
        return MISSING
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%SZ")


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat()


async def sessions(
    session: AsyncSession,
    sessions_factory: async_sessionmaker[AsyncSession],
    provider: PaymentProvider,
    arguments: argparse.Namespace,
    out: TextIO,
    settings: Settings,
) -> int:
    """List one merchant's open console sessions.

    Open ones only, unlike the credential listing, and the difference is what each is for. A
    revoked credential answers "was this key ever ours", which is the first question asked about
    a leak. A settled session answers nothing anybody asks later, and listing every session a
    merchant has ever opened would bury the ones that are live.

    No verifier and no fragment of one. The row holds a digest, and printing a digest would be
    printing the one value that is worth a brute force attempt.
    """
    del sessions_factory, provider, settings
    merchant_id = await _resolve_merchant(session, arguments)
    open_sessions = list(
        (
            await session.execute(
                select(MerchantConsoleSession)
                .where(
                    MerchantConsoleSession.merchant_id == merchant_id,
                    MerchantConsoleSession.revoked_at.is_(None),
                    MerchantConsoleSession.expires_at > await _now(session),
                )
                .order_by(MerchantConsoleSession.created_at)
            )
        )
        .scalars()
        .all()
    )
    rows = [
        {
            "session_id": str(record.id),
            "credential_id": str(record.credential_id),
            "created_at": record.created_at.isoformat(),
            "expires_at": record.expires_at.isoformat(),
        }
        for record in open_sessions
    ]
    if arguments.as_json:
        write_json(out, {"merchant_id": str(merchant_id), "sessions": rows})
        return ExitCode.OK

    if not rows:
        print("no open console sessions", file=out)
        return ExitCode.OK
    print(
        f"{'session':<{ID_WIDTH}}  {'credential':<{ID_WIDTH}}  {'opened':<{STAMP_WIDTH}}  expires",
        file=out,
    )
    for row in rows:
        opened = row["created_at"][:STAMP_WIDTH]
        expires = row["expires_at"][:STAMP_WIDTH]
        print(
            f"{row['session_id']:<{ID_WIDTH}}  {row['credential_id']:<{ID_WIDTH}}"
            f"  {opened:<{STAMP_WIDTH}}  {expires}",
            file=out,
        )
    return ExitCode.OK


async def revoke_sessions(
    session: AsyncSession,
    sessions_factory: async_sessionmaker[AsyncSession],
    provider: PaymentProvider,
    arguments: argparse.Namespace,
    out: TextIO,
    settings: Settings,
) -> int:
    """Close every open console session one merchant holds, and say how many that was."""
    del sessions_factory, provider, settings
    merchant_id = await _resolve_merchant(session, arguments)
    closed = await ConsoleSessionService(session).revoke_for_merchant(merchant_id)
    if arguments.as_json:
        write_json(out, {"merchant_id": str(merchant_id), "revoked": closed})
    else:
        print(f"revoked     {closed} console session(s)", file=out)
    return ExitCode.OK


async def purge_sessions(
    session: AsyncSession,
    sessions_factory: async_sessionmaker[AsyncSession],
    provider: PaymentProvider,
    arguments: argparse.Namespace,
    out: TextIO,
    settings: Settings,
) -> int:
    """Delete settled console sessions older than the cutoff, up to the limit.

    Both bounds are refused rather than clamped when they are nonsense. A negative cutoff would
    delete sessions that are still open by arithmetic rather than by intent, and a limit of zero
    is a command that does nothing while reporting success.
    """
    del sessions_factory, provider, settings
    if arguments.older_than_days < 0 or arguments.limit < 1:
        print("--older-than-days must not be negative and --limit must be at least 1", file=out)
        return ExitCode.USAGE
    removed = await ConsoleSessionService(session).purge_settled(
        older_than=timedelta(days=arguments.older_than_days), limit=arguments.limit
    )
    if arguments.as_json:
        write_json(out, {"deleted": removed, "limit": arguments.limit})
    else:
        print(f"deleted     {removed} settled console session(s)", file=out)
    return ExitCode.OK


async def _now(session: AsyncSession) -> datetime:
    """The database clock, so a listing cannot call a session open that the API would refuse."""
    return await ConsoleSessionService(session).clock()
