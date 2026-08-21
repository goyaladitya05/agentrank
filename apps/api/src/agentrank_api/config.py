"""Application configuration, read from the environment and validated at startup."""

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


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
