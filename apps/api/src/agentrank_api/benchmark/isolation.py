"""Running one mission in a process that has no database, and judging what came back.

This is the trusted half of the isolated boundary. It spawns a worker, hands it one mission on
standard input, reads one report from standard output, and decides from evidence it gathered
itself what happened. The worker is not trusted with any of that: it cannot mark its own mission,
cannot say whose fault an interruption was, and cannot tell this object anything except an
`ObservedResult`.

Why a process rather than an object. An in process surface holds a session factory, and anything
holding the surface can reach it with one attribute access, so every in process arrangement of
private names is a convention rather than a boundary. Python has no way to make it otherwise.
A process has one: the worker has no `DATABASE_URL`, so there is nothing for a session to be
built from, and the environment it is given is an allowlist rather than a filtered copy of ours.

What the worker gets:

```text
stdin      one MissionRequest: a brief, a merchant, a base URL and one merchant credential
env        PATH, HOME, the locale, TMPDIR, PYTHONPATH and the certificate paths
cwd        an empty directory of its own, so no `.env` is readable from it
```

The working directory is not a detail. `Settings` reads a `.env` file relative to it, so a worker
started in a checkout would read the developer's `POSTGRES_PASSWORD` off disk with an environment
containing nothing but `PATH`. That was found by a test asserting the opposite of what the code
did, and it is the reason the worker also refuses to run if it can build settings at all.

What it does not get: a database URL, a payment provider secret, this application's settings, the
suite, the run, any other mission, the expected outcome, the simulated value, or anything about
what an earlier mission did. One process per mission, so nothing it learns survives into the next
one and no counter it keeps can tell it where in the suite it is.

Attribution comes from three trusted places and none of them is the worker:

```text
the process     it could not be started, it exited non zero, it timed out, it spoke nonsense
the server      a 5xx it answered, and whether the payment route was ever reached
the report      an ObservedResult, and only ever as the observation itself
```

A worker that dies is a HARNESS fault and the mission is ERRORED. That is not a way to be
excused: the completion rate's denominator is the suite rather than what ran, so a buyer that
crashes on every mission it cannot solve scores zero rather than nothing, and `missions_errored`
is reported beside every rate.

A worker that dies after the payment route was reached is different and is refused rather than
recorded. Money may have moved, and a mission whose payment is unknown must never be replayed or
tidied away. The run stops with that mission RUNNING, which is exactly the state an operator
resolves through `agentrank_api.cli payments`.
"""

import asyncio
import json
import os
import sys
import tempfile
import uuid

from agentrank_api.benchmark.definitions import AgentMissionBrief
from agentrank_api.benchmark.endpoint import RequestLedger
from agentrank_api.benchmark.execution import ExecutorIdentity
from agentrank_api.benchmark.faults import ExecutionFault, FaultOrigin
from agentrank_api.benchmark.observation import ObservedResult
from agentrank_api.benchmark.wire import (
    REFERENCE_STRATEGY,
    MissionRequest,
    ProtocolError,
    report_from_payload,
)
from agentrank_api.benchmark.worker import worker_environment

WORKER_MODULE = "agentrank_api.benchmark.worker"

# Which strategy in the worker corresponds to which recorded identity. A run says what did the
# shopping, and a worker running one buyer while the run recorded another would make every
# historical comparison wrong with nothing on either to show it.
ISOLATED_REFERENCE = ExecutorIdentity(kind="reference-isolated", version=1)

# How long one mission may take end to end. Generous enough that a real payment through the real
# kernel on a loaded machine finishes, short enough that a worker which has stopped answering is
# reported rather than waited on forever.
DEFAULT_TIMEOUT = 120.0

# How long a killed worker is given to actually die before this stops waiting for it. A process
# that ignores SIGKILL is not something this can do anything about, and blocking on one would
# turn a bounded fault into a hang.
REAPING_TIMEOUT = 10.0


class PaymentUnaccountedError(RuntimeError):
    """A worker reached the payment route and did not come back with a report.

    Raised rather than recorded, and it stops the run. Money may have moved and nothing knows
    whether it did, so the mission stays RUNNING and is never replayed: re-executing it is how a
    benchmark buys the same thing twice. An operator reads the run, resolves the payment through
    `agentrank_api.cli payments`, and closes the run with `benchmark abort`.
    """

    def __init__(self, mission_key: str, detail: str) -> None:
        super().__init__(
            f"mission {mission_key} dispatched a payment and its executor did not report:"
            f" {detail}. The payment must be resolved before this run is closed"
        )
        self.mission_key = mission_key
        self.detail = detail


