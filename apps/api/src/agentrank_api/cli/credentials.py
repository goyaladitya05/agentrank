"""The merchant credential operator commands, and why they are commands rather than routes.

Provisioning is the one capability that must never be reachable over the network. An endpoint
that issued a merchant API key would be an endpoint that could issue one for any merchant, and
nothing could authenticate the caller asking for it, because a credential is the thing that
makes authentication possible in the first place. That is a bootstrapping problem with exactly
one honest answer: the first credential comes from somebody with a shell.

So there are three commands, all local, all delegating to `MerchantCredentialService`:

```text
create   mints a key and prints it once      the only place a secret ever exists
list     shows what a merchant holds         never shows a secret, because none is stored
revoke   withdraws one key, terminally       takes effect on the next request
```

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
from datetime import UTC, datetime
from typing import Any, TextIO

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentrank_api.auth.models import MerchantApiCredential
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
