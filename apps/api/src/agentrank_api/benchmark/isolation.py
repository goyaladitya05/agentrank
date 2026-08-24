"""Running one mission in a process that has no database, and judging what came back.

This is the trusted half of the isolated boundary. It spawns a worker, hands it one mission on
standard input, reads one report from standard output, and decides from evidence it gathered
itself what happened. The worker is not trusted with any of that: it cannot mark its own mission,
cannot say whose fault an interruption was, and cannot tell this object anything except an
`ExecutorReport`.

Why a process rather than an object. An in process surface holds a session factory, and anything
holding the surface can reach it with one attribute access, so every in process arrangement of
private names is a convention rather than a boundary. Python has no way to make it otherwise.
A process has one: the worker has no `DATABASE_URL`, so there is nothing for a session to be
built from, and the environment it is given is an allowlist rather than a filtered copy of ours.

What the worker gets:

```text
stdin      one MissionRequest: a brief, a merchant, a base URL and one merchant credential
env        PATH, HOME, the locale, TMPDIR and the certificate paths
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
the server      a 5xx it answered, whether the payment route was reached, and what the
                authorization layer answered where no row records it
the report      an ExecutorReport: identifiers and actions, never facts
```

A worker that dies is the buyer failing and the mission is FAILED, which is the correction Phase
2B-R2 made. It used to be a HARNESS fault and therefore ERRORED, the one status that carries no
failure reason and moves a mission's authored value out of demand the merchant lost. A model that
crashed whenever it could not solve something would have been excused from every one of them. The
harness's own failures, which are a process that could not be started at all and a worker that
refused the request this side wrote, are still ERRORED and are still reported beside every rate.

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
from collections.abc import Awaitable, Callable

from agentrank_api.benchmark.agent_trace import AgentExecutionEvidence
from agentrank_api.benchmark.definitions import AgentMissionBrief
from agentrank_api.benchmark.endpoint import DATABASE_UNAVAILABLE, RequestLedger
from agentrank_api.benchmark.evidence import CommerceEvidence
from agentrank_api.benchmark.execution import (
    REFERENCE_ISOLATED_KIND,
    ExecutorIdentity,
    implementation_revision,
)
from agentrank_api.benchmark.faults import ExecutionFault, FaultOrigin
from agentrank_api.benchmark.llm import GEMINI_PROVIDER, OPENAI_PROVIDER
from agentrank_api.benchmark.report import ExecutorReport
from agentrank_api.benchmark.wire import (
    REFERENCE_STRATEGY,
    MissionRequest,
    ProtocolError,
    agent_evidence_from_payload,
    report_from_payload,
)
from agentrank_api.benchmark.worker import (
    EXIT_NOT_ISOLATED,
    EXIT_PROTOCOL,
    worker_environment,
)
from agentrank_api.config import Settings

WORKER_MODULE = "agentrank_api.benchmark.worker"


# Which strategy in the worker corresponds to which recorded identity. A run says what did the
# shopping, and a worker running one buyer while the run recorded another would make every
# historical comparison wrong with nothing on either to show it.
def _revision() -> str:
    """Everything that decides what the isolated buyer does, digested together.

    Three modules rather than one. The scripted buyer decides what to select, the HTTP surface
    decides what it can see and how a refusal reaches it, and the worker decides what the
    process does with either. A run produced by an edit to any of them is a run produced by
    different code, and none of the three would move a declared version on its own.
    """
    return implementation_revision(
        sys.modules["agentrank_api.benchmark.reference_executor"],
        sys.modules["agentrank_api.benchmark.http_buyer"],
        sys.modules["agentrank_api.benchmark.worker"],
    )


def provider_worker_environment(settings: Settings, provider: str) -> dict[str, str]:
    """The environment one model buyer process is given, with exactly one provider credential.

    Built through the same allowlist the worker refuses to run without, and then narrowed once
    more: both provider variables are removed before the configured one is put back, so a worker
    running an OpenAI sample cannot see a Gemini key that happened to be in this process. The
    value is unwrapped here and nowhere else, and it crosses no argument vector, no standard
    input and no log line.
    """
    environment = worker_environment(os.environ)
    environment.pop("OPENAI_API_KEY", None)
    environment.pop("GEMINI_API_KEY", None)
    if provider == OPENAI_PROVIDER:
        if settings.openai is None:
            raise ValueError("an OpenAI buyer needs an OpenAI runtime credential")
        environment["OPENAI_API_KEY"] = settings.openai.api_key.get_secret_value()
        return environment
    if provider == GEMINI_PROVIDER:
        if settings.gemini is None:
            raise ValueError("a Gemini buyer needs a Gemini runtime credential")
        environment["GEMINI_API_KEY"] = settings.gemini.api_key.get_secret_value()
        return environment
    raise ValueError("LLM provider is not supported")


ISOLATED_REFERENCE = ExecutorIdentity(kind=REFERENCE_ISOLATED_KIND, version=1, revision=_revision())

# How long one mission may take end to end. Generous enough that a real payment through the real
# kernel on a loaded machine finishes, short enough that a worker which has stopped answering is
# reported rather than waited on forever.
DEFAULT_TIMEOUT = 120.0

# How long a killed worker is given to actually die before this stops waiting for it. A process
# that ignores SIGKILL is not something this can do anything about, and blocking on one would
# turn a bounded fault into a hang.
REAPING_TIMEOUT = 10.0


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
        provision_mandate: Callable[[AgentMissionBrief], Awaitable[uuid.UUID]] | None = None,
        agent_configuration: dict[str, object] | None = None,
        merchant_information: dict[str, object] | None = None,
        discovery: dict[str, object] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        environment: dict[str, str] | None = None,
        interpreter: str | None = None,
    ) -> None:
        self._base_url = base_url
        self._token = token
        self._served = served
        self._strategy = strategy
        self._provision_mandate = provision_mandate
        self._agent_configuration = agent_configuration
        self._merchant_information = merchant_information
        self._discovery = discovery
        self._timeout = timeout
        self._environment = environment
        self._interpreter = sys.executable if interpreter is None else interpreter
        self._fault: ExecutionFault | None = None
        self._agent_evidence: AgentExecutionEvidence | None = None

    # The witness half.

    def begin(self) -> None:
        """Forget the previous mission, on both records at once.

        One call rather than two, because a witness whose two halves can be reset separately is
        a witness that can be half stale, and the stale half would be the one attributing a
        fault to the wrong mission.
        """
        self._fault = None
        self._agent_evidence = None
        self._served.begin()

    def fault(self) -> ExecutionFault | None:
        """What went wrong, decided from the process and the server and never from the report.

        A server response that establishes a database outage comes first. The worker can only
        fail after receiving that response, so its exit is a consequence of benchmark
        infrastructure rather than evidence about the buyer. Other 5xx responses are merchant
        findings. Only when the server produced no failure does a worker exit, timeout or broken
        report become an agent failure.
        """
        failure = self._served.first_failure()
        if failure is not None:
            if failure.failure == DATABASE_UNAVAILABLE:
                return ExecutionFault(
                    origin=FaultOrigin.HARNESS,
                    detail="the benchmark database was unavailable",
                    operation=failure.path,
                )
            return ExecutionFault(
                origin=FaultOrigin.MERCHANT,
                detail=f"the merchant answered {failure.status}",
                operation=failure.path,
            )
        provider = _provider_fault(self._agent_evidence)
        if provider is not None:
            return provider
        return self._fault

    def payment_attempted(self) -> bool:
        return self._served.payment_attempted()

    def evidence(self) -> CommerceEvidence:
        """What the merchant answered where no row records it, read by the server that answered.

        The worker is not asked and has nothing to say: an authorization decision and a
        preparation that could not hold stock are read out of the response bodies this endpoint
        itself wrote.
        """
        return self._served.evidence()

    # The executor half.

    async def __call__(self, brief: AgentMissionBrief, *, merchant_id: uuid.UUID) -> ExecutorReport:
        """Carry one mission out in a process that has no database, and read what came back.

        Anything that goes wrong with the process becomes an empty report and a trusted fault.
        The runner substantiates any payment or authorization response before deciding whether
        the mission can be recorded. It leaves a mission RUNNING only when the trusted payment
        state is genuinely unresolved, not merely because the worker died after a known denial.
        """
        try:
            mandate_id = (
                None if self._provision_mandate is None else await self._provision_mandate(brief)
            )
            request = MissionRequest(
                brief=brief,
                merchant_id=merchant_id,
                base_url=self._base_url,
                token=self._token,
                strategy=self._strategy,
                mandate_id=mandate_id,
                agent_configuration=self._agent_configuration,
                merchant_information=self._merchant_information,
                discovery=self._discovery,
            )
            return await self._carry_out(request)
        except _WorkerFailureError as failed:
            self._fault = ExecutionFault(origin=failed.origin, detail=failed.detail)
            return ExecutorReport(merchant_id=merchant_id)
        except Exception as failed:
            self._fault = ExecutionFault(
                origin=FaultOrigin.HARNESS,
                detail=f"trusted mission provisioning failed: {type(failed).__name__}",
            )
            return ExecutorReport(merchant_id=merchant_id)

    async def _carry_out(self, request: MissionRequest) -> ExecutorReport:
        # An empty directory of its own, and this is load bearing rather than tidy. `Settings`
        # reads a `.env` file from the working directory, so a worker started in a checkout
        # picks up the developer's `POSTGRES_PASSWORD` from disk however carefully its
        # environment was built. Found by a test that asserted the opposite of what this did.
        with tempfile.TemporaryDirectory(prefix="agentrank-benchmark-") as sandbox:
            return await self._through(request, sandbox)

    async def _through(self, request: MissionRequest, sandbox: str) -> ExecutorReport:
        process = await self._spawn(sandbox)
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(json.dumps(request.to_payload()).encode("utf-8")),
                timeout=self._timeout,
            )
        except TimeoutError as expired:
            await self._reap(process)
            # The buyer's, and this is the one attribution a poorly performing model would most
            # like to have the other way. A mission it never finished is a mission it failed.
            raise _WorkerFailureError(
                f"the executor process did not finish in {self._timeout}s",
                FaultOrigin.AGENT,
            ) from expired
        if process.returncode != 0:
            # The worker's own words, not its classification. It has no way to say whose fault
            # anything was and this does not read one out of the text: the exit code is what is
            # believed and the text is only for a person reading a failure.
            raise _WorkerFailureError(
                f"the executor process exited {process.returncode}: {_said(stderr)}",
                _exited(process.returncode),
            )
        try:
            document = json.loads(stdout.decode("utf-8"))
            self._agent_evidence = agent_evidence_from_payload(document)
            return report_from_payload(document)
        except (UnicodeDecodeError, json.JSONDecodeError) as unreadable:
            raise _WorkerFailureError(
                "the executor process did not report JSON", FaultOrigin.AGENT
            ) from unreadable
        except (ProtocolError, ValueError) as malformed:
            raise _WorkerFailureError(
                f"the executor process reported {malformed}", FaultOrigin.AGENT
            ) from malformed

    def take_agent_evidence(self) -> AgentExecutionEvidence | None:
        """Return this mission's validated worker evidence to trusted orchestration once."""
        evidence, self._agent_evidence = self._agent_evidence, None
        return evidence

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
            # Ours. Nothing about the buyer was involved in failing to start it.
            raise _WorkerFailureError(
                f"the executor process could not be started: {unstartable}",
                FaultOrigin.HARNESS,
            ) from unstartable

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


