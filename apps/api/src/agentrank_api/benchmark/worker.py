"""The process an untrusted buyer runs in, and the only thing it is given.

Run as `python -m agentrank_api.benchmark.worker`. It reads one `MissionRequest` from stdin,
carries the mission out against the merchant's commerce API over HTTP, writes one report to
stdout, and exits. One mission per process, so nothing an executor learns can survive into the
next one and no counter it keeps can tell it which mission it is on.

What it has is a brief, a merchant identifier, a base URL and one merchant credential. What it
does not have is a database. No `DATABASE_URL` reaches it, so `create_engine` has nothing to
build from, and it holds no session, no repository and no run. There is no benchmark route on the
API it can reach, so there is no request it can make that touches a suite, a run or another
mission's result.

What that does not do, and an earlier version of this docstring wrongly said it did, is put the
authored suites out of reach. They are Python in the package this worker runs from, so code
running here can import `agentrank_api.benchmark.voltedge` and read every mission's expected
outcome, indexed by the mission key it was just handed. An independent test audit proved it by
doing it, in exactly this environment and this working directory. `PYTHONPATH` has been taken off
the allowlist since, which removes one route and not the one that matters.

The boundary is therefore honest about two different things. A database credential, a session and
every other run's results are absent from this process. The authored ground truth is not, and
closing that means the suites living somewhere the runtime package does not, which is written
down in docs/shortcomings.md as the first thing Phase 2C has to do. It is worth being precise
about who this is a boundary against in the meantime: the code here is this repository's, and the
untrusted thing behind it is a model that is handed a brief and a tool schema and never a Python
interpreter.

That is checked rather than assumed, twice and in two different ways.
`require_isolated_environment` refuses any variable that is not on a short allowlist, which is
the same allowlist the runner builds the environment from: the runner cannot pass a secret in by
accident, and a worker started by hand in a shell that has one exits rather than running.
`require_no_database_configuration` then checks the outcome rather than the inputs, because the
environment is not the only way in. `Settings` reads a `.env` file from the working directory, so
a worker started in a checkout picks up the developer's `POSTGRES_PASSWORD` from disk with an
empty environment. The runner starts a worker in an empty directory of its own, and this refuses
to run if that ever stops being true.

The report is an `ObservedResult` and nothing else. There is no field for a status, no field for
a failure reason and no field for an error origin, so this process cannot mark its own mission
and cannot say whose fault an interruption was. If the mission cannot be carried out at all, this
exits non zero and says nothing, and the trusted side attributes that from the exit code.

Standard output carries the report and nothing else. Anything this process wants to say to a
human goes to standard error, because a line of diagnostic prose on stdout is a protocol
violation and the runner treats it as one.
"""

import asyncio
import json
import os
import sys
from collections.abc import Mapping
from typing import TextIO

from agentrank_api.benchmark.http_buyer import HttpBuyerCommerceSurface, authenticated_client
from agentrank_api.benchmark.observation import ObservedResult
from agentrank_api.benchmark.reference_executor import ReferenceMissionExecutor
from agentrank_api.benchmark.wire import (
    REFERENCE_STRATEGY,
    MissionRequest,
    ProtocolError,
    report_payload,
)
from agentrank_api.config import get_settings

# Exactly the variables a buyer process needs, and this is the runner's side of the rule as well.
# An allowlist rather than a denylist, because a denylist is blind to whatever is added next and
# a secret added to the deployment after this was written is exactly the one that would leak.
#
# None of these is a credential. `PATH` and `HOME` are what an interpreter needs to run at all,
# the locale variables keep text handling identical to the parent, `TMPDIR` keeps temporary files
# where the parent puts them, and the certificate variables are what a TLS connection to a real
# merchant would need.
#
# `PYTHONPATH` was on this list and is not any more. It carries no secret, which is why it was
# there, and it decides what a process can import, which is why it should not have been: a
# checkout on it makes anything in the repository importable from a process whose whole point is
# that it has been given a brief and nothing else. The package this worker runs from is installed
# in the environment, so nothing needs it.
PERMITTED_ENVIRONMENT = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TMPDIR",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
)


class EnvironmentNotIsolatedError(RuntimeError):
    """This process can see something a buyer must never be given.

    Raised before any mission is read, so a worker started in a shell that has the developer's
    database URL in it exits rather than running with one. The message names the variables and
    never their values: a refusal that printed a connection string would be the leak it exists
    to prevent.
    """

    def __init__(self, names: frozenset[str]) -> None:
        listed = ", ".join(sorted(names))
        super().__init__(
            f"a benchmark executor process must not be able to see {listed}."
            " It is given a merchant API credential and nothing else"
        )
        self.names = names


