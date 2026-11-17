"""Prometheus metrics without a database (PRD FR-013)."""

from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from prometheus_client.parser import text_string_to_metric_families
from sqlalchemy.exc import OperationalError

from app.monitoring import database_metrics
from app.monitoring.database_metrics import DataAge, DatabaseSnapshot, JobState, ProviderState
from app.providers.errors import (
    ProviderRateLimitedError,
    ProviderUnavailableError,
    RateBudgetExceededError,
)
from tests.conftest import FakeSession
from tests.providers_support import FakeClock, make_client


def value(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def samples(text: str) -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    return {
        (sample.name, tuple(sorted(sample.labels.items()))): sample.value
        for family in text_string_to_metric_families(text)
        for sample in family.samples
    }


# --- The endpoint ---------------------------------------------------------------------


def test_metrics_report_the_database_down_and_still_answer(
    client: TestClient, fake_session: FakeSession
) -> None:
    fake_session.error = OperationalError("SELECT 1", {}, Exception("connection refused"))

    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    parsed = samples(response.text)
    assert parsed[("marketdata_database_up", ())] == 0
    # Process metrics are there too; database-derived ones are not.
    assert any(name == "process_cpu_seconds_total" for name, _ in parsed)
    assert not any(name.startswith("marketdata_job_") for name, _ in parsed)


def test_metrics_are_not_part_of_the_api_schema(client: TestClient) -> None:
    assert "/metrics" not in client.get("/openapi.json").json()["paths"]


def test_api_requests_are_counted_by_route_template(client: TestClient) -> None:
    labels = {"method": "GET", "route": "/api/v1/health", "status": "200"}
    before = value("marketdata_http_requests_total", **labels)
    unmatched = value(
        "marketdata_http_requests_total", method="GET", route="unmatched", status="404"
    )

    client.get("/api/v1/health")
    client.get("/no/such/path/123")

    assert value("marketdata_http_requests_total", **labels) == before + 1
    assert (
        value("marketdata_http_requests_total", method="GET", route="unmatched", status="404")
        == unmatched + 1
    )
    assert (
        value(
            "marketdata_http_request_duration_seconds_count", method="GET", route="/api/v1/health"
        )
        >= 1
    )


def test_path_parameters_do_not_become_labels(client: TestClient) -> None:
    client.get("/api/v1/indicators/NOPE/observations")

    routes = {
        sample.labels["route"]
        for family in REGISTRY.collect()
        if family.name == "marketdata_http_requests"
        for sample in family.samples
    }
    assert "/api/v1/indicators/{code}/observations" in routes
    assert "/api/v1/indicators/NOPE/observations" not in routes


# --- Database-derived metrics ---------------------------------------------------------


def test_database_snapshot_is_rendered_as_gauges() -> None:
    snapshot = DatabaseSnapshot(
        providers=[ProviderState("ecb", "down", 4), ProviderState("fred", "unknown", 0)],
        jobs=[
            JobState(
                "ecb_daily_quotes",
                "quotes",
                False,
                True,
                datetime(2026, 11, 17, 16, 0, tzinfo=UTC),
                86400,
            ),
            JobState("data_quality_check", "quality", False, False, None, 900),
        ],
        data_age=[DataAge("ecb", "forex", "rate", 3600.5)],
        open_issues={"stale": 2, "gap": 0},
        stale_series={"ecb": 2, "fred": 0},
    )

    parsed = samples(database_metrics.render(snapshot).decode())

    assert parsed[("marketdata_database_up", ())] == 1
    assert parsed[("marketdata_provider_health_status", (("provider", "ecb"),))] == 2
    # A provider never used has no health yet.
    assert ("marketdata_provider_health_status", (("provider", "fred"),)) not in parsed
    assert parsed[("marketdata_provider_consecutive_failures", (("provider", "ecb"),))] == 4
    job = (("job_name", "ecb_daily_quotes"), ("kind", "quotes"), ("market_hours", "false"))
    assert parsed[("marketdata_job_failed", job)] == 1
    assert (
        parsed[("marketdata_job_last_success_timestamp_seconds", job)]
        == datetime(2026, 11, 17, 16, 0, tzinfo=UTC).timestamp()
    )
    assert parsed[("marketdata_job_expected_interval_seconds", job)] == 86400
    never = (("job_name", "data_quality_check"), ("kind", "quality"), ("market_hours", "false"))
    assert ("marketdata_job_last_success_timestamp_seconds", never) not in parsed
    age = (("asset_type", "forex"), ("observation_type", "rate"), ("provider", "ecb"))
    assert parsed[("marketdata_data_age_seconds", age)] == 3600.5
    assert parsed[("marketdata_quality_open_issues", (("kind", "stale"),))] == 2
    assert parsed[("marketdata_stale_series", (("provider", "ecb"),))] == 2


def test_an_unreadable_database_renders_only_its_availability() -> None:
    parsed = samples(database_metrics.render(None).decode())

    assert parsed == {("marketdata_database_up", ()): 0}


# --- Provider requests ------------------------------------------------------------------


@pytest.mark.anyio
async def test_provider_requests_are_counted_by_outcome() -> None:
    responses = iter(
        [
            httpx.Response(503),
            httpx.Response(429, headers={"Retry-After": "1"}),
            httpx.Response(200, json={}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    before = {
        o: value("marketdata_provider_requests_total", provider="example", outcome=o)
        for o in ("server_error", "rate_limited", "success")
    }
    retries = value("marketdata_provider_retries_total", provider="example")

    await make_client(handler, clock=FakeClock(), max_retries=2).get_json("/x")

    for outcome in before:
        assert (
            value("marketdata_provider_requests_total", provider="example", outcome=outcome)
            == before[outcome] + 1
        )
    assert value("marketdata_provider_retries_total", provider="example") == retries + 2
    assert value("marketdata_provider_request_duration_seconds_count", provider="example") >= 3


@pytest.mark.anyio
async def test_timeouts_and_connection_errors_have_their_own_outcomes() -> None:
    errors = iter([httpx.ReadTimeout("slow"), httpx.ConnectError("refused")])

    def handler(request: httpx.Request) -> httpx.Response:
        raise next(errors)

    before = (
        value("marketdata_provider_requests_total", provider="example", outcome="timeout"),
        value("marketdata_provider_requests_total", provider="example", outcome="connection_error"),
    )

    with pytest.raises(ProviderUnavailableError):
        await make_client(handler, clock=FakeClock(), max_retries=1).get_json("/x")

    assert (
        value("marketdata_provider_requests_total", provider="example", outcome="timeout"),
        value("marketdata_provider_requests_total", provider="example", outcome="connection_error"),
    ) == (before[0] + 1, before[1] + 1)


@pytest.mark.anyio
async def test_requests_refused_by_the_rate_budget_are_counted() -> None:
    before = value("marketdata_provider_budget_refusals_total", provider="example")
    client = make_client(
        lambda request: httpx.Response(200, json={}),
        rate_limits=[{"limit": 1, "period": "day"}],
        rate_limit_max_wait_seconds=0,
    )
    await client.get_json("/x")

    with pytest.raises(RateBudgetExceededError):
        await client.get_json("/x")

    assert value("marketdata_provider_budget_refusals_total", provider="example") == before + 1


@pytest.mark.anyio
async def test_a_rate_limited_failure_is_counted_once_per_attempt() -> None:
    before = value("marketdata_provider_requests_total", provider="example", outcome="rate_limited")

    with pytest.raises(ProviderRateLimitedError):
        await make_client(
            lambda request: httpx.Response(429, headers={"Retry-After": "1"}),
            clock=FakeClock(),
            max_retries=0,
        ).get_json("/x")

    assert (
        value("marketdata_provider_requests_total", provider="example", outcome="rate_limited")
        == before + 1
    )
