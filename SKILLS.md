# SKILLS.md — Development Conventions

Concrete conventions for implementing the Financial Market Data Platform.

[AGENTS.md](AGENTS.md) holds the rules and workflow, [PRD.md](PRD.md) the requirements and data model. This file says how code is written so that it meets them. Where this file and AGENTS.md disagree, AGENTS.md wins.

## 1. Code Layout

All backend code lives under `backend/app/`. One responsibility per package:

| Package | Contains | Must not contain |
| --- | --- | --- |
| `api/` | FastAPI routers, request parsing | Queries, provider calls |
| `schemas/` | Pydantic request, response, and normalized provider schemas | Database access |
| `providers/` | One module per provider adapter, plus the shared HTTP client | Database access |
| `collectors/` | Collection jobs and the scheduler | Provider response parsing |
| `services/` | Business logic, ingestion, validation | HTTP or SQL details |
| `repositories/` | All SQL and SQLAlchemy queries | Business rules |
| `models/` | SQLAlchemy models | Logic |
| `config/` | Settings and logging setup | Anything else |
| `database.py` | Engine, session factory, session dependency | Queries |
| `cli.py` | Administrative commands (`python -m app.cli`) | Business logic |

Alembic revisions live in `backend/migrations/versions/`. Tests live under `backend/tests/`.

## 2. Python

- Annotate every function signature.
- Use `async def` for anything that performs I/O.
- Read configuration only through the settings object; never call `os.environ` elsewhere.
- Pass dependencies in (FastAPI `Depends`, constructor arguments); avoid module-level state.
- Raise specific exception types. Catch exceptions only where they can be handled or translated.
- Log through the standard `logging` module with structured fields; never log credentials or full request URLs that contain a key.

## 3. Provider Adapters

- Each adapter subclasses `Provider`, registers itself with `@register_adapter`, and lives in its own module.
- Follow [docs/providers/README.md](docs/providers/README.md) for every integration.
- An adapter returns normalized schemas only. No provider response shape leaves the adapter.
- All requests go through the shared HTTP client, which applies timeouts, retries with exponential backoff, and the provider's rate limiter.
- Retry only transient failures (timeouts, connection errors, HTTP 429 and 5xx). On HTTP 429, honor `Retry-After` when present.
- Validate provider responses with Pydantic through `self._validate` before normalizing them.
- Never put a credential, or a URL that may contain one, in a log line or error message.
- Each adapter has a document under `docs/providers/` covering the items listed in AGENTS.md section 5.

## 4. Normalization

- Parse numbers as `Decimal` from the provider's string or raw JSON token; never pass through `float`. Use the helpers in `providers/parsing.py`.
- Convert every timestamp to timezone-aware UTC. A date-only value becomes midnight UTC of that date.
- Set `freshness`, `quote_kind`, and `unit` explicitly for every observation; do not rely on defaults.
- Keep price, NAV, yield, and rate as separate observation types.
- Never fill, interpolate, or carry forward a missing value.
- Put provider fields that have no normalized column into `metadata` when they are useful for auditing.

## 5. Database

- Change the schema only through Alembic migrations, one migration per logical change, each with a working downgrade.
- Use `NUMERIC` for financial values and `TIMESTAMPTZ` for timestamps.
- Write time-series rows with `INSERT ... ON CONFLICT DO NOTHING` on the natural key, following the deduplication rules in PRD section 7.
- Insert in batches, not row by row.
- Repositories never commit; the caller owns the transaction.
- Services depend on the protocols in `repositories/interfaces.py`, not on the SQL implementations.
- A migration spells out its own enumeration values and never imports them from the application.
- Bound every time-series query by a time range and an instrument or indicator.
- Add an index together with the query that needs it.
- Never update or delete a stored observation, candle or economic value. The data-quality checks may set `market_observation.quality_status`; nothing else changes a stored row.
- Hypertables are compressed after `COMPRESS_AFTER_DAYS`. Inserts and the `quality_status` update work on compressed chunks; keep other writes to recent data.

## 6. API

- Routers live under `/api/v1` and delegate to services.
- Follow the API conventions in PRD FR-006 for errors, pagination, time ranges, decimals, and timestamps.
- Validate all query parameters with Pydantic or FastAPI parameter types.
- Never return provider credentials or the provider `configuration` object unfiltered.

## 7. Collection and Scheduling

