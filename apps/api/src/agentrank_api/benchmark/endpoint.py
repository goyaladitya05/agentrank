"""The commerce API on loopback, and the one credential a benchmark buyer is given to call it.

A buyer outside this process needs somewhere to send a request. In a deployment that is the API
somebody is already running; for an operator command and for a test it is this: the same
`create_app`, the same routes, the same authentication, listening on 127.0.0.1 on a port the
kernel chose, for exactly as long as one benchmark run takes.

Two things live here and both are about the boundary rather than about commerce.

`LocalCommerceEndpoint` is the server. It is bound to loopback and never to an interface, and it
is given the payment provider the command line is already holding rather than building one, so a
benchmark's payments and the operator's view of them are the same provider's. It has no benchmark
routes on it: publishing a suite, starting a run and reading a result are operator commands and
have deliberately never been endpoints, so nothing an executor can reach over this can touch a
run, an oracle or a mission definition.

`issued_benchmark_credential` is the key. It is issued only after the run has a durable RUNNING
claim and is structurally bound to that run as well as its merchant. It is still not a superuser
token: authentication reconstructs only that run's narrow mutation capability, so it cannot
authorize a different merchant or a later run. Cross merchant calls answer 404 with no side
effect, which is the property Phase 1H built and which this deliberately does not extend.

The token exists in one string and never reaches a log, an argument vector or an environment
variable. It is handed to the client that presents it and to nothing else. Revoking it is in a
`finally`, so a run that raised still ends with a credential nobody can use, and the revocation
is terminal because the authentication read has the condition in its SQL.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from http import HTTPStatus
from types import TracebackType
from typing import Self

import uvicorn
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agentrank_api.auth.service import MerchantCredentialService
from agentrank_api.auth.tokens import TokenMarker
from agentrank_api.benchmark.evidence import (
    CommerceEvidence,
    after_payment,
    after_preparation,
    payment_from_body,
    preparation_from_body,
)
from agentrank_api.benchmark.execution import BenchmarkRunCapability
from agentrank_api.config import Settings
from agentrank_api.main import create_app
from agentrank_api.payments.provider import PaymentProvider

# The label every benchmark credential carries, so one is recognizable in `credentials list` and
# an operator can tell a key that a run minted from a key a person did.
CREDENTIAL_LABEL = "benchmark executor"

# How long the server is given to bind before this gives up. Generous, because a loopback bind is
# immediate and anything slower than this is a machine in trouble rather than a slow start.
STARTUP_TIMEOUT = 20.0

# How often the bind is checked. Short enough that starting is not perceptibly slower than the
# bind itself.
STARTUP_POLL = 0.02


# Which request path means a payment was dispatched. Matched on the suffix rather than parsed,
# because the only thing that matters is that the payment route was reached at all.
PAYMENT_PATH_SUFFIX = "/payments"

# The one other answer worth reading, and the reason is that it leaves no row. A preparation that
# was authorized and could not hold the stock writes nothing, and neither does an authorization
# denial, so both are read here or not at all.
PREPARATION_PATH_SUFFIX = "/prepare-execution"

# How much of a response body this is willing to hold while it is being read. Both bodies are a
# few hundred bytes; the bound exists so that a benchmark's own bookkeeping cannot be made to
# hold something large by anything the buyer asks for.
MAX_RECORDED_BODY = 256 * 1024

DATABASE_UNAVAILABLE = "database_unavailable"


@dataclass(frozen=True, slots=True)
class ServedRequest:
    """One request the endpoint answered, as the server itself saw it.

    Method, path, status and an optional server-written failure code. No body, no header and no
    query string survive: this is evidence about the boundary rather than a trace of a mission,
    and a record carrying an Authorization header would be a credential written into a
    benchmark's own bookkeeping.
    """

    method: str
    path: str
    status: int
    failure: str | None = None


class RequestLedger:
    """Every request the endpoint answered, recorded on the trusted side of the boundary.

    This is what makes an out of process executor attributable. A worker that dies says nothing,
    and a worker that lies says whatever it likes, so neither can be the source of what happened.
    The server answered the requests, and the server is ours.

    Three questions, the same three an in process `ToolLedger` answers, and for the same reasons.
    A 5xx means the merchant surface failed rather than answered. A request to the payment route
    means money may have moved, whatever came back and whether or not the caller survived to
    report it. And two answers leave no row behind, so they are read from the responses this
    server itself wrote: what the authorization layer decided, and whether an allowed preparation
    could hold the stock.

    The bodies are parsed back through the models the routes declared rather than picked apart by
    key, so a field renamed on a view is a failure here rather than a silently absent fact. A body
    that will not parse records nothing, which is the fail closed direction: it is not evidence
    about an authorization.
    """

    def __init__(self) -> None:
        self._served: list[ServedRequest] = []
        self._evidence = CommerceEvidence()

    def begin(self) -> None:
        self._served = []
        self._evidence = CommerceEvidence()

    def record(self, served: ServedRequest) -> None:
        self._served.append(served)

    def observe(self, served: ServedRequest, body: bytes) -> None:
        """Read the two merchant answers that no row records out of a response this server wrote.

        Only the two routes that produce them, and only when the server actually answered. A 4xx
        or a 5xx carries an error document rather than a decision, and reading a decision out of
        one would be inventing an answer nobody gave.
        """
        if served.status != HTTPStatus.OK:
            return
        try:
            payload = json.loads(body)
        except UnicodeDecodeError, json.JSONDecodeError:
            return

        if served.path.endswith(PREPARATION_PATH_SUFFIX):
            prepared = preparation_from_body(payload)
            if prepared is not None:
                self._evidence = after_preparation(self._evidence, prepared)
            return

        paid = payment_from_body(payload)
        if paid is not None:
            self._evidence = after_payment(self._evidence, paid)

    def evidence(self) -> CommerceEvidence:
        """The merchant answers this mission produced that no row records."""
        return self._evidence

    @property
    def served(self) -> tuple[ServedRequest, ...]:
        return tuple(self._served)

    def first_failure(self) -> ServedRequest | None:
        """The first request the server could not answer, or None when it answered them all.

        The first rather than the last, for the reason the tool ledger uses: a buyer stops at
        its first refusal, so a later failure only happened because the first was swallowed.
        """
        return next((served for served in self._served if served.status >= 500), None)

    def payment_attempted(self) -> bool:
        return any(
            served.method == "POST" and served.path.endswith(PAYMENT_PATH_SUFFIX)
            for served in self._served
        )


class _Recording:
    """An ASGI wrapper that records what was answered and changes nothing about the answer.

    A plain wrapper rather than middleware registered on the application, so that the recording
    exists only for the endpoint a benchmark starts and never for a deployment. The status is
    read off the response start message, and the record is written in a `finally` so that a
    request the application failed to answer at all is still one that happened.

    Two routes have their response body kept as well as their status, and only those two. A
    preparation and a payment request are where the merchant states an authorization decision,
    and an authorization decision that denies writes no row anywhere, deliberately: a refusal
    with a side effect would be a worse refusal. Keeping the body this server just wrote is the
    only way that answer survives the request, and it is trusted for the obvious reason that the
    server wrote it. Nothing an executor sends is read here.

    The body is bounded and forwarded untouched. A buyer sees exactly the bytes it would have
    seen; what differs is that the trusted side kept a copy of two of them.
    """

    def __init__(self, app: ASGIApp, ledger: RequestLedger) -> None:
        self._app = app
        self._ledger = ledger

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        answered = {"status": 0}
        keeping = _answers_a_decision(scope)
        body = bytearray()

        async def watching(message: Message) -> None:
            if message["type"] == "http.response.start":
                answered["status"] = int(message["status"])
            elif message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                if len(body) + len(chunk) <= MAX_RECORDED_BODY:
                    body.extend(chunk)
            await send(message)

        try:
            await self._app(scope, receive, watching)
        finally:
            served = ServedRequest(
                method=str(scope.get("method", "")),
                path=str(scope.get("path", "")),
                # Nothing answered at all is a failure of the surface, which is what a
                # zero here means and what the 5xx test below then reads it as.
                status=answered["status"] or 500,
                failure=_failure_from(bytes(body)) if answered["status"] >= 500 else None,
            )
            self._ledger.record(served)
            if keeping:
                self._ledger.observe(served, bytes(body))


def _answers_a_decision(scope: Scope) -> bool:
    """Whether this request is one of the two whose answer carries an authorization decision."""
    if str(scope.get("method", "")) != "POST":
        return False
    path = str(scope.get("path", ""))
    return path.endswith(PREPARATION_PATH_SUFFIX) or path.endswith(PAYMENT_PATH_SUFFIX)


def _failure_from(body: bytes) -> str | None:
    """The stable code in a server error, if this application supplied one.

    The body is discarded immediately after parsing. A failure code is evidence because the
    server wrote it; arbitrary response prose is not retained or interpreted.
    """
    try:
        payload = json.loads(body)
    except UnicodeDecodeError, json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    code = payload.get("error")
    return code if isinstance(code, str) else None


class LocalCommerceEndpoint:
    """The merchant commerce API, listening on loopback for as long as this is entered.

    A real socket rather than an in process transport, and the difference is the whole point: a
    buyer in another process cannot be handed an ASGI application, and a benchmark that measured
    one would not be measuring anything an agent could reach.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        provider: PaymentProvider,
        observer: RequestLedger | None = None,
    ) -> None:
        application = create_app(settings, payment_provider=provider)
        self._app: ASGIApp = application if observer is None else _Recording(application, observer)
        self._server = uvicorn.Server(
            uvicorn.Config(
                self._app,
                host="127.0.0.1",
                port=0,
                # Nothing about a benchmark run belongs in an access log, and an access log
                # carrying a merchant's product identifiers is one more place a run's contents
                # leak. The application's own structured logging is unaffected.
                access_log=False,
                log_level="warning",
                lifespan="on",
            )
        )
        self._serving: asyncio.Task[None] | None = None
        self._port: int | None = None

    @property
    def base_url(self) -> str:
        """Where a buyer sends its requests. Raises before the server has bound."""
        if self._port is None:
            raise RuntimeError("the benchmark commerce endpoint is not listening")
        return f"http://127.0.0.1:{self._port}"

    async def __aenter__(self) -> Self:
        self._serving = asyncio.create_task(self._server.serve())
        await self._await_bind()
        self._port = self._bound_port()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._server.should_exit = True
        if self._serving is not None:
            await self._serving
        self._port = None

    async def _await_bind(self) -> None:
        """Wait until the server is actually listening, or say that it never did.

        Polled rather than signalled, because uvicorn exposes `started` and no event. The bound
        is what turns a server that failed to start into a failure naming this, rather than a
        benchmark that hangs before its first mission.
        """
        assert self._serving is not None
        waited = 0.0
        while not self._server.started:
            if self._serving.done():
                # Surfaces whatever the server raised, which is the useful error. A port that
                # cannot be bound and an application that failed its lifespan both arrive here.
                await self._serving
                raise RuntimeError("the benchmark commerce endpoint stopped before it listened")
            if waited >= STARTUP_TIMEOUT:
                raise RuntimeError(
                    f"the benchmark commerce endpoint did not listen within {STARTUP_TIMEOUT}s"
                )
            await asyncio.sleep(STARTUP_POLL)
            waited += STARTUP_POLL

    def _bound_port(self) -> int:
        sockets = [socket for server in self._server.servers for socket in server.sockets]
        if not sockets:
            raise RuntimeError("the benchmark commerce endpoint reported no socket")
        return int(sockets[0].getsockname()[1])