class IsolatedMissionExecutor:
    """A buyer in another process, and the trusted witness for what that process did.

    Both roles on one object on purpose. The witness has to be the thing that watched, and what
    watched is this: it started the process, it knows how the process ended, and it holds the
    server side record of every request that process made. A separate witness would have to be
    told, and being told is what this boundary exists to stop.
    """

    identity = ISOLATED_REFERENCE

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        served: RequestLedger,
        strategy: str = REFERENCE_STRATEGY,
        timeout: float = DEFAULT_TIMEOUT,
        environment: dict[str, str] | None = None,
        interpreter: str | None = None,
    ) -> None:
        self._base_url = base_url
        self._token = token
        self._served = served
        self._strategy = strategy
        self._timeout = timeout
        self._environment = environment
        self._interpreter = sys.executable if interpreter is None else interpreter
        self._fault: ExecutionFault | None = None

    # The witness half.

    def begin(self) -> None:
        """Forget the previous mission, on both records at once.

        One call rather than two, because a witness whose two halves can be reset separately is
        a witness that can be half stale, and the stale half would be the one attributing a
        fault to the wrong mission.
        """
        self._fault = None
        self._served.begin()

    def fault(self) -> ExecutionFault | None:
        """What went wrong, decided from the process and the server and never from the report.

        The process comes first. A worker that died, timed out or spoke nonsense could not have
        carried the mission out whatever the server saw, and that is the harness's failure to
        run its own executor rather than a fact about the merchant.
        """
        if self._fault is not None:
            return self._fault
        failure = self._served.first_failure()
        if failure is None:
            return None
        return ExecutionFault(
            origin=FaultOrigin.MERCHANT,
            detail=f"the merchant answered {failure.status}",
            operation=failure.path,
        )

    def payment_attempted(self) -> bool:
        return self._served.payment_attempted()

    # The executor half.

    async def __call__(self, brief: AgentMissionBrief, *, merchant_id: uuid.UUID) -> ObservedResult:
        """Carry one mission out in a process that has no database, and read what came back.

        Anything that goes wrong with the process is recorded as a harness fault and an empty
        report, so one crashed mission does not end a run: the mission is marked ERRORED, which
        lowers the completion rate rather than removing the mission from its denominator.

        The one exception is a worker that reached the payment route and did not report. That
        raises, because money may have moved and a mission whose payment is unknown must not be
        recorded, retried or tidied away.
        """
        request = MissionRequest(
            brief=brief,
            merchant_id=merchant_id,
            base_url=self._base_url,
            token=self._token,
            strategy=self._strategy,
        )
        try:
            return await self._carry_out(request)
        except PaymentUnaccountedError:
            raise
        except _WorkerFailureError as failed:
            self._fault = ExecutionFault(origin=FaultOrigin.HARNESS, detail=failed.detail)
            if self.payment_attempted():
                raise PaymentUnaccountedError(brief.key, failed.detail) from failed
            return ObservedResult(merchant_id=merchant_id)

    async def _carry_out(self, request: MissionRequest) -> ObservedResult:
        # An empty directory of its own, and this is load bearing rather than tidy. `Settings`
        # reads a `.env` file from the working directory, so a worker started in a checkout
        # picks up the developer's `POSTGRES_PASSWORD` from disk however carefully its
        # environment was built. Found by a test that asserted the opposite of what this did.
        with tempfile.TemporaryDirectory(prefix="agentrank-benchmark-") as sandbox:
            return await self._through(request, sandbox)

    async def _through(self, request: MissionRequest, sandbox: str) -> ObservedResult:
        process = await self._spawn(sandbox)
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(json.dumps(request.to_payload()).encode("utf-8")),
                timeout=self._timeout,
            )
        except TimeoutError as expired:
            await self._reap(process)
            raise _WorkerFailureError(
                f"the executor process did not finish in {self._timeout}s"
            ) from (expired)

        if process.returncode != 0:
            # The worker's own words, not its classification. It has no way to say whose fault
            # anything was and this does not read one out of the text: the exit code is what is
            # believed and the text is only for a person reading a failure.
            raise _WorkerFailureError(
                f"the executor process exited {process.returncode}: {_said(stderr)}"
            )
        try:
            return report_from_payload(json.loads(stdout.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError) as unreadable:
            raise _WorkerFailureError("the executor process did not report JSON") from unreadable
        except (ProtocolError, ValueError) as malformed:
            raise _WorkerFailureError(f"the executor process reported {malformed}") from malformed

    async def _spawn(self, sandbox: str) -> asyncio.subprocess.Process:
        """Start the worker with an environment built by allowlist and a directory of its own.

        `env` is passed rather than inherited, which is half the mechanism. A filtered copy of
        this process's environment would leak whatever nobody thought to filter; an allowlist
        leaks nothing that was not deliberately placed on it.

        `cwd` is the other half, and it was not there first. `Settings` reads a `.env` file from
        the working directory, so a worker started in a checkout reads the developer's database
        password off disk with an environment containing nothing but `PATH`. An empty directory
        has no `.env` in it. The worker refuses to run if it can build settings anyway, so the
        two checks are independent and both are meant to hold.
        """
        try:
            return await asyncio.create_subprocess_exec(
                self._interpreter,
                "-m",
                WORKER_MODULE,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.environment,
                cwd=sandbox,
            )
        except OSError as unstartable:
            raise _WorkerFailureError(
                f"the executor process could not be started: {unstartable}"
            ) from (unstartable)

    @property
    def environment(self) -> dict[str, str]:
        """Exactly what a worker this executor starts will be able to see.

        A property rather than a private field so that a test can assert it without starting a
        process, and so that the assertion is about the thing actually used rather than about a
        copy of the rule.
        """
        return worker_environment(os.environ if self._environment is None else self._environment)

    async def _reap(self, process: asyncio.subprocess.Process) -> None:
        """Make sure a worker that timed out is gone before anything else runs.

        A worker left alive still holds a credential and can still reach the merchant, so the
        next mission would be sharing its world with an executor nobody is reading. Killed
        rather than terminated, because a process that stopped answering is a process that may
        not handle a signal either.
        """
        if process.returncode is not None:
            return
        process.kill()
        try:
            await asyncio.wait_for(process.wait(), timeout=REAPING_TIMEOUT)
        except TimeoutError:
            # Nothing further is available. Reported through the fault the caller is already
            # raising rather than swallowed silently.
            return


class _WorkerFailureError(RuntimeError):
    """Something went wrong with the process rather than inside the mission."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def _said(stderr: bytes) -> str:
    """Whatever the worker wrote for a human, bounded and decoded safely.

    Bounded because a worker is not trusted to be brief, and a failure message that carried a
    megabyte of output would be one nobody reads. Decoded with replacement because a process
    that is failing is a process that may also be writing broken text.
    """
    text = stderr.decode("utf-8", errors="replace").strip()
    return text[:500] if text else "it said nothing"
