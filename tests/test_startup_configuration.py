"""What a process refuses to start with, and what it merely does without.

Two failure modes this repository has to keep apart. A missing provider credential is a
capability this process does not have, and it starts and serves everything else. A missing
database is not a capability, and a deployment that came up against a developer's localhost
default would be a process reporting itself healthy against the wrong database or against none.

The `.env` rule is the other half. A file on disk may configure a development machine and may not
configure a deployment, because a file that happened to be in a working directory would otherwise
be deciding how a real process behaves.
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from agentrank_api.config import (
    ENV_FILE,
    ENVIRONMENT_VARIABLE,
    FILE_CONFIGURED_ENVIRONMENTS,
    REQUIRED_IN_DEPLOYMENT,
    build_settings,
)

# Enough of a database to build settings with, and synthetic in the only sense that matters:
# nothing here connects to anything.
DEPLOYMENT_DATABASE = {
    "POSTGRES_HOST": "db.internal",
    "POSTGRES_DB": "agentrank",
    "POSTGRES_USER": "agentrank",
    "POSTGRES_PASSWORD": "not-a-real-password",
}

MANAGED = (
    ENVIRONMENT_VARIABLE,
    *REQUIRED_IN_DEPLOYMENT,
    "POSTGRES_PORT",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "RAZORPAY_KEY_ID",
    "RAZORPAY_KEY_SECRET",
    "AGENTRANK_IMPORT_ALLOWED_NETWORKS",
)


@pytest.fixture
def environment() -> Iterator[dict[str, str]]:
    """A clean process environment for the variables under test, restored afterwards.

    Cleared rather than added to, because the point of most of these is what happens when a
    variable is absent, and a developer machine with a real `.env` and real keys would otherwise
    be testing its own configuration.
    """
    saved = {name: os.environ.get(name) for name in MANAGED}
    for name in MANAGED:
        os.environ.pop(name, None)
    try:
        yield dict(DEPLOYMENT_DATABASE)
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@pytest.fixture
def elsewhere(tmp_path: Path) -> Iterator[Path]:
    """A working directory with no `.env` in it.

    So that what these tests exercise is file discovery rather than whatever happens to be in the
    repository root when they run.
    """
    before = Path.cwd()
    os.chdir(tmp_path)
    try:
        yield tmp_path
    finally:
        os.chdir(before)


def test_a_deployment_must_state_its_database_rather_than_inherit_a_default(
    environment: dict[str, str], elsewhere: Path
) -> None:
    """Every one of them, named, and one at a time so no single default can slip through."""
    del elsewhere
    os.environ[ENVIRONMENT_VARIABLE] = "production"

    for missing in REQUIRED_IN_DEPLOYMENT:
        for name, value in environment.items():
            os.environ[name] = value
        os.environ.pop(missing, None)

        with pytest.raises(ValueError) as refused:
            build_settings()
        assert missing in str(refused.value)


def test_a_deployment_with_a_stated_database_starts(
    environment: dict[str, str], elsewhere: Path
) -> None:
    del elsewhere
    os.environ[ENVIRONMENT_VARIABLE] = "production"
    os.environ.update(environment)

    settings = build_settings()

    assert settings.environment == "production"
    assert settings.postgres_host == "db.internal"
    assert settings.file_configured is False


@pytest.mark.parametrize("name", sorted(FILE_CONFIGURED_ENVIRONMENTS))
def test_a_development_environment_keeps_its_defaults(
    environment: dict[str, str], elsewhere: Path, name: str
) -> None:
    """The localhost defaults are a convenience for a developer and stay one.

    A password is still required: it has no default anywhere, in any environment, because there
    is no password this repository could pick that would be safe to fall back to.
    """
    del elsewhere
    os.environ[ENVIRONMENT_VARIABLE] = name
    os.environ["POSTGRES_PASSWORD"] = environment["POSTGRES_PASSWORD"]

    settings = build_settings()

    assert settings.postgres_host == "localhost"
    assert settings.file_configured is True


def test_a_deployment_ignores_an_env_file_it_finds(
    environment: dict[str, str], elsewhere: Path
) -> None:
    """A file left in a working directory must not decide how a deployment behaves."""
    (elsewhere / ENV_FILE).write_text("POSTGRES_HOST=from-the-file\nOPENAI_API_KEY=from-the-file\n")
    os.environ[ENVIRONMENT_VARIABLE] = "production"
    os.environ.update(environment)

    settings = build_settings()

    assert settings.postgres_host == "db.internal"
    assert settings.openai is None


def test_a_development_process_does_read_its_env_file(
    environment: dict[str, str], elsewhere: Path
) -> None:
    """The same file, in the environment that is meant to have one."""
    (elsewhere / ENV_FILE).write_text(
        f"POSTGRES_HOST=from-the-file\nPOSTGRES_PASSWORD={environment['POSTGRES_PASSWORD']}\n"
    )
    os.environ[ENVIRONMENT_VARIABLE] = "development"

    assert build_settings().postgres_host == "from-the-file"


def test_a_missing_provider_credential_is_a_capability_and_not_a_failure(
    environment: dict[str, str], elsewhere: Path
) -> None:
    del elsewhere
    os.environ[ENVIRONMENT_VARIABLE] = "production"
    os.environ.update(environment)

    settings = build_settings()

    assert settings.capability_report() == {
        "openai": False,
        "gemini": False,
        "razorpay_test_mode": False,
        "import_networks_widened": False,
    }
    assert settings.openai is None
    assert settings.gemini is None
    assert settings.razorpay is None


def test_a_configured_capability_is_reported_as_present_and_never_as_a_value(
    environment: dict[str, str], elsewhere: Path
) -> None:
    del elsewhere
    os.environ[ENVIRONMENT_VARIABLE] = "production"
    os.environ.update(environment)
    os.environ["OPENAI_API_KEY"] = "not-a-real-openai-key"

    report = build_settings().capability_report()

    assert report["openai"] is True
    assert report["gemini"] is False
    assert all(isinstance(value, bool) for value in report.values())


def test_a_blank_provider_credential_is_absence_rather_than_a_credential(
    environment: dict[str, str], elsewhere: Path
) -> None:
    del elsewhere
    os.environ[ENVIRONMENT_VARIABLE] = "production"
    os.environ.update(environment)
    os.environ["GEMINI_API_KEY"] = "   "

    assert build_settings().gemini is None


def test_a_half_configured_razorpay_pair_stops_the_process_and_names_the_missing_half(
    environment: dict[str, str], elsewhere: Path
) -> None:
    """Discovering it on the first payment request is the worst possible moment."""
    del elsewhere
    os.environ[ENVIRONMENT_VARIABLE] = "production"
    os.environ.update(environment)
    os.environ["RAZORPAY_KEY_ID"] = "rzp_test_synthetic"

    with pytest.raises(ValueError) as refused:
        build_settings()
    assert "RAZORPAY_KEY_SECRET" in str(refused.value)


def test_a_live_razorpay_key_cannot_be_loaded_at_all(
    environment: dict[str, str], elsewhere: Path
) -> None:
    """This project has no live mode, and that is structural rather than a policy."""
    del elsewhere
    os.environ[ENVIRONMENT_VARIABLE] = "production"
    os.environ.update(environment)
    os.environ["RAZORPAY_KEY_ID"] = "rzp_live_synthetic"
    os.environ["RAZORPAY_KEY_SECRET"] = "not-a-real-secret"

    with pytest.raises(ValueError) as refused:
        build_settings()
    assert "rzp_test_" in str(refused.value)


def test_a_configuration_failure_names_variables_and_never_values(
    environment: dict[str, str], elsewhere: Path
) -> None:
    """An operator has to be able to paste a startup failure into a ticket."""
    del elsewhere
    os.environ[ENVIRONMENT_VARIABLE] = "production"
    os.environ.update(environment)
    os.environ["RAZORPAY_KEY_ID"] = "rzp_live_synthetic"
    os.environ["RAZORPAY_KEY_SECRET"] = "the-secret-that-must-not-be-printed"

    with pytest.raises(ValueError) as refused:
        build_settings()
    assert "the-secret-that-must-not-be-printed" not in str(refused.value)


def test_a_deployment_refuses_to_start_with_an_importer_network_allowance(
    environment: dict[str, str], elsewhere: Path
) -> None:
    """The merchant page importer reaches the public internet in a deployment, and only that.

    A structural block rather than a policy, in the same shape as the refusal of a live payment
    key. The variable that widens the importer's address policy is how a development machine
    reaches a synthetic storefront on loopback; in a deployment the same variable would turn one
    authenticated endpoint into a way to reach that deployment's own private network, so a process
    holding one does not start.
    """
    del elsewhere
    os.environ[ENVIRONMENT_VARIABLE] = "production"
    os.environ.update(environment)
    os.environ["AGENTRANK_IMPORT_ALLOWED_NETWORKS"] = "127.0.0.0/8"

    with pytest.raises(ValueError, match="AGENTRANK_IMPORT_ALLOWED_NETWORKS"):
        build_settings()


def test_a_development_process_may_reach_the_networks_it_names(
    environment: dict[str, str], elsewhere: Path
) -> None:
    del elsewhere
    os.environ[ENVIRONMENT_VARIABLE] = "development"
    os.environ.update(environment)
    os.environ["AGENTRANK_IMPORT_ALLOWED_NETWORKS"] = "127.0.0.0/8"

    settings = build_settings()

    assert settings.capability_report()["import_networks_widened"] is True
    assert settings.import_address_policy.permits_port(8080)


def test_a_network_allowance_that_is_not_cidr_is_a_startup_failure(
    environment: dict[str, str], elsewhere: Path
) -> None:
    """Parsed at startup so a malformed block names the variable rather than a refused import."""
    del elsewhere
    os.environ[ENVIRONMENT_VARIABLE] = "development"
    os.environ.update(environment)
    os.environ["AGENTRANK_IMPORT_ALLOWED_NETWORKS"] = "not-a-network"

    with pytest.raises(ValueError, match="CIDR"):
        build_settings()


def test_a_process_that_never_stated_its_environment_may_not_widen_the_importer(
    environment: dict[str, str], elsewhere: Path
) -> None:
    """The half of the structural block that an absence used to satisfy.

    `AGENTRANK_ENV` defaults to development, so a process where nobody set it, a chart that
    dropped it or an image that cleared it, was authorised to point the merchant page importer at
    a private network by an absence. Every other guard keyed on that variable fails in the same
    silence, so the allowance requires it to have been stated.
    """
    del elsewhere
    os.environ.pop(ENVIRONMENT_VARIABLE, None)
    os.environ.update(environment)
    os.environ["AGENTRANK_IMPORT_ALLOWED_NETWORKS"] = "0.0.0.0/0"

    with pytest.raises(ValueError, match=ENVIRONMENT_VARIABLE):
        build_settings()
