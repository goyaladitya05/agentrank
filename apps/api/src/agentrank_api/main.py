"""FastAPI application factory."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from agentrank_api.config import Settings, get_settings
from agentrank_api.database import create_engine, create_session_factory
from agentrank_api.errors import (
    AuthenticationError,
    ConflictError,
    ErrorResponse,
    InvalidField,
    InvalidRequestResponse,
    NotFoundError,
    UpstreamError,
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
    constraints,
    evaluations,
    insights,
    mandates,
    payments,
    razorpay,
    sources,
    system,
)

# What a validation refusal may say about one field.
#
# All three bounds exist for one reason: a location part can be a field name the caller invented.
# A body rejected for an unexpected field puts that name into the location, so without a bound on
# its length a megabyte of key name comes back twice over, in the location and again in the
# sentence built from it. The number of parts and the message length are bounded for symmetry;
# neither is reachable by a caller, because the deepest location any schema here produces is seven
# parts and every message is a validator's own fixed string.
MAX_INVALID_FIELDS = 20
MAX_FIELD_LOCATION_PARTS = 12
MAX_FIELD_LOCATION_PART_LENGTH = 64
MAX_FIELD_MESSAGE_LENGTH = 200


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
        fields = [
            InvalidField(
                location=[
                    _shortened(str(part), MAX_FIELD_LOCATION_PART_LENGTH)
                    for part in entry.get("loc", ())
                ][:MAX_FIELD_LOCATION_PARTS],
                message=_shortened(str(entry.get("msg", "is not valid")), MAX_FIELD_MESSAGE_LENGTH),
            )
            for entry in error.errors()[:MAX_INVALID_FIELDS]
        ]
        named = "; ".join(
            f"{'.'.join(field.location) or 'body'}: {field.message}" for field in fields[:3]
        )
        body = InvalidRequestResponse(
            error="invalid_request",
            detail=named or "the request body could not be read",
            fields=fields,
        )
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

    @app.exception_handler(SQLAlchemyError)
    async def handle_database_unavailable(_: Request, error: SQLAlchemyError) -> JSONResponse:
        """Refuse a request when this application's database cannot establish a fact.

        This is infrastructure, not a merchant business answer and not a generic 500. The stable
        response lets the trusted benchmark endpoint distinguish a database outage from a
        merchant surface failure without exposing driver detail to a buyer.
        """
        logging.getLogger(__name__).warning("database request failed", exc_info=error)
        body = ErrorResponse(error="database_unavailable", detail="the database is unavailable")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=body.model_dump(),
        )

    app.include_router(system.router)
    app.include_router(commerce.router)
    app.include_router(mandates.router)
    app.include_router(checkouts.router)
    app.include_router(constraints.router)
    app.include_router(payments.router)
    app.include_router(razorpay.router)
    app.include_router(insights.router)
    app.include_router(compiler.router)
    app.include_router(sources.router)
    if benchmark_commands:
        app.include_router(evaluations.router)
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
            )
        },
    )
    return app


def _shortened(value: str, limit: int) -> str:
    """One string, bounded for a response, saying so rather than looking whole."""
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."
