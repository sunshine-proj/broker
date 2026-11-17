"""Application settings, read from environment variables."""

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

# Repository-root .env, used when running outside Docker. Real environment
# variables always take precedence over it.
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_ENV_FILE = _BACKEND_DIR.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_ENV_FILE, extra="ignore")

    app_name: str = "Financial Market Data Platform"
    app_version: str = "0.1.0"

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "text"] = "json"

    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str
    postgres_user: str
    postgres_password: SecretStr
    # Database used by integration tests; never the application's own database.
    postgres_test_db: str = "marketdata_test"
    database_connect_timeout_seconds: float = 5.0

    # Instruments, provider mappings, indicators and jobs to track (PRD FR-004).
    seed_file: Path = _BACKEND_DIR / "seed.toml"

    # Run the collection jobs on their schedules in this process. Enable it in
    # exactly one process per deployment.
    scheduler_enabled: bool = False
    # Required in the X-API-Key header to trigger a job manually through the
    # API. Manual triggers through the API are disabled while it is unset.
    admin_api_key: SecretStr | None = None

    # Read-only database role for Grafana, set up by `python -m app.cli reader-role`.
    grafana_db_user: str = "grafana_reader"
    grafana_db_password: SecretStr | None = None
    # The backend's own role (read, insert, update), set up by `python -m app.cli app-role`.
    # The backend container connects as it; migrations and the CLI use POSTGRES_USER.
    backend_db_user: str = "marketdata_app"
    backend_db_password: SecretStr | None = None

    # Private holdings for portfolio tracking (PRD FR-016), loaded at every
    # start by `python -m app.cli portfolio`. A missing file means no
    # portfolios. Never commit it. In Docker, /portfolio is mounted from the
    # repository's portfolio/ directory.
    portfolio_file: Path = _BACKEND_DIR.parent / "portfolio" / "portfolio.toml"

    # Time-series storage (PRD section 6, Retention), applied by
    # `python -m app.cli storage-policies`. Unset compression means never;
    # unset retention means keep forever.
    compress_after_days: int | None = Field(default=30, gt=0)
    observation_retention_days: int | None = Field(default=None, gt=0)
    candle_retention_days: int | None = Field(default=None, gt=0)

    @field_validator(
        "compress_after_days",
        "observation_retention_days",
        "candle_retention_days",
        mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("admin_api_key", "grafana_db_password", "backend_db_password", mode="before")
    @classmethod
    def _blank_key_is_unset(cls, value: object) -> object:
        # Docker Compose passes an unset variable as an empty string. An empty
        # key must disable the trigger, not become a key that anyone can send.
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value

    def database_url(self, database: str | None = None) -> URL:
        """SQLAlchemy URL for the application database, or for `database` if given."""
        return URL.create(
            "postgresql+asyncpg",
            username=self.postgres_user,
            password=self.postgres_password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=database or self.postgres_db,
        )

    def credential(self, env_name: str) -> SecretStr | None:
        """A provider credential, looked up by the name of its environment variable.

        Provider configuration stores only that name (PRD FR-005). Outside
        Docker the repository-root .env is consulted as well.
        """
        value = os.environ.get(env_name) or _dotenv_values().get(env_name)
        return SecretStr(value) if value else None


@lru_cache
def _dotenv_values() -> dict[str, str | None]:
    return dict(dotenv_values(_ENV_FILE)) if _ENV_FILE.is_file() else {}


@lru_cache
def get_settings() -> Settings:
    return Settings()
