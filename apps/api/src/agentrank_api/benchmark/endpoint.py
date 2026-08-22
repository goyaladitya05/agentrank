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

`issued_credential` is the key. An ordinary merchant API credential, scoped to the one
merchant the world describes, issued before the run and revoked after it whatever happens. It is
not a superuser token and there is no such thing here: authentication answers with a merchant
identifier and nothing else, so what this credential can do is exactly what that merchant's own
integration can do. Cross merchant calls answer 404 with no side effect, which is the property
Phase 1H built and which this deliberately does not extend.

The token exists in one string and never reaches a log, an argument vector or an environment
variable. It is handed to the client that presents it and to nothing else. Revoking it is in a
`finally`, so a run that raised still ends with a credential nobody can use, and the revocation
is terminal because the authentication read has the condition in its SQL.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import TracebackType
from typing import Self

import uvicorn
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agentrank_api.auth.service import MerchantCredentialService
from agentrank_api.auth.tokens import TokenMarker
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


@dataclass(frozen=True, slots=True)
class ServedRequest:
    """One request the endpoint answered, as the server itself saw it.

    Method, path and status. No body, no header and no query string: this is evidence about the
    boundary rather than a trace of a mission, and a record carrying an Authorization header
    would be a credential written into a benchmark's own bookkeeping.
    """

    method: str
    path: str
    status: int


class RequestLedger:
    """Every request the endpoint answered, recorded on the trusted side of the boundary.

    This is what makes an out of process executor attributable. A worker that dies says nothing,
    and a worker that lies says whatever it likes, so neither can be the source of what happened.
    The server answered the requests, and the server is ours.

    Two questions, the same two an in process `ToolLedger` answers, and for the same reasons. A
    5xx means the merchant surface failed rather than answered. A request to the payment route
    means money may have moved, whatever came back and whether or not the caller survived to
    report it.
    """

    def __init__(self) -> None:
        self._served: list[ServedRequest] = []

    def begin(self) -> None:
        self._served = []

    def record(self, served: ServedRequest) -> None:
        self._served.append(served)

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
    """

    def __init__(self, app: ASGIApp, ledger: RequestLedger) -> None:
        self._app = app
        self._ledger = ledger

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        answered = {"status": 0}

        async def watching(message: Message) -> None:
            if message["type"] == "http.response.start":
                answered["status"] = int(message["status"])
            await send(message)

        try:
            await self._app(scope, receive, watching)
        finally:
            self._ledger.record(
                ServedRequest(
                    method=str(scope.get("method", "")),
                    path=str(scope.get("path", "")),
                    # Nothing answered at all is a failure of the surface, which is what a
                    # zero here means and what the 5xx test below then reads it as.
                    status=answered["status"] or 500,
                )
            )


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
