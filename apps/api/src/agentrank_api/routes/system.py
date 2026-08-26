"""Liveness and readiness endpoints.

/health answers from the process alone and never touches infrastructure, so an orchestrator can
tell "the process is wedged" apart from "a dependency is down".

/ready reports every dependency this process needs before it can serve traffic. Two things
qualify, and each of them makes every useful request fail when it is wrong:

```text
database   PostgreSQL answers
schema     the database is at the migration revision this build expects
```

Nothing here calls a model provider, and nothing here consumes quota. A readiness probe runs on a
schedule forever, and one that spent provider budget would be a bill nobody authorised.

Neither endpoint requires a credential, which is what makes what they may say a security question
rather than a formatting one. They name components and states, and never a host, a user, a URL,
a configured value or a driver's own message. A migration revision is reported, because it is a
build fact that appears in filenames and history and is the one thing that makes a readiness
probe useful while a deploy is in flight.
"""

from typing import Literal

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from agentrank_api.database import check_connection
from agentrank_api.schema import EXPECTED_REVISION, applied_revision

router = APIRouter(tags=["system"])


class HealthResponse(BaseModel):
    status: Literal["ok"]


class ComponentStatus(BaseModel):
    name: str
    status: Literal["connected", "unavailable", "compatible", "incompatible"]
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
    """Report whether required infrastructure is available and compatible."""
    engine = request.app.state.engine
    try:
        await check_connection(engine)
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

    components = [ComponentStatus(name="database", status="connected")]

    try:
        applied = await applied_revision(engine)
    except SQLAlchemyError as error:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        components.append(
            ComponentStatus(name="schema", status="unavailable", detail=type(error).__name__)
        )
        return ReadinessResponse(status="not_ready", components=components)

    if applied == EXPECTED_REVISION:
        components.append(
            ComponentStatus(name="schema", status="compatible", detail=EXPECTED_REVISION)
        )
        return ReadinessResponse(status="ready", components=components)

    # Not ready rather than ready with a warning. A process serving requests against a schema it
    # was not built for is the failure mode an explicit migration step exists to prevent, and a
    # rolling deploy that treated this as cosmetic would route traffic straight into it.
    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    components.append(
        ComponentStatus(
            name="schema",
            status="incompatible",
            detail=(
                f"expected {EXPECTED_REVISION},"
                f" found {'no migrations applied' if applied is None else applied}"
            ),
        )
    )
    return ReadinessResponse(status="not_ready", components=components)
