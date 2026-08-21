"""FastAPI application factory."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from agentrank_api.config import Settings, get_settings
from agentrank_api.database import create_engine, create_session_factory
from agentrank_api.errors import ConflictError, ErrorResponse, NotFoundError
from agentrank_api.payments.provider import PaymentProvider
from agentrank_api.payments.wiring import build_payment_provider
from agentrank_api.routes import checkouts, commerce, constraints, mandates, payments, system


def create_app(
    settings: Settings | None = None, payment_provider: PaymentProvider | None = None
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
    """
    resolved = settings or get_settings()
    provider = payment_provider or build_payment_provider()
    logging.basicConfig(level=resolved.log_level.upper())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = resolved
        app.state.payment_provider = provider
        app.state.engine = create_engine(resolved)
        app.state.session_factory = create_session_factory(app.state.engine)
        try:
            yield
        finally:
            await app.state.engine.dispose()

    app = FastAPI(
        title="AgentRank API",
        version="0.0.0",
        lifespan=lifespan,
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

    app.include_router(system.router)
    app.include_router(commerce.router)
    app.include_router(mandates.router)
    app.include_router(checkouts.router)
    app.include_router(constraints.router)
    app.include_router(payments.router)
    return app
