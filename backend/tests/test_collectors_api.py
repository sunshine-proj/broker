"""POST /api/v1/collectors/{job}/run, with the scheduler replaced by a fake."""

import uuid
from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.collectors.runner import JobAlreadyRunningError, JobDisabledError, JobNotFoundError
from app.config.settings import Settings
from app.database import get_session
from app.main import create_app
from tests.conftest import FakeSession, signed_in

KEY = "admin-key-for-tests"
EXECUTION = uuid.UUID("00000000-0000-0000-0000-00000000abcd")


class FakeScheduler:
    def __init__(self) -> None:
        self.triggered: list[str] = []
        self.error: Exception | None = None

    async def trigger_manual(self, job: str) -> SimpleNamespace:
        if self.error is not None:
            raise self.error
        self.triggered.append(job)
        return SimpleNamespace(execution_id=EXECUTION)

    async def shutdown(self) -> None:
        pass


def client_for(settings: Settings) -> tuple[TestClient, FakeScheduler]:
    app: FastAPI = signed_in(create_app(settings))
    app.dependency_overrides[get_session] = lambda: FakeSession()
    fake = FakeScheduler()
    client = TestClient(app, raise_server_exceptions=False)
    client.__enter__()
    app.state.scheduler = fake
    return client, fake


@pytest.fixture
def api(settings: Settings) -> Iterator[tuple[TestClient, FakeScheduler]]:
    client, fake = client_for(settings.model_copy(update={"admin_api_key": SecretStr(KEY)}))
    try:
        yield client, fake
    finally:
        client.__exit__(None, None, None)


def run(client: TestClient, job: str = "ecb_daily_quotes", key: str | None = KEY):
    headers = {"X-API-Key": key} if key is not None else {}
    return client.post(f"/api/v1/collectors/{job}/run", headers=headers)


def test_valid_key_starts_the_job(api: tuple[TestClient, FakeScheduler]) -> None:
    client, fake = api

    response = run(client)

    assert response.status_code == 202
    assert response.json() == {
        "execution_id": str(EXECUTION),
        "job": "ecb_daily_quotes",
        "status": "running",
        "trigger": "manual",
    }
    assert fake.triggered == ["ecb_daily_quotes"]


@pytest.mark.parametrize("key", [None, "wrong-key", ""])
def test_missing_or_wrong_key_is_rejected(
    api: tuple[TestClient, FakeScheduler], key: str | None
) -> None:
    client, fake = api

    response = run(client, key=key)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"
    assert fake.triggered == []


def test_trigger_is_disabled_without_a_configured_key(settings: Settings) -> None:
    client, fake = client_for(settings)
    try:
        response = run(client, key="anything")
    finally:
        client.__exit__(None, None, None)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "manual_trigger_disabled"
    assert fake.triggered == []


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (JobNotFoundError("x"), 404, "job_not_found"),
        (JobDisabledError("x"), 409, "job_disabled"),
        (JobAlreadyRunningError("x"), 409, "job_running"),
    ],
)
def test_job_errors_use_the_standard_error_shape(
    api: tuple[TestClient, FakeScheduler], error: Exception, status: int, code: str
) -> None:
    client, fake = api
    fake.error = error

    response = run(client)

    assert response.status_code == status
    assert response.json()["error"]["code"] == code


def test_key_never_appears_in_the_openapi_schema(api: tuple[TestClient, FakeScheduler]) -> None:
    client, _ = api

    schema = client.get("/openapi.json").text

    assert "/api/v1/collectors/{job}/run" in schema
    assert KEY not in schema


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_admin_key_disables_the_trigger(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    # Docker Compose passes an unset ADMIN_API_KEY as an empty string.
    monkeypatch.setenv("ADMIN_API_KEY", blank)
    configured = Settings(
        _env_file=None, postgres_db="unit", postgres_user="unit", postgres_password="unit"
    )
    assert configured.admin_api_key is None

    client, fake = client_for(configured)
    try:
        response = client.post("/api/v1/collectors/ecb_daily_quotes/run", headers={"X-API-Key": ""})
    finally:
        client.__exit__(None, None, None)

    assert response.status_code == 403
    assert fake.triggered == []


def test_empty_header_never_matches(api: tuple[TestClient, FakeScheduler]) -> None:
    client, fake = api

    response = client.post("/api/v1/collectors/ecb_daily_quotes/run", headers={"X-API-Key": ""})

    assert response.status_code == 401
    assert fake.triggered == []
