"""Application configuration, read from the environment and validated at startup."""

from dataclasses import dataclass
from functools import lru_cache
from typing import Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

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
        env_file=".env",
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


@lru_cache
def get_settings() -> Settings:
    """Return the process wide settings, loaded once."""
    # mypy cannot see that pydantic-settings supplies required fields from the
    # environment, so it reports the required password as a missing argument.
    return Settings()  # type: ignore[call-arg]
