"""FastAPI application factory."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from agentrank_api.config import Settings, get_settings
from agentrank_api.database import create_engine, create_session_factory
from agentrank_api.errors import (
    AuthenticationError,
    ConflictError,
    ErrorResponse,
    NotFoundError,
    UpstreamError,
)
from agentrank_api.payments.provider import PaymentProvider
from agentrank_api.payments.wiring import build_payment_provider
from agentrank_api.razorpay.client import HttpRazorpayClient, RazorpayClient
from agentrank_api.razorpay.wiring import build_razorpay_client
from agentrank_api.routes import (
    checkouts,
    commerce,
    constraints,
    mandates,
    payments,
    razorpay,
    system,
)


def create_app(
    settings: Settings | None = None,
    payment_provider: PaymentProvider | None = None,
    razorpay_client: RazorpayClient | None = None,
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
    )

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
    return app
