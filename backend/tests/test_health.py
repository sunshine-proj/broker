from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from tests.conftest import FakeSession


def test_health_reports_ok_when_database_answers(client: TestClient, app: FastAPI) -> None:
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": app.version, "database": "ok"}


def test_health_reports_degraded_when_database_is_down(
    client: TestClient, app: FastAPI, fake_session: FakeSession
) -> None:
    fake_session.error = OperationalError("SELECT 1", {}, ConnectionRefusedError("refused"))

    response = client.get("/api/v1/health")

    assert response.status_code == 503
    assert response.json() == {
        "status": "degraded",
        "version": app.version,
        "database": "unavailable",
    }


def test_health_is_not_served_outside_the_versioned_path(client: TestClient) -> None:
    assert client.get("/health").status_code == 404


def test_openapi_documents_the_health_endpoint(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()

    assert "/api/v1/health" in schema["paths"]