@asynccontextmanager
async def issued_credential(
    service: MerchantCredentialService, *, merchant_id: uuid.UUID, marker: TokenMarker
) -> AsyncIterator[str]:
    """One merchant API credential for the length of a benchmark run, revoked afterwards.

    Scoped to the merchant the world describes and to nothing else, because that is all a
    merchant credential can be: authentication answers with a merchant identifier, so there is
    no wider key to issue by accident.

    Revoked in a `finally`. A run that raised still ends with a credential nobody can present,
    and the revocation is terminal: the authentication read has the condition in its SQL, so
    there is no cache and no window. A request that was already authenticated when the
    revocation committed is not retroactively unauthenticated, which is the honest boundary and
    is bounded here by the run being over.
    """
    issued = await service.issue(merchant_id=merchant_id, label=CREDENTIAL_LABEL, marker=marker)
    try:
        yield issued.token
    finally:
        await service.revoke(issued.credential.id)


@asynccontextmanager
async def issued_benchmark_credential(
    service: MerchantCredentialService,
    *,
    capability: BenchmarkRunCapability,
    marker: TokenMarker,
) -> AsyncIterator[str]:
    """Issue a loopback credential structurally bound to the active benchmark run.

    This is separate from ``issued_credential`` because an ordinary credential must never gain
    run authority by a caller adding a flag.  The service checks the durable RUNNING claim as it
    creates the row; authentication later reconstructs the same capability only from that row.
    """
    issued = await service.issue_for_benchmark(
        capability=capability, label=CREDENTIAL_LABEL, marker=marker
    )
    try:
        yield issued.token
    finally:
        await service.revoke(issued.credential.id)
