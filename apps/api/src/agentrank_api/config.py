"""Application configuration, read from the environment and validated at startup.

Two rules a private-beta deployment depends on, and both live here.

**A production process is configured by its environment and by nothing else.** `.env` is read
only in the environments that are meant to have one, so a file left in a working directory
cannot quietly define how a real deployment behaves. A production process that finds one ignores
it and says so, because a `.env` baked into an image is worth knowing about even when it changed
nothing.

That rule is only worth anything if the file cannot also decide which environment this is.
`AGENTRANK_ENV` is an ordinary field on the model below, so a `.env` containing
`AGENTRANK_ENV=production` would otherwise produce the worst possible process: one that calls
itself a deployment, reports that it read no file, was never asked to state its database, and
took every value including its provider credentials from the file. The environment is therefore
read from the process environment before the model exists and checked against the model
afterwards, and a file that tried to move it is refused by name.

**A production process states its database rather than defaulting into one.** The localhost
defaults below are a convenience for a developer and a hazard for a deployment: a process that
silently reached for `localhost` would come up healthy against nothing. In production every
connection variable has to be set.

What is required and what is merely absent are different answers everywhere in this file. A
missing provider credential is a capability this process does not have, and it starts. A missing
database password is not.
"""

import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Self

from pydantic import Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

from agentrank_api.importer.network import PUBLIC_ONLY, AddressPolicy, PermittedNetworks

log = logging.getLogger(__name__)

# The environments that are allowed to be configured from a file on disk. Everything else is a
# deployment, and a deployment is configured by its environment.
FILE_CONFIGURED_ENVIRONMENTS = frozenset({"development", "ci", "test"})

ENVIRONMENT_VARIABLE = "AGENTRANK_ENV"

ENV_FILE = ".env"

