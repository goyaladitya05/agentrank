"""Behavior of the liveness and readiness endpoints.

Neither requires a credential, so what they may say is a security question as much as a
formatting one, and both of them are asserted here rather than read.
"""

from collections.abc import Callable

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import text

from agentrank_api.config import Settings
from agentrank_api.main import create_app
from agentrank_api.schema import EXPECTED_REVISION


def components(body: dict[str, object]) -> dict[str, dict[str, object]]:
    listed = body["components"]
    assert isinstance(listed, list)
    return {entry["name"]: entry for entry in listed}


def test_health_reports_the_process_is_running(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_reports_the_database_and_the_schema_it_is_at(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    named = components(body)
    assert named["database"]["status"] == "connected"
    assert named["schema"]["status"] == "compatible"
    assert named["schema"]["detail"] == EXPECTED_REVISION


def test_ready_fails_when_the_database_is_unreachable(settings: Settings, unused_port: int) -> None:
    unreachable = settings.model_copy(update={"postgres_port": unused_port})

    with TestClient(create_app(unreachable)) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["components"][0]["status"] == "unavailable"


def test_ready_refuses_a_database_at_the_wrong_migration(
    throwaway_database: Settings,
) -> None:
    """A process serving requests against a schema it was not built for is the deploy hazard
    an explicit migration step exists to prevent, so this is not ready rather than ready with
    a warning."""
    with TestClient(create_app(throwaway_database)) as client:
        unmigrated = client.get("/ready")

    assert unmigrated.status_code == 503
    body = unmigrated.json()
    assert body["status"] == "not_ready"
    schema = components(body)["schema"]
    assert schema["status"] == "incompatible"
    assert "no migrations applied" in str(schema["detail"])
    assert EXPECTED_REVISION in str(schema["detail"])


def test_ready_names_a_revision_it_does_not_recognise(settings: Settings) -> None:
    """A database migrated past this build reads as incompatible in the same words."""
    application = create_app(settings)
    with TestClient(application) as client:
        engine = application.state.engine
        assert client.get("/ready").status_code == 200

        async def stamp(revision: str) -> None:
            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE alembic_version SET version_num = :revision"),
                    {"revision": revision},
                )

        portal = client.portal
        assert portal is not None
        portal.call(stamp, "not-a-revision-this-build-knows")
        try:
            response = client.get("/ready")
            assert response.status_code == 503
            schema = components(response.json())["schema"]
            assert schema["status"] == "incompatible"
        finally:
            portal.call(stamp, EXPECTED_REVISION)


def test_neither_endpoint_names_a_host_a_user_or_a_configured_value(settings: Settings) -> None:
    """A probe nobody has to authenticate for is not where connection details belong."""
    with TestClient(create_app(settings)) as client:
        answers = f"{client.get('/health').text}{client.get('/ready').text}"

    for secret in (
        settings.postgres_host,
        settings.postgres_user,
        settings.postgres_db,
        settings.postgres_password.get_secret_value(),
        str(settings.postgres_port),
    ):
        assert secret not in answers


def test_health_does_not_depend_on_the_database(settings: Settings, unused_port: int) -> None:
    unreachable = settings.model_copy(update={"postgres_port": unused_port})

    with TestClient(create_app(unreachable)) as client:
        response = client.get("/health")

    assert response.status_code == 200


def test_the_expected_revision_is_the_migration_head(
    settings: Settings, alembic_config_factory: Callable[[Settings], Config]
) -> None:
    """The one hazard of a constant, that somebody adds a migration and forgets it, closed here.

    `agentrank_api.schema` states the revision this build expects rather than reading the
    migrations directory at runtime, because a deployed process should not have to be able to
    find that directory to know what it was built against. This is what keeps the constant true.
    """
    directory = ScriptDirectory.from_config(alembic_config_factory(settings))
    assert directory.get_current_head() == EXPECTED_REVISION


@pytest.mark.parametrize("path", ["/health", "/ready"])
def test_neither_endpoint_requires_a_credential(settings: Settings, path: str) -> None:
    """Deliberate, and stated: an orchestrator probing them holds no merchant identity."""
    with TestClient(create_app(settings)) as client:
        assert client.get(path).status_code in {200, 503}