def _provider_fault(evidence: AgentExecutionEvidence | None) -> ExecutionFault | None:
    """Classify a provider outage observed by the fixed worker runtime, not model text.

    Only a mission that actually ended on the outage is attributed. A throttled invocation that
    was retried and recovered also records PROVIDER_ERROR events, and those are diagnostic
    history rather than a fault: the buyer went on to carry the mission out.
    """
    if evidence is None or not evidence.events:
        return None
    last = evidence.events[-1]
    if last.event_type != "AGENT_ABORT" or last.payload.get("reason") != "provider_unavailable":
        return None
    event = next(
        (event for event in reversed(evidence.events) if event.event_type == "PROVIDER_ERROR"),
        None,
    )
    if event is None:
        return None
    detail = event.payload.get("detail")
    if not isinstance(detail, str) or not detail:
        detail = "provider failure without detail"
    return ExecutionFault(origin=FaultOrigin.AGENT, detail=f"LLM provider failed: {detail}")


def _exited(code: int | None) -> FaultOrigin:
    """Whose failure an exit code describes.

    Two codes are the worker refusing before it read a mission at all: an environment it should
    not be able to see, and a request this side wrote that it could not read. Both are the
    harness's own doing and neither is anything a buyer decided.

    Everything else is the buyer. The exit code is trusted because the code that produces it is
    this repository's and runs before any model output is involved, and because an unrecognised
    code is attributed to the buyer rather than excused.
    """
    if code in {EXIT_NOT_ISOLATED, EXIT_PROTOCOL}:
        return FaultOrigin.HARNESS
    return FaultOrigin.AGENT


class _WorkerFailureError(RuntimeError):
    """Something went wrong with the process rather than inside the mission.

    It carries the origin because the origin is decided where the failure was observed rather
    than reconstructed afterwards from a message. A process that could not be started and a
    process that hung are both failures of the same object and they belong to different sides.
    """

    def __init__(self, detail: str, origin: FaultOrigin) -> None:
        super().__init__(detail)
        self.detail = detail
        self.origin = origin


def _said(stderr: bytes) -> str:
    """Whatever the worker wrote for a human, bounded and decoded safely.

    Bounded because a worker is not trusted to be brief, and a failure message that carried a
    megabyte of output would be one nobody reads. Decoded with replacement because a process
    that is failing is a process that may also be writing broken text.
    """
    text = stderr.decode("utf-8", errors="replace").strip()
    return text[:500] if text else "it said nothing"