def require_isolated_environment(environment: Mapping[str, str]) -> None:
    """Refuse to run in a process that can reach anything a buyer should not have.

    The check is the complement of the allowlist rather than a list of known secrets, so a
    variable nobody thought of is refused by default. That is the whole reason it is written
    this way: the leak worth preventing is the one added to the deployment next year.
    """
    intruders = frozenset(name for name in environment if name not in PERMITTED_ENVIRONMENT)
    if intruders:
        raise EnvironmentNotIsolatedError(intruders)


def require_no_database_configuration() -> None:
    """Refuse to run in a process that could configure a database, however it got the values.

    The environment allowlist is not enough on its own and this is why. `Settings` reads a `.env`
    file from the working directory, so a worker started in a checkout picks up the developer's
    `POSTGRES_PASSWORD` from disk with an environment that contains nothing at all. That was
    found by a test asserting the opposite of what the code did.

    So the check is on the outcome rather than on the inputs: if this process can build settings,
    it can build an engine, and it is not isolated. The runner also starts a worker in an empty
    directory of its own, which is what makes this pass; the two are independent and both are
    meant to hold.
    """
    try:
        get_settings()
    except Exception:  # the settings could not be built, which is the whole point
        return
    raise EnvironmentNotIsolatedError(frozenset({"a readable settings file"}))


def worker_environment(parent: Mapping[str, str]) -> dict[str, str]:
    """What the runner hands a worker process, built by allowlist from its own environment.

    A pure function so that what a worker will be able to see is assertable without starting
    one. The runner uses it and a test reads it, which is the only way this rule can be checked
    other than by reading a process's `/proc` entry from the outside.
    """
    return {name: value for name, value in parent.items() if name in PERMITTED_ENVIRONMENT}


async def execute(request: MissionRequest) -> ObservedResult:
    """Carry out one mission over the merchant's own commerce API.

    The strategy is named by the request and refused if it is not one this build has, because a
    worker that quietly ran a different buyer than the run's executor identity claims would make
    every historical comparison wrong in a way nothing could detect.
    """
    if request.strategy != REFERENCE_STRATEGY:
        raise ProtocolError(f"unknown buyer strategy {request.strategy!r}")

    client = authenticated_client(request.base_url, request.token)
    surface = HttpBuyerCommerceSurface(client, merchant_id=request.merchant_id)
    async with client:
        return await ReferenceMissionExecutor(surface)(
            request.brief, merchant_id=request.merchant_id
        )


def read_request(source: TextIO) -> MissionRequest:
    """The one document this process is given, or a refusal that it was not."""
    raw = source.read()
    if not raw.strip():
        raise ProtocolError("a benchmark worker is given one mission request on standard input")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as malformed:
        raise ProtocolError("the mission request was not JSON") from malformed
    return MissionRequest.from_payload(payload)


def main(
    argv: list[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Read one mission, carry it out, write one report.

    Every stream is injectable so that a test can drive this without a subprocess, and a test
    that drives it with a subprocess is asserting something different and does both.

    A mission that could not be carried out at all exits non zero with nothing on stdout. That
    is deliberate: this process is not trusted to say what went wrong, so it says nothing, and
    the trusted side attributes the failure from the exit code and the silence.
    """
    del argv
    errors = sys.stderr if stderr is None else stderr
    try:
        require_isolated_environment(os.environ if environment is None else environment)
        require_no_database_configuration()
        request = read_request(sys.stdin if stdin is None else stdin)
        observed = asyncio.run(execute(request))
    except EnvironmentNotIsolatedError as leaked:
        print(f"refused: {leaked}", file=errors)
        return 2
    except ProtocolError as malformed:
        print(f"refused: {malformed}", file=errors)
        return 3
    except Exception as failed:  # the exit code is the report, not this
        # The type and nothing else. A traceback from a buyer process can carry a request body,
        # and a request body carries the credential this process was given.
        print(f"failed: {type(failed).__name__}", file=errors)
        return 4

    json.dump(report_payload(observed), sys.stdout if stdout is None else stdout)
    return 0


if __name__ == "__main__":  # pragma: no cover  exercised through a real subprocess
    raise SystemExit(main())
