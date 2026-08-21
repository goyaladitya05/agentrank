"""FastAPI application factory."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from agentrank_api.config import Settings, get_settings
from agentrank_api.database import create_engine
from agentrank_api.routes import system


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
        try:
            yield
        finally:
            await app.state.engine.dispose()

    app = FastAPI(
        title="AgentRank API",
        version="0.0.0",
        lifespan=lifespan,
    )
    app.include_router(system.router)
    return app
