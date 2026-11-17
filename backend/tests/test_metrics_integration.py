"""Prometheus metrics against the test database (PRD FR-013)."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from app.config.settings import Settings
from app.main import create_app
from tests.sample_data import NOW, Ids

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@pytest.fixture
def client(migrated_database: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(migrated_database)) as test_client:
        yield test_client


def scrape(client: TestClient) -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    response = client.get("/metrics")
    assert response.status_code == 200
    return {
        (sample.name, tuple(sorted(sample.labels.items()))): sample.value
        for family in text_string_to_metric_families(response.text)
        for sample in family.samples
    }


async def test_platform_state_is_read_from_the_database(client: TestClient, ids: Ids) -> None:
    metrics = scrape(client)

    assert metrics[("marketdata_database_up", ())] == 1
    # alpha failed once: degraded. beta has no enabled job, so it is not monitored.
    assert metrics[("marketdata_provider_health_status", (("provider", "alpha"),))] == 1
    assert ("marketdata_provider_consecutive_failures", (("provider", "beta"),)) not in metrics
    assert metrics[("marketdata_provider_consecutive_failures", (("provider", "alpha"),))] == 1

    job = (("job_name", "alpha_quotes"), ("kind", "quotes"), ("market_hours", "false"))
    assert metrics[("marketdata_job_failed", job)] == 0
    # The newest successful run finished 14 minutes before NOW.
    assert metrics[("marketdata_job_last_success_timestamp_seconds", job)] == (
        NOW.timestamp() - 14 * 60
    )
    assert metrics[("marketdata_job_expected_interval_seconds", job)] == 900

    ages = {
        labels: v for (name, labels), v in metrics.items() if name == "marketdata_data_age_seconds"
    }
    assert (("asset_type", "stock"), ("observation_type", "price"), ("provider", "alpha")) in ages
    assert metrics[("marketdata_quality_open_issues", (("kind", "gap"),))] == 0
    assert metrics[("marketdata_stale_series", (("provider", "alpha"),))] == 0