# The database variables a deployment has to state rather than inherit from a default written for
# a developer's laptop. A process that came up against `localhost` because nobody set these would
# be a process reporting itself healthy against the wrong database, or against none.
REQUIRED_IN_DEPLOYMENT = ("POSTGRES_HOST", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD")

# Razorpay marks the mode in the key identifier itself. Requiring the test marker is how this
# project refuses live mode structurally rather than by intention: a live key cannot be loaded,
# so no code path can reach a live payment, and there is no flag anywhere that turns this off.
RAZORPAY_TEST_KEY_PREFIX = "rzp_test_"

# The documented base for the Razorpay REST API. Configurable so a test can point the transport
# at a local stub, and defaulted so nothing has to be set to reach the real one.
RAZORPAY_API_BASE_URL = "https://api.razorpay.com/v1"


@dataclass(frozen=True, slots=True)
class RazorpayCredentials:
    """One Razorpay Test Mode key pair, and the only place the secret is carried.

    The secret stays a `SecretStr` all the way to the moment it is used, so a stray repr, an
    exception message, a log line or a serialized settings object shows a mask rather than the
    value. It is unwrapped in exactly two places: the HTTP transport, which needs it for basic
    authentication, and the signature verifier, which needs it as an HMAC key.

    `key_id` is public. Standard Checkout runs in the browser and cannot work without it, so it
    is returned by the preparation endpoint on purpose. The secret is never returned by
    anything, never appears in an audit payload, and has no accessor that would make doing so
    convenient.
    """

    key_id: str
    key_secret: SecretStr
    api_base_url: str
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class OpenAICredentials:
    """A proper application-runtime OpenAI API key, never a developer subscription token."""

    api_key: SecretStr


@dataclass(frozen=True, slots=True)
class GeminiCredentials:
    """A runtime Gemini API key, unwrapped only by the isolated worker."""

    api_key: SecretStr


class Settings(BaseSettings):
    """Runtime configuration.

    Every value comes from the environment. Missing required values raise at startup
    with the offending variable named, rather than failing later on first use.
    """

    model_config = SettingsConfigDict(
        # Chosen per process by `get_settings` rather than fixed here, because whether this
        # deployment may be configured from a file is itself a configuration question.
        env_file=None,
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    environment: str = Field(default="development", alias="AGENTRANK_ENV")
    log_level: str = Field(default="info", alias="AGENTRANK_LOG_LEVEL")

    postgres_host: str = Field(default="localhost", alias="POSTGRES_HOST")
    postgres_port: int = Field(default=5432, alias="POSTGRES_PORT")
    postgres_db: str = Field(default="agentrank", alias="POSTGRES_DB")
    postgres_user: str = Field(default="agentrank", alias="POSTGRES_USER")
    postgres_password: SecretStr = Field(alias="POSTGRES_PASSWORD")
    postgres_connect_timeout: int = Field(default=5, alias="POSTGRES_CONNECT_TIMEOUT")

    # Razorpay Test Mode, and only Test Mode. Both halves are optional because the application
    # runs perfectly well without them: every existing payment path uses the deterministic fake
    # provider, and the interactive Razorpay bridge is refused with a named reason when these
    # are absent rather than failing at startup. An integration that is not configured should
    # not stop a merchant from using the rest of the API.
    razorpay_key_id: str | None = Field(default=None, alias="RAZORPAY_KEY_ID")
    razorpay_key_secret: SecretStr | None = Field(default=None, alias="RAZORPAY_KEY_SECRET")
    razorpay_api_base_url: str = Field(default=RAZORPAY_API_BASE_URL, alias="RAZORPAY_API_BASE_URL")
    razorpay_timeout_seconds: float = Field(default=15.0, alias="RAZORPAY_TIMEOUT_SECONDS")
    openai_api_key: SecretStr | None = Field(default=None, alias="OPENAI_API_KEY")
    gemini_api_key: SecretStr | None = Field(default=None, alias="GEMINI_API_KEY")

    # Networks the merchant page importer may reach in addition to the public internet, as a
    # comma separated list of CIDR blocks. Empty everywhere except a developer's machine and the
    # test suite, where it is how a synthetic merchant fixture served on loopback is reachable at
    # all, and refused outright in any environment that is a deployment. See the validator below.
    import_allowed_networks: str = Field(default="", alias="AGENTRANK_IMPORT_ALLOWED_NETWORKS")

    @model_validator(mode="after")
    def razorpay_is_test_mode_or_absent(self) -> Self:
        """Refuse a half configured integration, and refuse a live key outright.

        Half configured is refused because the alternative is discovering it on the first
        request that needs the missing half, which for a payment integration is the worst
        possible moment. The variable that is missing is named, so the fix is obvious.

        A live key is refused because this project has no live mode. The marker is part of the
        key identifier Razorpay issues, so this is a structural block rather than a policy: a
        live credential cannot be loaded into the process at all, and there is no environment
        variable, request field or command line flag that relaxes it. Removing this check would
        be the deliberate act of enabling real money, which is exactly what it should take.
        """
        if (self.razorpay_key_id is None) != (self.razorpay_key_secret is None):
            missing = "RAZORPAY_KEY_SECRET" if self.razorpay_key_id else "RAZORPAY_KEY_ID"
            raise ValueError(f"{missing} must be set alongside the other half of the key pair")
        if self.razorpay_key_id is not None and not self.razorpay_key_id.startswith(
            RAZORPAY_TEST_KEY_PREFIX
        ):
            raise ValueError(
                f"RAZORPAY_KEY_ID must be a Test Mode key beginning {RAZORPAY_TEST_KEY_PREFIX!r};"
                " this project has no live mode"
            )
        if self.razorpay_timeout_seconds <= 0:
            raise ValueError("RAZORPAY_TIMEOUT_SECONDS must be positive")
        return self

    @model_validator(mode="after")
    def import_allowance_is_not_a_deployment_setting(self) -> Self:
        """Refuse an importer network allowance outside a development, CI or test process.

        The merchant page importer connects to an address a request body chose, and the boundary
        that makes that safe is "globally routable addresses only". This variable is the one way
        to widen it, and widening it in a deployment would turn one authenticated endpoint into a
        way to reach whatever that deployment can reach: its own database, its metadata service,
        anything on its private network.

        A structural block rather than a policy, in the same shape as the Razorpay live key
        refusal above. There is no request field, no header and no flag that relaxes it, and no
        combination of environment variables that produces a production process with a widened
        importer. Removing this check would be the deliberate act of enabling it.

        The environment has to have been *stated*, not merely defaulted, and that half was
        missing. `AGENTRANK_ENV` defaults to `development`, so a process where nobody set it, a
        chart that dropped it or an image that cleared it, would have been authorised to widen the
        importer by an absence. That is the one shape a structural block must not have, because
        every other guard keyed on the same variable fails in the same silence. The Razorpay
        refusal does not have it: `rzp_test_` is checked on the value, and there is no unset that
        means permitted.

        Parsed here rather than at first use so that a malformed block is a startup failure
        naming the variable, not a refused import naming nothing.
        """
        if not self.import_allowed_networks.strip():
            return self
        if not self.file_configured or ENVIRONMENT_VARIABLE not in os.environ:
            raise ValueError(
                f"AGENTRANK_IMPORT_ALLOWED_NETWORKS requires {ENVIRONMENT_VARIABLE} to be set in"
                " the process environment to development, ci or test; the merchant page importer"
                " reaches public internet addresses only"
            )
        PermittedNetworks.parse(self.import_allowed_networks)
        return self

    @property
    def import_address_policy(self) -> AddressPolicy:
        """Which addresses this process may fetch a merchant page from.

        The public-only policy in every deployment, because the validator above refuses any other
        answer there. A development or test process may widen it, which is how the synthetic
        merchant fixture the test suite serves on loopback is reachable.
        """
        if not self.import_allowed_networks.strip():
            return PUBLIC_ONLY
        return PermittedNetworks.parse(self.import_allowed_networks).policy()

    @property
    def razorpay(self) -> RazorpayCredentials | None:
        """The Razorpay credentials, or None when the integration is not configured.

        None rather than raising, because an unconfigured integration is an ordinary state and
        the refusal belongs at the endpoint that needs it, where it can name itself to a
        caller. The validator above has already guaranteed that both halves are present
        together and that the key is a Test Mode key.
        """
        if self.razorpay_key_id is None or self.razorpay_key_secret is None:
            return None
        return RazorpayCredentials(
            key_id=self.razorpay_key_id,
            key_secret=self.razorpay_key_secret,
            api_base_url=self.razorpay_api_base_url.rstrip("/"),
            timeout_seconds=self.razorpay_timeout_seconds,
        )

    @property
    def openai(self) -> OpenAICredentials | None:
        """Runtime provider credentials, or None when live LLM runs are not configured.

        A blank value counts as absent. `Settings` reads a `.env` file, so the only way a
        deployment that has a key on disk can say it does not want one is to set the variable
        empty in the environment, and a credential of zero length is not a credential in any
        case: it would be carried all the way to a provider that then refuses it.
        """
        if self.openai_api_key is None or not self.openai_api_key.get_secret_value().strip():
            return None
        return OpenAICredentials(api_key=self.openai_api_key)

    @property
    def gemini(self) -> GeminiCredentials | None:
        """Runtime Gemini credentials, or None when live Gemini runs are not configured.

        Blank counts as absent, for the same reasons as above.
        """
        if self.gemini_api_key is None or not self.gemini_api_key.get_secret_value().strip():
            return None
        return GeminiCredentials(api_key=self.gemini_api_key)

    @property
    def file_configured(self) -> bool:
        """Whether this environment is one that may be configured from a file on disk."""
        return self.environment in FILE_CONFIGURED_ENVIRONMENTS

    def capability_report(self) -> dict[str, bool]:
        """Which optional capabilities this process holds, as presence and never as values.

        Absence is an answer rather than a failure for every one of these. A process with no
        OpenAI credential runs benchmark launches frozen to every other executor; a process with
        no Razorpay pair serves every endpoint except the interactive checkout bridge, which
        refuses by name. What this exists for is a startup line an operator can read to see
        which of those a process is, without reading a single configured value.
        """
        return {
            "openai": self.openai is not None,
            "gemini": self.gemini is not None,
            "razorpay_test_mode": self.razorpay is not None,
            # Presence rather than the blocks themselves, and true is the interesting value: it
            # says this process may fetch merchant pages from somewhere other than the public
            # internet, which no deployment can be.
            "import_networks_widened": bool(self.import_allowed_networks.strip()),
        }

    @property
    def database_url(self) -> URL:
        """Async SQLAlchemy URL.

        Built with URL.create so that special characters in the password are escaped
        correctly instead of corrupting a hand assembled DSN string.
        """
        return URL.create(
            drivername="postgresql+psycopg",
            username=self.postgres_user,
            password=self.postgres_password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )


def _field_problems(invalid: ValidationError) -> list[str]:
    """What was wrong with a settings load, as field names and reasons and never as values.

    Pydantic names the alias in `loc` for an aliased field, which is the environment variable an
    operator has to go and fix. `msg` is the validator's own sentence; every validator in this
    module writes its own and none of them interpolates a configured value.
    """
    problems = []
    for error in invalid.errors():
        where = ".".join(str(part) for part in error.get("loc", ())) or "configuration"
        problems.append(f"{where}: {error.get('msg', 'is not valid')}")
    return problems


def _deployment_gaps(environment: str) -> list[str]:
    """Database variables a deployment must state, and has not.

    Read from the process environment rather than from a built `Settings`, because the whole
    point is to catch a value that came from a default rather than from the deployment. Once the
    model is built the two are indistinguishable.
    """
    if environment in FILE_CONFIGURED_ENVIRONMENTS:
        return []
    return [name for name in REQUIRED_IN_DEPLOYMENT if not os.environ.get(name, "").strip()]


def build_settings() -> Settings:
    """Load configuration for this process, from the sources this environment is allowed.

    The environment name is read from the process environment directly, before the model exists,
    because it decides where the rest of the configuration may come from. A deployment that named
    itself in a `.env` would be deciding that question with the answer.

    A `.env` present in a deployment is ignored and reported. Reported rather than refused: the
    file may be a developer's checkout that somebody is running a one-off command in, and
    refusing would make an operator's shell useless for a reason that changed nothing. What it
    must not do is take effect.
    """
    environment = os.environ.get(ENVIRONMENT_VARIABLE, "development").strip() or "development"
    file_configured = environment in FILE_CONFIGURED_ENVIRONMENTS
    if not file_configured and Path(ENV_FILE).is_file():
        log.warning(
            "ignoring %s: %s=%s is a deployment and is configured by its environment",
            ENV_FILE,
            ENVIRONMENT_VARIABLE,
            environment,
        )

    gaps = _deployment_gaps(environment)
    if gaps:
        raise ValueError(
            f"{', '.join(gaps)} must be set when {ENVIRONMENT_VARIABLE}={environment}."
            " A deployment states its database rather than inheriting a developer default."
        )

    # Passed as keyword arguments through a dict, which is also what keeps mypy quiet:
    # pydantic-settings fills required fields from the environment, and a literal call would be
    # reported as missing the password it is going to find there.
    overrides: dict[str, Any] = {"_env_file": ENV_FILE if file_configured else None}
    try:
        settings = Settings(**overrides)
    except ValidationError as invalid:
        # Re-raised without pydantic's rendering. Its message embeds a truncated repr of the
        # whole input dictionary, and the truncation keeps the tail, so the last environment
        # sourced value, a provider key or the database password, is printed verbatim into
        # whatever reads a failed boot. What a caller needs is which fields were wrong.
        raise ValueError(
            "configuration is not usable: "
            + "; ".join(sorted(_field_problems(invalid)))
            + f". Values are not shown. See {ENV_FILE}.example for what each variable is."
        ) from None

    # The one field a file may not decide, checked after the fact because the model is where a
    # file would have got at it.
    if settings.environment != environment:
        raise ValueError(
            f"{ENVIRONMENT_VARIABLE} must come from the process environment, not from"
            f" {ENV_FILE}. This process was started as {environment!r} and the file names"
            " something else, so which rules apply to it would depend on which layer you read."
        )
    return settings


@lru_cache
def get_settings() -> Settings:
    """Return the process wide settings, loaded once."""
    return build_settings()
