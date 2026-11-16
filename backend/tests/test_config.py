import json
import logging
import sys

import pytest
from pydantic import ValidationError

from app.config.logging import JsonFormatter
from app.config.settings import Settings


def test_settings_are_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_HOST", "db.internal")
    monkeypatch.setenv("POSTGRES_PORT", "6543")
    monkeypatch.setenv("POSTGRES_DB", "markets")
    monkeypatch.setenv("POSTGRES_USER", "collector")
    monkeypatch.setenv("POSTGRES_PASSWORD", "s3cret")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    settings = Settings(_env_file=None)

    url = settings.database_url()
    assert url.drivername == "postgresql+asyncpg"
    assert (url.host, url.port, url.database, url.username) == (
        "db.internal",
        6543,
        "markets",
        "collector",
    )
    assert url.password == "s3cret"
    assert settings.log_level == "DEBUG"


def test_database_password_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    monkeypatch.setenv("POSTGRES_DB", "markets")
    monkeypatch.setenv("POSTGRES_USER", "collector")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_password_is_not_revealed_by_repr() -> None:
    secret = "correct-horse-battery"
    settings = Settings(
        _env_file=None, postgres_db="markets", postgres_user="collector", postgres_password=secret
    )

    assert secret not in repr(settings)
    assert secret not in str(settings.database_url())


def test_json_formatter_emits_structured_fields() -> None:
    record = logging.LogRecord(
        "app.test", logging.WARNING, __file__, 1, "rate %s", ("limited",), None
    )
    record.provider = "example"

    payload = json.loads(JsonFormatter().format(record))

    assert payload["level"] == "WARNING"
    assert payload["logger"] == "app.test"
    assert payload["message"] == "rate limited"
    assert payload["provider"] == "example"
    assert payload["timestamp"].endswith("+00:00")


def test_json_formatter_includes_exception() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.getLogger("app.test").makeRecord(
            "app.test", logging.ERROR, __file__, 1, "failed", (), sys.exc_info()
        )

    payload = json.loads(JsonFormatter().format(record))

    assert "ValueError: boom" in payload["exception"]
