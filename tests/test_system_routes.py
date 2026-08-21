"""Behavior of the liveness and readiness endpoints."""

from agentrank_api.config import Settings
from agentrank_api.main import create_app
from fastapi.testclient import TestClient


def test_health_reports_the_process_is_running(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_reports_database_connection(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["components"] == [{"name": "database", "status": "connected", "detail": None}]


def test_ready_fails_when_the_database_is_unreachable(settings: Settings, unused_port: int) -> None:
    unreachable = settings.model_copy(update={"postgres_port": unused_port})

    with TestClient(create_app(unreachable)) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["components"][0]["status"] == "unavailable"


def test_health_does_not_depend_on_the_database(settings: Settings, unused_port: int) -> None:
    unreachable = settings.model_copy(update={"postgres_port": unused_port})

    with TestClient(create_app(unreachable)) as client:
        response = client.get("/health")

    assert response.status_code == 200
