"""The operator command line, and the reason it is a command line rather than an endpoint.

Payment recovery needs a surface. Reconciliation exists, abandonment exists, an unresolved
payment can be listed, and until now none of it could be reached by anybody who was not
willing to open a Python REPL and write a session by hand. That is not a recovery path; it is
a recovery path's ingredients.

The surface is this, and specifically not HTTP. Nothing in this application authenticates
anybody yet. An endpoint that terminalized a payment or released a merchant's stock would be
an unauthenticated way to do exactly that, which is strictly worse than the stuck payment it
recovers from. A command line moves the trust boundary onto something that already exists: to
run these commands you must already be able to run this repository's code against this
repository's database, and anybody who can do that could write the session by hand anyway.

```text
what the CLI can do            what it deliberately cannot
------------------------------ ------------------------------
list unresolved payments       select a provider
inspect one payment            set a status
query a provider               release a reservation on its own
dispatch an admitted payment   rewrite a terminal outcome
abandon an unresolved payment  show a secret it already printed
issue a merchant API key       do any of it over the network
revoke a merchant API key
```

Every command delegates. There is no SQL here, no lock, no transaction and no rule about what
a payment may do next: those live in `agentrank_api.payments` and a second copy of any of them
in a command would be a second answer to a question that must have one. What is here is
argument parsing, one call into a service, and printing.

The commands are grouped under `payments` because operator tooling will eventually cover more
than payments, and a flat command list is a thing that is easy to add to and impossible to
read later.

Run it through the repository environment:

```bash
uv run python -m agentrank_api.cli payments list-unresolved
uv run python -m agentrank_api.cli payments show <attempt-id>
uv run python -m agentrank_api.cli payments reconcile <attempt-id>
uv run python -m agentrank_api.cli payments reconcile-unresolved --limit 20
uv run python -m agentrank_api.cli payments resume <attempt-id>
uv run python -m agentrank_api.cli payments abandon <attempt-id> --reason provider_unreachable
uv run python -m agentrank_api.cli payments status
uv run python -m agentrank_api.cli credentials create --merchant-slug ampere-supply --label local
uv run python -m agentrank_api.cli credentials list --merchant-slug ampere-supply
uv run python -m agentrank_api.cli credentials revoke <credential-id>
uv run python -m agentrank_api.cli benchmark seed
uv run python -m agentrank_api.cli benchmark run --representation-label baseline
uv run python -m agentrank_api.cli benchmark show <run-id>
uv run python -m agentrank_api.cli benchmark abort <run-id>
uv run python -m agentrank_api.cli compiler run --merchant-slug voltedge --source-id <source-id>
uv run python -m agentrank_api.cli compiler show --merchant-slug voltedge <run-id>
uv run python -m agentrank_api.cli compiler review --merchant-slug voltedge <candidate-id> accept
uv run python -m agentrank_api.cli compiler publish --merchant-slug voltedge <run-id>
```

Exit codes are meant to be acted on by a script as well as read by a person:

```text
0  the command ran and reported what it found
1  an unexpected internal failure, with the traceback, because this is a trusted tool
2  the arguments were wrong, including an authored benchmark world that cannot be read
3  the payment, merchant, credential or benchmark run named does not exist
4  the current state refuses the operation
```

A payment that is still UNKNOWN after a successful reconciliation is a zero. The command did
what it was asked, the provider answered, and "nobody knows yet" is a finding rather than a
failure. Only a refusal, a missing payment, bad arguments or a crash are non zero.

Operator identity is still not recorded, because there is still nothing to record. Audit events
written through these commands are attributed to a role, exactly as they are everywhere else in
this system, and the role is honest: `SYSTEM` for an abandonment and for credential provisioning,
because this application acted, and `PAYMENT_PROVIDER` for an outcome, because a provider
reported it. Nothing here reads a Unix username and calls it authentication.

Phase 1H did not change that, and it is worth being exact about why rather than leaving it
looking like an oversight. What that phase built is merchant authentication: a credential proves
which merchant an HTTP request acts for, and HTTP events now record which credential authorized
them. An operator is not a merchant and holds no credential, so there is nothing for these
commands to record that would be more than a guess. Operator identity remains open. See
docs/security.md.
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from typing import TextIO

from agentrank_api.benchmark.authored import AuthoredDefinitionError
from agentrank_api.cli import benchmark, compiler, credentials, payments, representation
from agentrank_api.cli.command import Command
from agentrank_api.cli.exits import ExitCode
from agentrank_api.config import Settings, get_settings
from agentrank_api.database import create_engine, create_session_factory
from agentrank_api.errors import ConflictError, NotFoundError
from agentrank_api.payments.provider import PaymentProvider
from agentrank_api.payments.wiring import build_payment_provider

PROGRAM = "agentrank"


def build_parser() -> argparse.ArgumentParser:
    """The whole command surface, declared in one place.

    argparse rather than a framework. There are ten commands, none of them has a nested
    option group, and a dependency added for this would be a dependency in the deployment
    artifact for the sake of coloured help text.

    Three groups. `credentials` provisions merchant API keys, which is deliberately not an HTTP
    surface: an endpoint that issued one could issue it for anybody, and nothing could
    authenticate the caller asking, because a credential is what makes authentication possible.
    `benchmark` prepares a world and executes a suite, which overwrites a merchant's catalog and
    consumes its stock, and neither is something a network surface should be able to start.
    """
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description="AgentRank operator tooling. Trusted local surface, no authentication.",
    )
    groups = parser.add_subparsers(dest="group", required=True)
    payments.add_commands(groups.add_parser("payments", help="payment operations and recovery"))
    credentials.add_commands(groups.add_parser("credentials", help="merchant API key provisioning"))
    benchmark.add_commands(
        groups.add_parser("benchmark", help="benchmark worlds, runs and results")
    )
    representation.add_commands(
        groups.add_parser("representation", help="merchant source and Commerce IR artifacts")
    )
    compiler.add_commands(
        groups.add_parser("compiler", help="merchant compiler and review workflow")
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
    provider: PaymentProvider | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Parse, run one command and report.

    Every collaborator is injectable and none of them is discovered. Settings and the provider
    are parameters so that a test runs the real commands against a real database and a
    configured fake, which is the only way a recovery path gets tested at all: the interesting
    cases are a provider that times out, a provider with no record and a provider that
    guarantees absence, and none of those can be asked for from the outside.

    Injectable is not selectable. There is no flag, no environment variable and no argument
    that chooses a provider, so an operator cannot point this at something the application is
    not running with. The default comes from `build_payment_provider`, the same function
    `create_app` uses.

    The two deliberate errors are caught here rather than in each command, for the same reason
    the application installs exception handlers rather than catching in routes: a refusal means
    the same thing whichever command produced it. Everything else propagates.
    """
    parser = build_parser()
    arguments = parser.parse_args(argv)
    stream = sys.stdout if out is None else out
    errors = sys.stderr if err is None else err

    try:
        return asyncio.run(
            _run(
                arguments,
                settings=get_settings() if settings is None else settings,
                provider=build_payment_provider() if provider is None else provider,
                out=stream,
            )
        )
    except AuthoredDefinitionError as unreadable:
        # An authored world is an argument rather than an internal fact: the operator named the
        # directory, and a file that is missing or malformed is something they can fix. It is a
        # usage exit for the same reason a bad flag is, and it says which file and why.
        print(f"unusable benchmark world: {unreadable}", file=errors)
        return ExitCode.USAGE
    except NotFoundError as missing:
        print(f"not found: {missing}", file=errors)
        return ExitCode.NOT_FOUND
    except ConflictError as refused:
        print(f"refused: {refused.reason}: {refused.detail}", file=errors)
        return ExitCode.REFUSED


async def _run(
    arguments: argparse.Namespace,
    *,
    settings: Settings,
    provider: PaymentProvider,
    out: TextIO,
) -> int:
    """Open one engine and one session for one command, and close both.

    A command is a short lived process, so the engine is built and disposed around it rather
    than kept. The session is the same object a route would hand a service, which is what makes
    the command path the real path: the transaction boundaries, the locks and the commits are
    the service's, exactly as they are over HTTP.

    The factory is handed down beside it rather than kept private, so that a command needing a
    second independent session gets one on this engine instead of building an engine of its own.
    One command needs that and the rest ignore it.
    """
    engine = create_engine(settings)
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            command: Command = arguments.command
            return await command(session, factory, provider, arguments, out, settings)
    finally:
        await engine.dispose()


__all__ = ["Command", "ExitCode", "build_parser", "main"]
