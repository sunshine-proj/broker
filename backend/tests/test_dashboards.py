"""Grafana dashboards: their structure, and every query against the database.

The SQL tests run each panel and variable query against the test database
holding the sample dataset, as Grafana's read-only role, after expanding the
Grafana macros and variables the dashboards use.
"""

import json
import re
import runpy
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config.settings import Settings
from app.services.reader_role import ReaderRoleError, ensure_reader_role
from tests.sample_data import Ids

REPOSITORY = Path(__file__).resolve().parents[2]
DASHBOARDS = REPOSITORY / "grafana" / "dashboards"
GENERATOR = REPOSITORY / "scripts" / "build_dashboards.py"

# A role used only by these tests. Roles are shared by every database on the
# server, so it must not be the application's own reader role.
TEST_ROLE = "grafana_reader_test"
TEST_ROLE_PASSWORD = "test-only: not a secret"


def boards() -> list[dict[str, Any]]:
    return [json.loads(path.read_text()) for path in sorted(DASHBOARDS.glob("*.json"))]


def panels(board: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield from board["panels"]


def queries(board: dict[str, Any]) -> Iterator[tuple[str, str]]:
    """The SQL queries; Prometheus queries are checked by scripts/check_dashboards.py."""
    for item in panels(board):
        for target in item.get("targets", []):
            if "rawSql" in target:
                yield item["title"], target["rawSql"]


# --- Structure ---------------------------------------------------------------------------


def test_dashboard_files_match_the_generator() -> None:
    generator = runpy.run_path(str(GENERATOR))
    expected = {
        f"{board['uid']}.json": generator["render"](board)
        for board in generator["all_dashboards"]()
    }

    actual = {path.name: path.read_text() for path in DASHBOARDS.glob("*.json")}

    assert actual == expected, "run scripts/build_dashboards.py"


def test_every_dashboard_in_the_prd_exists() -> None:
    titles = {board["title"] for board in boards()}

    assert titles == {
        "Global Market Overview",
        "Stock Market",
        "ETF Monitor",
        "Cryptocurrency Market",
        "Forex Monitor",
        "Government Bond Yields",
        "Interest Rates",
        "Economic Indicators",
        "Collector Monitoring",
        "Commodities",
        "Data Quality",
        "System Monitoring",
        "Provider Availability",
        "Collection Failures",
        "Market Analytics",
        "Watchlists and Groups",
        "Portfolio",
    }


@pytest.mark.parametrize("board", boards(), ids=lambda b: b["uid"])
def test_dashboard_structure(board: dict[str, Any]) -> None:
    assert board["tags"] == ["market-data"]
    assert board["timezone"] == "utc"
    assert board["time"]["to"] == "now"
    assert board["timepicker"]["refresh_intervals"]
    assert board["editable"] is False
    ids = [item["id"] for item in panels(board)]
    assert len(ids) == len(set(ids))
    for item in panels(board):
        position = item["gridPos"]
        assert position["x"] + position["w"] <= 24
        for target in item.get("targets", []):
            assert target["datasource"]["uid"] in ("marketdata", "prometheus")
            assert ("rawSql" in target) != ("expr" in target)
    # Every dashboard shows how current its data is.
    assert any("freshness" in item["title"].lower() for item in panels(board))


@pytest.mark.parametrize(
    ("uid", "required"),
    [
        (
            "market-overview",
            [
                "Powered by",
                "CoinGecko",
                "Binance",
                "Source: [European Central Bank",
                "subject to the [Terms of Use]",
                "The New York Fed is not responsible for publication of these rates",
                "U.S. Department of the Treasury",
                "FRED® API",
            ],
        ),
        ("stock-market", ["Twelve Data"]),
        ("etf-monitor", ["Twelve Data"]),
        ("crypto-market", ["**Powered by [CoinGecko]", "Binance"]),
        ("forex-monitor", ["Twelve Data", "European Central Bank"]),
        (
            "government-bond-yields",
            [
                "U.S. Department of the Treasury",
                "European Central Bank",
                "not endorsed or certified by the Federal Reserve Bank of St. Louis",
            ],
        ),
        (
            "interest-rates",
            ["European Central Bank", "DTCC Solutions LLC", "FRED® API"],
        ),
        ("commodities", ["Twelve Data"]),
        ("data-quality", ["CoinGecko", "Binance", "DTCC Solutions LLC", "FRED® API"]),
        ("economic-indicators", ["European Central Bank", "FRED® API"]),
        *(
            (
                uid,
                [
                    "Twelve Data",
                    "**Powered by [CoinGecko]",
                    "Binance",
                    "European Central Bank",
                    "DTCC Solutions LLC",
                    "U.S. Department of the Treasury",
                ],
            )
            for uid in ("market-analytics", "watchlists", "portfolio")
        ),
    ],
)
def test_dashboards_show_the_attribution_their_providers_require(
    uid: str, required: list[str]
) -> None:
    (board,) = [b for b in boards() if b["uid"] == uid]
    (sources,) = [p for p in panels(board) if p["title"] == "Data sources"]

    for phrase in required:
        assert phrase in sources["options"]["content"]
    # Shown first, so it is always in view.
    assert sources["gridPos"]["y"] == 0


def test_market_dashboards_have_variables() -> None:
    without = {b["uid"] for b in boards() if not b["templating"]["list"]}

    assert without == {"collector-monitoring", "system-monitoring"}


def test_series_keep_providers_apart() -> None:
    """Time series built from v_observation name each line by symbol and provider."""
    for board in boards():
        for item in panels(board):
            if item["type"] != "timeseries":
                continue
            for target in item.get("targets", []):
                if "FROM v_observation" in target.get("rawSql", ""):
                    assert "|| ' · ' || provider AS metric" in target["rawSql"], (
                        board["uid"],
                        item["title"],
                    )


# --- SQL ---------------------------------------------------------------------------------------

_TIME_FILTER = re.compile(r"\$__timeFilter\(([^)]+)\)")
_VARIABLE = re.compile(r"\$\{(\w+):sqlstring\}")


def expand(query: str, values: dict[str, list[str]]) -> str:
    """Expand the Grafana macros and variables the dashboards use, as Grafana would."""
    query = _TIME_FILTER.sub(r"\1 BETWEEN '2000-01-01T00:00:00Z' AND '2100-01-01T00:00:00Z'", query)
    query = query.replace("$__timeFrom()", "'2000-01-01T00:00:00Z'")
    query = query.replace("$__timeTo()", "'2100-01-01T00:00:00Z'")

    def variable(match: re.Match[str]) -> str:
        chosen = values.get(match.group(1)) or ["__no_value__"]
        return ", ".join("'" + value.replace("'", "''") + "'" for value in chosen)

    query = _VARIABLE.sub(variable, query)
    assert "$" not in query, f"unexpanded Grafana syntax in: {query}"
    return query


@pytest.fixture
async def reader(
    migrated_database: Settings, empty_database: AsyncEngine, ids: Ids
) -> AsyncIterator[asyncpg.Connection]:
    """A connection to the test database as a read-only role set up like Grafana's."""
    async with empty_database.begin() as connection:
        await ensure_reader_role(connection, TEST_ROLE, TEST_ROLE_PASSWORD)
    connection = await asyncpg.connect(
        host=migrated_database.postgres_host,
        port=migrated_database.postgres_port,
        user=TEST_ROLE,
        password=TEST_ROLE_PASSWORD,
        database=migrated_database.postgres_db,
    )
    try:
        yield connection
    finally:
        await connection.close()
        async with empty_database.begin() as admin:
            await admin.execute(sa.text(f"DROP OWNED BY {TEST_ROLE}"))
            await admin.execute(sa.text(f"DROP ROLE IF EXISTS {TEST_ROLE}"))


@pytest.mark.integration
@pytest.mark.anyio
async def test_every_dashboard_query_runs_as_the_read_only_role(
    reader: asyncpg.Connection,
) -> None:
    ran, returned_rows = 0, 0
    for board in boards():
        values: dict[str, list[str]] = {}
        for variable in board["templating"]["list"]:
            rows = await reader.fetch(expand(variable["query"], values))
            # A __value column, if present, is what the variable holds. A
            # single-value variable holds one, a multi-value one (or All) every one.
            options = [str(row["__value"] if "__value" in row else row[0]) for row in rows]
            values[variable["name"]] = options if variable["multi"] else options[:1]
        for title, query in queries(board):
            try:
                rows = await reader.fetch(expand(query, values))
            except asyncpg.PostgresError as exc:
                pytest.fail(f"{board['uid']} / {title}: {exc}")
            ran += 1
            returned_rows += bool(rows)

    assert ran >= 40
    # With the sample data most panels have something to show.
    assert returned_rows >= ran // 2


@pytest.mark.integration
@pytest.mark.anyio
async def test_reader_role_can_only_read(reader: asyncpg.Connection) -> None:
    assert await reader.fetchval("SELECT count(*) FROM v_latest_observation") > 0
    assert await reader.fetchval("SHOW default_transaction_read_only") == "on"
    assert await reader.fetchval("SHOW statement_timeout") == "30s"

    with pytest.raises(asyncpg.PostgresError):
        await reader.execute("DELETE FROM market_observation")
    with pytest.raises(asyncpg.PostgresError):
        await reader.execute(
            "SET TRANSACTION READ WRITE; INSERT INTO provider (name, base_url) VALUES ('x', 'https://x')"
        )
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await reader.fetch("SELECT * FROM alembic_version")


@pytest.mark.integration
@pytest.mark.anyio
async def test_reader_role_setup_is_repeatable_and_validated(
    reader: asyncpg.Connection, empty_database: AsyncEngine
) -> None:
    async with empty_database.begin() as connection:
        await ensure_reader_role(connection, TEST_ROLE, TEST_ROLE_PASSWORD)
        with pytest.raises(ReaderRoleError, match="invalid role name"):
            await ensure_reader_role(connection, "Robert'); DROP TABLE x;--", "pw")
        with pytest.raises(ReaderRoleError, match="needs a password"):
            await ensure_reader_role(connection, TEST_ROLE, "")

    assert await reader.fetchval("SELECT count(*) FROM v_provider_status") == 2


PROMETHEUS_METRICS = {
    "up",
    "ALERTS",
    "process_resident_memory_bytes",
    "process_cpu_seconds_total",
    "marketdata_database_up",
    "marketdata_http_requests_total",
    "marketdata_http_request_duration_seconds_bucket",
    "marketdata_provider_health_status",
    "marketdata_provider_requests_total",
    "marketdata_provider_request_duration_seconds_bucket",
    "marketdata_provider_retries_total",
    "marketdata_provider_budget_refusals_total",
    "marketdata_provider_rate_limiter_wait_seconds_total",
    "marketdata_collector_runs_total",
    "marketdata_job_last_success_timestamp_seconds",
    "marketdata_job_expected_interval_seconds",
    "marketdata_backup_last_success_timestamp_seconds",
}


_METRIC_NAME = re.compile(r"\b(?:marketdata_\w+|process_\w+|up|ALERTS)\b")


def test_prometheus_queries_use_metrics_the_platform_exposes() -> None:
    """A renamed metric must not leave a dashboard querying nothing."""
    import app.api.metrics  # noqa: F401  (defines the process metrics)
    from app.monitoring import database_metrics

    used = {
        name
        for board in boards()
        for item in panels(board)
        for target in item.get("targets", [])
        if "expr" in target
        for name in _METRIC_NAME.findall(target["expr"])
    }
    assert used == PROMETHEUS_METRICS

    # Every marketdata metric is defined by the backend: in the process registry
    # (counters as <name>_total, histograms as <name>_bucket) or the database ones.
    from prometheus_client import REGISTRY

    defined = set()
    for family in REGISTRY.collect():
        defined |= {family.name, f"{family.name}_total", f"{family.name}_bucket"}
    from datetime import UTC, datetime

    everything = database_metrics.DatabaseSnapshot(
        backup=database_metrics.BackupState(False, 86400, datetime.now(UTC), 1)
    )
    snapshot = database_metrics.render(everything).decode()
    defined |= {line.split()[2] for line in snapshot.splitlines() if line.startswith("# TYPE")}
    assert {name for name in used if name.startswith("marketdata_")} <= defined