- A job run records a collector execution row at start and updates it at the end, whether it succeeds or fails.
- A job never overlaps with a previous run of itself.
- One provider's failure must not prevent other providers' jobs from running.
- Scheduled runs, manual triggers, and retries all draw from the same per-provider rate budget.
- Run a single scheduler instance (`SCHEDULER_ENABLED=true` in one process only). If the backend is ever scaled to several replicas, only one may run the scheduler.
- Add or change jobs in the seed file, never in the database; keep each provider within its rate budget, which the seed check enforces.
- Tests of anything that runs jobs commit for real and must clean up the tables they use.
- Report data problems as data-quality issues (PRD FR-012), never by correcting data. Give each issue a fingerprint that identifies the problem, so detecting it again updates the same issue.
- A new collecting job states its quality thresholds where the defaults do not fit (`stale_after_minutes`, `gap_after_minutes`, `business_days_only`), and a provider with collecting jobs gets a backfill job within its budget.

## 8. Grafana

- Provision data sources and dashboards from files under `grafana/`; do not rely on changes made in the Grafana interface.
- Change dashboards in `scripts/build_dashboards.py` and regenerate; never edit the JSON by hand.
- Query the reporting views (`v_observation`, `v_latest_observation`, `v_candle`, `v_indicator_value`, `v_data_freshness`, ...) rather than joining tables in each panel. Put a new reusable query in a view, through a migration.
- Use `${variable:sqlstring}` for variables in SQL, so values are quoted.
- Grafana connects with a read-only database role.
- Every dashboard showing provider data carries that provider's attribution.
- Every panel sets a unit, and every dashboard shows when its data was last updated.
- Label delayed, end-of-day, and reference data as such.

## 9. Metrics and Alerts

- Define process metrics in `app/monitoring/metrics.py` and platform-state metrics in `app/monitoring/database_metrics.py`. Prefix names with `marketdata_` and follow Prometheus naming (`_total` counters, `_seconds` durations).
- Keep label values bounded: route templates, provider and job names, fixed outcome names. Never a raw path, symbol, error text or id.
- Label job names `job_name`, never `job`.
- An alert rule comes with a `severity` label, a summary and description, a test in `prometheus/tests/alerts_test.yml`, and an entry in `docs/monitoring.md` and PRD FR-013.
- Run `scripts/check_monitoring.sh` after changing anything under `prometheus/` or `alertmanager/`.

## 10. Docker

- Pin image versions.
- Every service has a restart policy, a memory and CPU limit, and the shared log rotation (`logging: *logging`); every long-running service has a health check.
- Harden services with `x-hardened` (no new privileges, no capabilities) and `read_only: true` with a `/tmp` tmpfs, unless the image cannot run that way; say why in a comment.
- `docker-compose.yml` publishes no ports. Development ports go in `docker-compose.override.yml`, bound to `BIND_ADDRESS`; production exposure goes through the proxy in `docker-compose.prod.yml`.
- Put a service on the `internal` network, and on `egress` only if it must reach the internet.
- Persistent data lives in named volumes.

## 11. Testing

- Unit tests need no network and no database.
- Mock provider HTTP calls with `httpx.MockTransport` and recorded fixture responses stored under `backend/tests/fixtures/`.
- Inject a fake clock and sleep into anything that waits; tests never sleep for real.
- Integration tests are marked `integration` and run against a separate test database on the Compose `db` service. SQLite is not a substitute, because the schema uses TimescaleDB.
- Database tests use the `session` fixture, which rolls back everything the test wrote, including commits.
- Async tests are marked `anyio`; no extra pytest plugin is needed.
- Each adapter test covers a successful response, a malformed response, a rate-limit response, and a timeout.
- Fixtures are deterministic: no current time, no random values.

## 12. Security

- Secrets come from environment variables only, and appear in `.env.example` as empty values. A new secret is added to `GENERATED` in `scripts/generate_env.py` and to the checks in `scripts/check_env.py`.
- The running backend connects as its restricted role (read, insert, update). Code that needs more belongs in migrations, the seed file or the command line, which run as the owner.
- Use HTTPS for all provider connections.
- Treat provider responses as untrusted input.

## 13. Quality Checks

Run from `backend/` before reporting a task as complete:

```bash
ruff check .
ruff format --check .
pytest
```

All new functionality includes tests. Prefer readable code over clever abstractions.
