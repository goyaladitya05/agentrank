"""Liveness and readiness endpoints.

/health answers from the process alone and never touches infrastructure, so an
orchestrator can tell "the process is wedged" apart from "a dependency is down".
/ready reports every dependency the application needs before it can serve traffic.
"""

from typing import Literal

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from agentrank_api.database import check_connection

router = APIRouter(tags=["system"])


class HealthResponse(BaseModel):
    status: Literal["ok"]


class ComponentStatus(BaseModel):
    name: str
    status: Literal["connected", "unavailable"]
    detail: str | None = None


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    components: list[ComponentStatus]


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Report that the API process is running."""
    return HealthResponse(status="ok")


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessResponse}},
)
async def ready(request: Request, response: Response) -> ReadinessResponse:
    """Report whether required infrastructure is available."""
    try:
        await check_connection(request.app.state.engine)
    except SQLAlchemyError as error:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        # Only the exception class is reported. Driver messages can carry the host,
        # user and other connection details that do not belong in an HTTP response.
        database = ComponentStatus(
            name="database",
            status="unavailable",
            detail=type(error).__name__,
        )
        return ReadinessResponse(status="not_ready", components=[database])

    database = ComponentStatus(name="database", status="connected")
    return ReadinessResponse(status="ready", components=[database])
