"""Request validation of the analytics endpoints (PRD FR-014). No database."""

import pytest
from fastapi.testclient import TestClient


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/analytics/performance", {"currency": "E"}),
        ("/analytics/performance", {"currency": "EUR; DROP"}),
        (
            "/analytics/returns",
            {"instrument_id": "00000000-0000-0000-0000-000000000001", "period": "day"},
        ),
        ("/analytics/daily", {}),
        ("/analytics/relative", {"instrument_id": "not-a-uuid"}),
    ],
)
def test_invalid_parameters_are_rejected(client: TestClient, path: str, params: dict) -> None:
    response = client.get(f"/api/v1{path}", params=params)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_relative_performance_needs_something_to_compare(client: TestClient) -> None:
    response = client.get("/api/v1/analytics/relative")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "nothing_to_compare"


def test_endpoints_are_documented(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]

    for path in (
        "/api/v1/analytics/performance",
        "/api/v1/analytics/daily",
        "/api/v1/analytics/returns",
        "/api/v1/analytics/relative",
        "/api/v1/analytics/spreads",
        "/api/v1/analytics/spreads/{name}",
        "/api/v1/analytics/yield-curves",
        "/api/v1/watchlists",
        "/api/v1/watchlists/{name}",
        "/api/v1/groups",
        "/api/v1/groups/{name}",
        "/api/v1/portfolios",
        "/api/v1/portfolios/{name}",
        "/api/v1/portfolios/{name}/history",
    ):
        assert set(paths[path]) == {"get"}, path
