"""Shared test fixtures.

Backend tests run against a real PostgreSQL 18 instance, never SQLite. Locally that is
the Docker Compose service, and in CI it is a service container.
"""

import socket

import pytest
from agentrank_api.config import Settings, get_settings


@pytest.fixture(scope="session")
def settings() -> Settings:
    """Configuration for the local or CI database."""
    return get_settings()


@pytest.fixture
def unused_port() -> int:
    """A TCP port with nothing listening on it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
