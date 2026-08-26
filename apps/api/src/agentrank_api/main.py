"""FastAPI application factory."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import InterfaceError, OperationalError, SQLAlchemyError

from agentrank_api.config import Settings, get_settings
from agentrank_api.database import create_engine, create_session_factory
from agentrank_api.errors import (
    AuthenticationError,
    ConflictError,
    ErrorResponse,
    InvalidRequestError,
    InvalidRequestResponse,
    NotFoundError,
    UpstreamError,
    bounded_fields,
    refusal_detail,
)
from agentrank_api.importer.schemas import (
    MAX_IMPORT_REQUEST_BYTES,
    MAX_IMPORT_REQUEST_DEPTH,
)
from agentrank_api.limits import BodyLimit, RequestBodyLimit
from agentrank_api.payments.provider import PaymentProvider
from agentrank_api.payments.wiring import build_payment_provider
from agentrank_api.razorpay.client import HttpRazorpayClient, RazorpayClient
from agentrank_api.razorpay.wiring import build_razorpay_client
from agentrank_api.representation.schemas import (
    MAX_SOURCE_REQUEST_BYTES,
    MAX_SOURCE_REQUEST_DEPTH,
)
from agentrank_api.routes import (
    checkouts,
    commerce,
    compiler,
    console,
    constraints,
    evaluations,
    imports,
    insights,
    mandates,
    payments,
    razorpay,
    sources,
    system,
    workspaces,
)
from agentrank_api.schema import EXPECTED_REVISION

# The bound every request body takes unless its own path names a tighter one. See the middleware
# for why there is a default at all.
MAX_REQUEST_BYTES = 64 * 1024
MAX_REQUEST_DEPTH = 12


def create_app(
    settings: Settings | None = None,
    payment_provider: PaymentProvider | None = None,
    razorpay_client: RazorpayClient | None = None,
    *,
    benchmark_commands: bool = True,
) -> FastAPI:
    """Build the application.

    Settings are injectable so that tests can point the application at a different
    database without touching the process environment.

    The payment provider is injectable for the same reason and for one more: it is the only
    part of this system that is not this system, and a test that cannot configure a decline
    cannot test one. The default comes from `build_payment_provider`, which is the one place
    the choice is made, so the operator command line and this application cannot end up wired
    to different providers. It is a deterministic fake, because it is the only implementation
    that exists. Phase 1F is provider independent on purpose, and no request field can select a
    different outcome.

    `benchmark_commands` is the one router that is optional, and the reason is the loopback
    commerce endpoint an untrusted buyer is given. Publishing a suite, starting a run and reading
    a result have deliberately never been endpoints, so nothing an executor can reach over that
    server touches a run, an oracle or a mission definition. The merchant evaluation command
    is a benchmark write, and mounting it on the server a buyer holds a credential for would let
    a buyer queue a benchmark. The console's application mounts it; the buyer's does not.

    The Razorpay transport is injectable for the same reasons, and it is deliberately not the
    payment provider. Standard Checkout is interactive: the browser collects the payment and
    this application creates an order beforehand and confirms a result afterwards, so it does
    not fit an interface whose one operation is "perform this payment". It is None when no Test
    Mode key pair is configured, and the endpoints that need one refuse by name rather than the
    application refusing to start.
    """
    resolved = settings or get_settings()
    provider = payment_provider or build_payment_provider()
    transport = razorpay_client if razorpay_client is not None else build_razorpay_client(resolved)
    logging.basicConfig(level=resolved.log_level.upper())
    _report_configuration(resolved)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = resolved
        app.state.payment_provider = provider
        app.state.razorpay_client = transport
        app.state.engine = create_engine(resolved)
        app.state.session_factory = create_session_factory(app.state.engine)
        try:
            yield
        finally:
            await app.state.engine.dispose()
            # Only the real transport holds a connection pool. An injected fake has nothing to
            # close, and closing something a test still owns would be this application reaching
            # outside itself.
            if isinstance(transport, HttpRazorpayClient):
                await transport.aclose()

    app = FastAPI(
        title="AgentRank API",
        version="0.0.0",
        lifespan=lifespan,
        # Declared once, for every operation. FastAPI documents a 422 as its own
        # `HTTPValidationError` on any route with a schema, and this application does not answer
        # with that shape: `detail` is a sentence and the structured half is `fields`. A generated
        # client that trusted the default would fail to decode a refusal.
        responses={
            status.HTTP_422_UNPROCESSABLE_CONTENT: {
                "model": InvalidRequestResponse,
                "description": "The request body could not be read",
            }
        },
    )
    # Set here as well as in the lifespan, because none of the three needs a lifecycle: they are
    # decided when the application is built and nothing closes them. The engine and its session
    # factory genuinely do need one and stay there. This is what lets a caller that builds an
    # application without running its lifespan still read the configuration it was built with.
    app.state.settings = resolved
    app.state.payment_provider = provider
    app.state.razorpay_client = transport

    @app.exception_handler(AuthenticationError)
    async def handle_unauthenticated(_: Request, error: AuthenticationError) -> JSONResponse:
        """The caller did not establish who they are, so nothing about the resource is said.

        401 and not 403. The distinction is the whole of this phase: 403 would mean an
        identified caller is not permitted, and there is no identified caller here. A request
        that authenticates and then asks for somebody else's resource gets a 404 instead, from
        the merchant scoped query that found nothing.

        `WWW-Authenticate` because the status code requires it. It names the scheme and nothing
        else: no realm carrying a merchant name, and no parameter that could differ between a
        revoked credential and an unknown one.

        The body is the same for every way authentication can fail, and it is built from
        constants on the error rather than from anything the request supplied. Nothing here
        echoes a header, a token, a credential identifier or any fragment of one.
        """
        body = ErrorResponse(error=error.reason, detail=error.detail)
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content=body.model_dump(),
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.exception_handler(RequestValidationError)
    async def handle_invalid_request(_: Request, error: RequestValidationError) -> JSONResponse:
        """A request body this application could not read, without serializing what arrived.

        FastAPI's own handler answers by encoding `exc.errors()`, and every entry of that carries
        an `input` field holding the fragment of the caller's body that failed. Encoding it
        recurses once per nesting level, inside an exception handler that no `try` in the request
        path wraps, so a body of a few hundred nested brackets left this application answering
        500 to a request that was merely wrong, on every route with a body schema.

        An authenticated caller, to be exact. Body field errors are collected inside dependency
        solving and after the dependencies themselves run, so `require_merchant` refuses an
        anonymous caller before a validation error is ever built. That is a narrower blast radius
        than it first looked and not a smaller bug: every route here takes a merchant credential,
        and a merchant is not a trusted caller.

        So the value is never encoded. What comes back is where the body was wrong and what was
        wrong with it, plus a `detail` string naming the first few so a caller that reads only
        prose still learns something. Every part of it is bounded, including the length of a
        location part, because an unexpected-field error puts the caller's own key name there.
        Nothing here is recursive and the size of a refusal does not follow the size of what was
        refused.
        """
        fields = bounded_fields(error.errors())
        body = InvalidRequestResponse(
            error="invalid_request", detail=refusal_detail(fields), fields=fields
        )
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, content=body.model_dump()
        )

    @app.exception_handler(InvalidRequestError)
    async def handle_invalid_command(_: Request, error: InvalidRequestError) -> JSONResponse:
        """A refusal a route discovered after the body parsed, in the same shape as one before it.

        Routes used to answer these with `HTTPException(422, detail=str(error))`, which produced
        a body carrying only `detail`: no `error` code, no `fields`, and nothing a client
        generated from this application's own OpenAPI document could decode, because that
        document declares `InvalidRequestResponse` as the 422 model for every operation. Worse,
        `pydantic.ValidationError` is a `ValueError`, so a route catching one rendered pydantic's
        report verbatim, including the caller's own input value. Both are closed by there being
        one handler and one shape.
        """
        body = InvalidRequestResponse(error=error.reason, detail=error.detail, fields=error.fields)
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, content=body.model_dump()
        )

    @app.exception_handler(NotFoundError)
    async def handle_not_found(_: Request, error: NotFoundError) -> JSONResponse:
        """Services raise NotFoundError; routes never have to translate it."""
        body = ErrorResponse(
            error="not_found",
            detail=str(error),
            resource=error.resource,
            identifier=error.identifier,
        )
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content=body.model_dump())

    @app.exception_handler(ConflictError)
    async def handle_conflict(_: Request, error: ConflictError) -> JSONResponse:
        """State refused the request, rather than the request being wrong.

        409 and not 422: the body is well formed and the resource exists. An inactive
        variant or an empty shelf is a fact about now, and the same request could have
        succeeded an hour ago.
        """
        body = ErrorResponse(
            error=error.reason,
            detail=error.detail,
            resource=error.resource,
            identifier=error.identifier,
        )
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content=body.model_dump())

    @app.exception_handler(UpstreamError)
    async def handle_upstream(_: Request, error: UpstreamError) -> JSONResponse:
        """A system this application depends on did not give a usable answer.

        502 rather than 409, because nothing about the request or the state was wrong and a
        caller that could not tell the two apart would keep editing a request that was fine.
        502 rather than 500, because this application did not fail: a caller reading its own
        monitoring should be able to separate a bug here from a gateway that timed out.

        The body carries a code this repository chose and a sentence it wrote. Nothing an
        upstream said is in it, including for an upstream that answered with prose explaining
        itself.
        """
        body = ErrorResponse(error=error.reason, detail=error.detail)
        return JSONResponse(status_code=status.HTTP_502_BAD_GATEWAY, content=body.model_dump())

    @app.exception_handler(OperationalError)
    @app.exception_handler(InterfaceError)
    async def handle_database_unavailable(_: Request, error: SQLAlchemyError) -> JSONResponse:
        """Refuse a request when this application cannot reach its database.

        This is infrastructure, not a merchant business answer and not a generic 500. The stable
        response lets the trusted benchmark endpoint distinguish a database outage from a
        merchant surface failure without exposing driver detail to a buyer.

        Narrow on purpose, and it used to be registered on `SQLAlchemyError` instead. Every
        database error this application raises is a subclass of that, so an unmapped constraint
        violation, a query against a schema this build was not written for, and a database that
        is genuinely down all answered `503 database_unavailable`. Two of those three are bugs,
        and `agentrank_api.conflicts` re-raises an unrecognised violation expressly so that a bug
        looks like one; answering 503 took that away and sent an operator to look at PostgreSQL
        instead. `OperationalError` and `InterfaceError` are the two SQLAlchemy raises when the
        connection itself is the problem, which is the only thing this response claims.
        """
        logging.getLogger(__name__).warning("database request failed", exc_info=error)
        body = ErrorResponse(error="database_unavailable", detail="the database is unavailable")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=body.model_dump(),
        )

    app.include_router(system.router)
    app.include_router(console.router)
    app.include_router(commerce.router)
    app.include_router(mandates.router)
    app.include_router(checkouts.router)
    app.include_router(constraints.router)
    app.include_router(payments.router)
    app.include_router(razorpay.router)
    app.include_router(insights.router)
    app.include_router(compiler.router)
    # Before the source router, and that order is load bearing rather than tidy. Starlette matches
    # in registration order, and `/api/v1/sources/{source_snapshot_id}` would otherwise swallow
    # `/api/v1/sources/imports` and answer it with "imports is not a UUID". A test asserts that
    # listing imports works, which is what pins this.
    app.include_router(imports.router)
    app.include_router(sources.router)
    if benchmark_commands:
        app.include_router(evaluations.router)
        # Beside the evaluation command and gated by the same flag. Building an evaluation setup
        # publishes the benchmark suite a run is marked against, so a buyer holding a credential
        # for the loopback commerce endpoint must have no route to it.
        app.include_router(workspaces.router)
    # In front of routing rather than in a dependency, because FastAPI reads and parses a request
    # body before it solves dependencies: by the time a schema or a route could look at it, the
    # bytes have been received and the parser has already recursed through them. A merchant source
    # document is the one body this application accepts that a caller composes freely, so it is
    # the one with a declared bound on both its size and its nesting.
    app.add_middleware(
        RequestBodyLimit,
        limits={
            ("POST", sources.router.prefix): BodyLimit(
                max_bytes=MAX_SOURCE_REQUEST_BYTES, max_depth=MAX_SOURCE_REQUEST_DEPTH
            ),
            # The import command is a list of URLs rather than a document, so it is bounded far
            # more tightly. It is here for the same reason the source document is: it is a body a
            # caller composes freely, and finding out that it is not an import command should not
            # require receiving and parsing it.
            ("POST", imports.router.prefix): BodyLimit(
                max_bytes=MAX_IMPORT_REQUEST_BYTES, max_depth=MAX_IMPORT_REQUEST_DEPTH
            ),
        },
        # Every other body this application accepts. Not a considered figure for any one route
        # and deliberately generous: the largest of them is a checkout with its whole line
        # allowance or a mandate with its whole constraint allowance, both of which are orders of
        # magnitude inside this. What it replaces is nothing at all, which is what an
        # authenticated merchant posting half a gigabyte to a commerce command used to get.
        default=BodyLimit(max_bytes=MAX_REQUEST_BYTES, max_depth=MAX_REQUEST_DEPTH),
    )
    return app


def _report_configuration(settings: Settings) -> None:
    """One startup line saying what this process is, in names and presence and never in values.

    An operator looking at a process that is behaving unexpectedly has to be able to tell which
    optional capabilities it holds without reading its environment, and a deployment reading its
    own boot log is the cheapest place for that. Which schema this build expects is here for the
    same reason: it is what a readiness probe will be comparing against.

    Every value is a boolean or a name this repository chose. No credential, no host, no database
    name and no URL passes through here.
    """
    capabilities = settings.capability_report()
    logging.getLogger(__name__).info(
        "agentrank api starting: environment=%s schema=%s capabilities=%s",
        settings.environment,
        EXPECTED_REVISION,
        ",".join(sorted(name for name, present in capabilities.items() if present)) or "none",
    )
