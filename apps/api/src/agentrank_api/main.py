"""FastAPI application factory."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from agentrank_api.config import Settings, get_settings
from agentrank_api.database import create_engine, create_session_factory
from agentrank_api.errors import ErrorResponse, NotFoundError
from agentrank_api.routes import commerce, system


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    Settings are injectable so that tests can point the application at a different
    database without touching the process environment.
    """
    resolved = settings or get_settings()
    logging.basicConfig(level=resolved.log_level.upper())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = resolved
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

    app.include_router(system.router)
    app.include_router(commerce.router)
    return app
