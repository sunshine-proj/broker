from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.errors import AppError


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    @app.get("/test/app-error")
    async def app_error() -> None:
        raise AppError("instrument_not_found", "No such instrument.", 404, {"id": "abc"})

    @app.get("/test/crash")
    async def crash() -> None:
        raise RuntimeError("secret internal detail")

    @app.get("/test/items/{item_id}")
    async def item(item_id: int) -> dict[str, int]:
        return {"item_id": item_id}

    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def test_app_error_uses_standard_shape(client: TestClient) -> None:
    response = client.get("/test/app-error")

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "instrument_not_found",
            "message": "No such instrument.",
            "details": {"id": "abc"},
        }
    }


def test_unknown_route_uses_standard_shape(client: TestClient) -> None:
    response = client.get("/api/v1/does-not-exist")

    assert response.status_code == 404
    assert response.json() == {"error": {"code": "not_found", "message": "Not Found"}}


def test_wrong_method_uses_standard_shape(client: TestClient) -> None:
    response = client.post("/api/v1/health")

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"


def test_validation_error_uses_standard_shape_without_echoing_input(client: TestClient) -> None:
    response = client.get("/test/items/not-a-number")

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"]["errors"][0]["loc"] == ["path", "item_id"]
    assert "not-a-number" not in response.text


def test_unexpected_error_hides_internal_details(client: TestClient) -> None:
    response = client.get("/test/crash")

    assert response.status_code == 500
    assert response.json() == {
        "error": {"code": "internal_error", "message": "An unexpected error occurred."}
    }
    assert "secret internal detail" not in response.text
