# Financial Market Data Platform

A self-hosted financial market data aggregation platform supporting stocks, ETFs, cryptocurrencies, forex, government bond yields, interest rates, commodities, market indices, and economic indicators.

**Status:** Phases 0 to 7 are complete (the MVP). Phase 8 (additional market coverage) is complete except for the items its providers' free plans do not cover. Phases 9 (data quality and reliability) and 10 (monitoring and alerting) are complete; see [ROADMAP.md](ROADMAP.md). Collection runs automatically on schedules defined in the seed file, and the collected data is served by the REST API and shown in Grafana dashboards. Seven providers are integrated:

- **Twelve Data:** US stocks, ETFs, forex, spot gold.
- **CoinGecko:** crypto prices and market data, averaged across exchanges.
- **Binance:** exchange prices of crypto pairs, every minute.
- **ECB:** reference exchange rates, €STR, key rates, euro-area yield curves, euro-area statistics.
- **New York Fed:** SOFR, EFFR and OBFR.
- **US Treasury:** the daily Treasury par yield curve.
- **FRED:** US Treasury yields, federal funds rate, US statistics. Needs a key, and its jobs are disabled; see the licensing note in [docs/providers/fred.md](docs/providers/fred.md).

Collect financial data from multiple providers, normalize it, store historical observations, and visualize everything through Grafana.

## Features (planned)

- Multiple financial data providers
- Stocks, ETFs, crypto, forex, government bond yields, interest rates, commodities, indices, and economic indicators
- Historical market data
- Centralized PostgreSQL/TimescaleDB storage
- Automated data collection
- REST API
- Grafana dashboards
- Docker Compose deployment
- Extensible provider architecture

## Architecture

```text
Financial Data Providers
        |
        v
Python Data Collectors
        |
        v
Data Normalization
        |
        v
PostgreSQL + TimescaleDB
        |
        +----------> FastAPI
        |
        +----------> Grafana
```

## Technology Stack

- Python 3.12+
- FastAPI
- PostgreSQL
- TimescaleDB
- SQLAlchemy (async, asyncpg)
- Alembic
- APScheduler
- Grafana
- Docker Compose

## Data Providers

| Provider | Data | Key | Document |
| --- | --- | --- | --- |
| Twelve Data | US stocks, ETFs, intraday forex, spot gold | Required | [twelve_data.md](docs/providers/twelve_data.md) |
| CoinGecko | Cryptocurrency prices and market data | Optional | [coingecko.md](docs/providers/coingecko.md) |
| Binance | Exchange-specific cryptocurrency prices and candles | None | [binance.md](docs/providers/binance.md) |
| ECB | Reference exchange rates, €STR, key rates, euro-area yield curves and economic data | None | [ecb.md](docs/providers/ecb.md) |
| New York Fed | SOFR, EFFR, OBFR | None | [nyfed.md](docs/providers/nyfed.md) |
| US Treasury | Daily Treasury par yield curve | None | [us_treasury.md](docs/providers/us_treasury.md) |
| FRED | US Treasury yields, federal funds rate, US economic indicators | Required | [fred.md](docs/providers/fred.md) |

Not covered by the free plans in use: stocks outside the US, market indices, silver, crude oil and natural gas. Twelve Data offers them from its paid Grow plan.

API access and coverage depend on provider subscriptions and licensing. Default collection intervals are sized to fit the providers' free plans; see the data refresh strategy in [PRD.md](PRD.md).

## Prerequisites

- Docker
- Docker Compose
- Git

API keys are required for providers that require authentication.

## Installation

### 1. Clone the repository

```bash
git clone <repository-url>
cd <repository-directory>
```

### 2. Configure the environment

```bash
cp .env.example .env
```

Configure the required credentials and application settings.

### 3. Start services

```bash
docker compose up -d --build
```

Database migrations and the seed file are applied automatically by the one-shot `migrate` service before the backend starts.

### 4. Check service status

```bash
docker compose ps
curl http://localhost:8000/api/v1/health
```

`migrate` is expected to show as exited with code 0.

### 5. Access applications

| Service | URL |
| --- | --- |
| FastAPI | <http://localhost:8000> |
| API documentation | <http://localhost:8000/docs> |
| Grafana | <http://localhost:3000> |
| Prometheus | <http://localhost:9090> |
| Alertmanager | <http://localhost:9093> |
| Backend metrics | <http://localhost:8000/metrics> |

Default development credentials are documented in `.env.example` (Grafana: `admin` / `change_me`) and must be changed before deployment.

All published ports bind to `127.0.0.1` by default, so PostgreSQL (`localhost:5432`) is reachable only from the host itself.

Data is stored in the named volumes `db_data`, `grafana_data`, `prometheus_data` and `alertmanager_data` and survives `docker compose down`. Use `docker compose down -v` only if you intend to delete it.

## Development

Requires Python 3.12+ (on Debian/Ubuntu also `python3.12-venv`).

Set up the backend development environment:

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

`requirements.txt` holds pinned runtime dependencies; `requirements-dev.txt` adds test and lint tooling.

Run the application (the database must be running: `docker compose up -d db`):

```bash
alembic upgrade head
python -m app.cli seed
uvicorn app.main:create_app --factory --reload
```

Outside Docker, settings are read from the repository-root `.env`, which points at the database published on `localhost`.

Run linting, the format check, and all tests:

```bash
scripts/check.sh
```

Arguments are passed to pytest, for example `scripts/check.sh -m "not integration"` to run only the tests that need no database.

Integration tests use a separate database (`POSTGRES_TEST_DB`), created automatically on the same server. They are skipped when the database is unreachable; set `REQUIRE_DATABASE=1` to make that a failure instead.

Without a local Python environment, run the same checks in a container attached to the Compose network:

```bash
docker compose up -d db
scripts/check-docker.sh
```

The backend design is described in [docs/architecture.md](docs/architecture.md).

## Project Structure

```text
backend/
    app/
        api/
        collectors/
        monitoring/  # Prometheus metrics
        providers/
        services/
        models/
        repositories/
        schemas/
        config/
        database.py
        main.py
    migrations/     # Alembic revisions
    seed.toml       # what the platform tracks
    tests/

grafana/
    dashboards/     # generated by scripts/build_dashboards.py
    provisioning/   # datasources and dashboard provider
prometheus/         # scrape configuration, alert rules and their tests
alertmanager/       # alert routing; no notification channel by default

scripts/            # check.sh, check-docker.sh, check_monitoring.sh,
                    # build_dashboards.py, check_dashboards.py
docs/
    architecture.md
    monitoring.md   # metrics, alerts and notifications
    providers/      # integration requirements, and one document per provider (from Phase 4)
```

## API

Base path: `/api/v1`. Interactive documentation: <http://localhost:8000/docs> (Swagger UI), schema at `/openapi.json`.

| Method | Endpoint | Description |
| --- | --- | --- |
| GET | `/health` | Health status |
| GET | `/instruments` | Instruments, with each provider's symbol. Filters: `asset_type`, `provider`, `search`, `enabled` |
| GET | `/instruments/{id}` | Instrument details |
| GET | `/prices/latest` | Latest value of each instrument from each provider. Filters: `asset_type`, `symbol`, `instrument_id`, `provider`, `observation_type` |
| GET | `/prices/history` | Values of one instrument over time (`instrument_id`; `observation_type`, `provider`, `start`, `end`) |
| GET | `/ohlcv` | Candles of one instrument (`instrument_id`; `interval`, `provider`, `start`, `end`) |
| GET | `/forex/rates` | Latest currency rates. Filters: `base`, `quote`, `provider`, `quote_kind` |
| GET | `/bonds/yields` | Latest government bond yields. Filters: `country`, `provider` |
| GET | `/indicators` | Economic indicators with their latest value. Filters: `country`, `provider`, `frequency`, `enabled` |
| GET | `/indicators/{code}/observations` | Values of one indicator (`start`, `end`, `include_revisions`) |
| GET | `/providers` | Providers, their rate limits and health |
| GET | `/collectors/status` | Collection jobs: schedule, last run, outcome, next run |
| GET | `/collectors/executions` | Run history, newest first. Filters: `job`, `status` |
| GET | `/quality/issues` | Data-quality issues, most recently seen first. Filters: `kind`, `status`, `provider`, `symbol`, `start`, `end` (last seen; default the last 7 days) |
| POST | `/collectors/{job}/run` | Run a job now (requires `X-API-Key`; see [Collecting Data](#collecting-data)) |

Conventions:

- **Pages:** lists return `{"items": [...], "limit": 100, "offset": 0, "has_more": false}`. `limit` is at most 1000. Fetch the next page with `offset` = `offset` + `limit`.
- **Values:** sent as strings (`"1.1052"`), so no precision is lost.
- **Timestamps:** ISO 8601 in UTC with a `Z`. `start` is inclusive and `end` exclusive; a timestamp without an offset is read as UTC.
- **Time series:** returned oldest first. Default ranges are 30 days for price history, 90 days for candles and 10 years for indicators.
- **Several providers:** values from different providers are never merged. Each comes back as its own item, with its `provider`, `freshness` (`real_time`, `delayed`, `end_of_day`) and `quote_kind` (`traded`, `indicative`, `reference`).
- **Errors:** `{"error": {"code": "...", "message": "...", "details": {...}}}`. For example, `instrument_not_found` (404), `unknown_provider` (422), `invalid_time_range` (422), `validation_error` (422).

Read endpoints need no authentication; they rely on the backend being bound to `127.0.0.1` (see `BIND_ADDRESS`).

Examples:

```bash
curl "http://localhost:8000/api/v1/prices/latest?asset_type=crypto"
curl "http://localhost:8000/api/v1/forex/rates?base=EUR&quote=USD"
curl "http://localhost:8000/api/v1/indicators/US_CPI/observations?include_revisions=true"
```

## Grafana

Open <http://localhost:3000> and sign in with `GRAFANA_ADMIN_USER` and `GRAFANA_ADMIN_PASSWORD`. The dashboards are in the **Market Data** folder:

| Dashboard | Shows |
| --- | --- |
| Global Market Overview | Key values, relative performance and the latest value of everything tracked |
| Stock Market, ETF Monitor | Latest prices, daily change, price and performance charts, daily candles |
| Cryptocurrency Market | Prices, market capitalization, volume, supply and dominance; Binance exchange prices beside CoinGecko's averages |
| Forex Monitor | Market and ECB reference rates, side by side |
| Government Bond Yields | Yield curve and yield history of the US Treasury curve and the euro area curves; choose a curve and a provider |
| Interest Rates | €STR, ECB key rates, SOFR, EFFR, OBFR and the federal funds rate |
| Commodities | Spot gold price and daily candles |
| Economic Indicators | Latest values, history and revisions of each indicator |
| Collector Monitoring | Provider health, jobs, runs per hour, run durations, collection latency, failures |
| Data Quality | Open issues, gaps and their backfill, conflicts and spikes, provider comparison, suspect values, collection latency |
| System Monitoring | Availability of the backend, database, Prometheus and Alertmanager; firing alerts; API traffic, errors and latency |
| Provider Availability | Health, success rate, requests by outcome, latency, retries and rate limiting per provider |
| Collection Failures | Firing collection alerts, failed runs, time since each job's last success, errors |

Every dashboard has:

- **Variables** to choose instruments, and a time range.
- **A data freshness table:** the age of the newest data, coloured by what is normal for that data.
- **The attribution its providers' licences require.**
- **Labels on every value:** values from different providers are shown separately, labelled real-time, delayed or end-of-day, and traded, indicative or reference.

How it is set up:

- **Read-only access:** Grafana connects as a read-only database role (`GRAFANA_DB_USER`), which the `migrate` service creates and updates on every start. The role can only read, and each query is stopped after 30 seconds.
- **Provisioning:** the datasource and dashboards are provisioned from `grafana/` at start-up. Changes made in the Grafana interface are not kept.
- **Generated dashboards:** the dashboards are generated by [scripts/build_dashboards.py](scripts/build_dashboards.py). To change one, edit the script and regenerate:

  ```bash
  python3 scripts/build_dashboards.py
  ```

  Grafana picks up the change within a minute. A test fails if the JSON and the script disagree.
- **Checking a running Grafana:** this runs every dashboard query through Grafana itself and reports any that fail:

  ```bash
  set -a; . ./.env; set +a; python3 scripts/check_dashboards.py
  ```

**Licensing:** Twelve Data's free plan is described as "internal non-display usage only". Whether showing its data on a personal dashboard is allowed is unresolved; see [docs/providers/twelve_data.md](docs/providers/twelve_data.md).

## Configuration

Application configuration is provided through environment variables, read from `.env` by Docker Compose.

| Variable | Default | Description |
| --- | --- | --- |
| `BIND_ADDRESS` | `127.0.0.1` | Host address the published ports bind to |
| `BACKEND_PORT` | `8000` | Host port for the FastAPI backend |
| `GRAFANA_PORT` | `3000` | Host port for Grafana |
| `POSTGRES_PORT` | `5432` | Host port for PostgreSQL |
| `POSTGRES_DB` | `marketdata` | Database name |
| `POSTGRES_USER` | `marketdata` | Database user |
| `POSTGRES_PASSWORD` | `change_me` | Database password (required) |
| `POSTGRES_TEST_DB` | `marketdata_test` | Database used only by integration tests |
| `POSTGRES_HOST` | `localhost` | Database host; Docker Compose sets it to `db` for the backend |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL` |
| `LOG_FORMAT` | `json` | `json` (one object per line) or `text` |
| `SCHEDULER_ENABLED` | `false` (`true` in the backend container) | Run the collection jobs on their schedules in this process. Enable it in one process only. |
| `ADMIN_API_KEY` | (empty) | Key for triggering a job through the API (`X-API-Key` header). Empty disables API triggers. |
| `SEED_FILE` | `backend/seed.toml` | Seed file to apply; in Docker, a path inside the backend image |
| `FRED_API_KEY` | (empty) | [FRED](https://fredaccount.stlouisfed.org/apikeys) API key. Required for FRED collection. |
| `COINGECKO_API_KEY` | (empty) | Optional [CoinGecko](https://www.coingecko.com/en/api) Demo key. Without it, CoinGecko's keyless API is used, at about 5 calls a minute. |
| `TWELVEDATA_API_KEY` | (empty) | [Twelve Data](https://twelvedata.com/pricing) API key. The free Basic plan is enough for the default instruments. Without it, Twelve Data collection fails and is reported as a provider failure. |
| `GRAFANA_ADMIN_USER` | `admin` | Grafana administrator user |
| `GRAFANA_ADMIN_PASSWORD` | `change_me` | Grafana administrator password (required) |
| `GRAFANA_DB_USER` | `grafana_reader` | Read-only database role Grafana connects with |
| `GRAFANA_DB_PASSWORD` | `change_me_reader` | Its password (required). Changing it updates the role on the next start. |
| `COMPRESS_AFTER_DAYS` | `30` | Compress market observations and candles older than this many days. Empty: never. |
| `OBSERVATION_RETENTION_DAYS` | (empty) | Drop market observations older than this many days, for good. Empty keeps everything. |
| `CANDLE_RETENTION_DAYS` | (empty) | The same for candles. Economic observations are never dropped. |
| `PROMETHEUS_PORT` | `9090` | Host port for Prometheus |
| `ALERTMANAGER_PORT` | `9093` | Host port for Alertmanager |
| `PROMETHEUS_RETENTION` | `15d` | How long Prometheus keeps metrics |

Database and Grafana credentials are applied when their volumes are first created; changing them later in `.env` does not alter an existing volume.

The storage settings are applied by the `migrate` service at every start (`python -m app.cli storage-policies`), so changing one and restarting is enough. A provider's timeouts, retries, rate limits and the name of its key variable are set in `backend/seed.toml`; adding a provider is described in [docs/providers/README.md](docs/providers/README.md). Credentials are read from the environment only and are never stored in the database.

Never commit API credentials or production secrets.

## Tracked Instruments

What the platform tracks is defined in [backend/seed.toml](backend/seed.toml): exchanges, providers, instruments, the symbol each provider uses for an instrument, and economic indicators. The file documents its own format.

- It is applied on every startup, and applying it again changes nothing.
- An entry is matched on its natural key and updated in place.
- Removing an entry does not delete it, because stored history refers to it. Set `enabled = false` to stop tracking something.
- An invalid file stops the startup, and the `migrate` service log says what is wrong.

After editing the file, rebuild and restart: `docker compose up -d --build`.

The file currently tracks:

- 7 US stocks, 7 ETFs, 5 forex pairs and spot gold through Twelve Data;
- 10 cryptocurrencies through CoinGecko;
- 5 Tether-quoted crypto pairs through Binance;
- 14 euro reference exchange rates, €STR, two ECB key rates, 8 euro-area government yields and 7 euro-area economic indicators through the ECB;
- SOFR, EFFR and OBFR through the New York Fed;
- 11 US Treasury par yields through the US Treasury;
- 6 US Treasury yields, the effective federal funds rate and 6 US economic indicators through FRED (disabled).

Instruments carry metadata such as country, FIGI, ETF issuer and focus, pair base and quote, and yield-curve maturity. Each provider's share is sized to its free plan. To find the symbol a provider uses:

```bash
docker compose run --rm migrate python -m app.cli search twelve_data "apple"
```

## Collecting Data

Collection runs automatically. The backend container runs a scheduler that starts each job defined under `[[jobs]]` in [backend/seed.toml](backend/seed.toml):

| Job | Schedule (UTC) |
| --- | --- |
| `twelve_data_listed_quotes` | Every 15 minutes, during New York trading hours |
| `twelve_data_forex_quotes` | Every 30 minutes |
| `twelve_data_commodity_quotes` | Every 30 minutes |
| `twelve_data_daily_candles` | Daily at 00:40, retried once after an hour if it fails |
| `coingecko_quotes` | Every 5 minutes |
| `coingecko_dominance` | Every 2 hours |
| `ecb_daily_quotes` | Daily at 16:00, with retry |
| `ecb_indicators` | Daily at 10:00, with retry |
| `binance_quotes` | Every minute |
| `binance_daily_candles` | Daily at 00:10, with retry |
| `nyfed_quotes` | Daily at 20:30, after the New York Fed's same-day revision window, with retry |
| `us_treasury_quotes` | Daily at 23:30, with retry |
| `data_quality_check` | Every 15 minutes; reads the database only (see [Data Quality](#data-quality)) |
| `twelve_data_backfill`, `coingecko_backfill`, `binance_backfill`, `ecb_backfill`, `nyfed_backfill`, `us_treasury_backfill` | Daily between 03:10 and 04:00, a few requests each |
| `fred_daily_quotes`, `fred_indicators` | Disabled until `FRED_API_KEY` is set and FRED's terms are settled |

When the seed file is applied, the jobs are checked against every provider's rate limits. A configuration that would exceed one stops the startup, and the `migrate` log names the provider and the jobs.

See the jobs, their last outcome and their next run:

```bash
docker compose run --rm migrate python -m app.cli jobs
```

Run a job now. From the command line:

```bash
docker compose run --rm migrate python -m app.cli run ecb_daily_quotes
```

Or through the API, with `ADMIN_API_KEY` set:

```bash
curl -X POST -H "X-API-Key: $ADMIN_API_KEY" http://localhost:8000/api/v1/collectors/ecb_daily_quotes/run
```

A job never runs twice at the same time. Every run is recorded in the `collector_execution` table with its trigger, status, record count and error.

Collections can also be run directly, outside the jobs and without execution history:

```bash
docker compose run --rm migrate python -m app.cli collect quotes twelve_data
docker compose run --rm migrate python -m app.cli collect candles twelve_data --interval 1day --days 30
docker compose run --rm migrate python -m app.cli collect quotes coingecko
docker compose run --rm migrate python -m app.cli collect history coingecko --days 90
docker compose run --rm migrate python -m app.cli collect quotes ecb
docker compose run --rm migrate python -m app.cli collect history ecb --days 365
docker compose run --rm migrate python -m app.cli collect indicators ecb
docker compose run --rm migrate python -m app.cli collect quotes fred
docker compose run --rm migrate python -m app.cli collect indicators fred
```

Each run uses provider credits:

- **Twelve Data:** 20 for quotes or candles of the default instruments. The free plan allows 8 a minute, so a run takes about two minutes.
- **CoinGecko:** 2 for current values and dominance, and 10 for history, one per coin. History is limited to the past 365 days and stops 2 days short of today, because CoinGecko may still revise recent values.

- **ECB:** no key and no published limit. A collection takes a few requests.
- **FRED:** one request per series, within 120 a minute.

CoinGecko's free plans require attribution ("Powered by CoinGecko") wherever its data is displayed, ECB data must cite the ECB as its source, and applications using the FRED API must show FRED's notice. FRED's terms also prohibit archiving its content, which is unresolved; see [docs/providers/fred.md](docs/providers/fred.md). Provider limits and licensing are documented in [docs/providers/](docs/providers/).

## Data Quality

The `data_quality_check` job looks for problems in the stored data every 15 minutes and records each one as an issue (PRD FR-012). It never changes or deletes a value.

| Issue | Meaning |
| --- | --- |
| `stale` | A series has nothing newer than its job allows: by default three intervals, or four days for a daily job. Market-hours series are checked only while their session runs. Closes when a newer value arrives. |
| `gap` | Two consecutive values are further apart than the job allows (`gap_after_minutes`; candles by default their interval), within the last 30 days of what the job collected. Closed by backfill. |
| `conflict` | A provider re-sent a stored value differently. The stored value is kept and marked suspect. |
| `spike` | A price moved more than 20 %, or a rate or yield more than 1 point, from the value before. The value is marked suspect. |
| `discrepancy` | Two providers' values of the same instrument at the same time differ by more than 2 % (or 0.1 points). Closes when they agree again. |

- **Backfill:** each provider's backfill job re-fetches a few gaps a day from the provider's history, within its rate budget. A gap is then `filled`, `no_data` (the provider has nothing for it, such as an exchange holiday) or `unrecoverable` (the provider has no history for that data, as for Binance prices).
- **Thresholds:** set per job in `backend/seed.toml` (`stale_after_minutes`, `gap_after_minutes`, `business_days_only`) and on the quality job (`spike_percent`, `spike_points`, `discrepancy_percent`, `discrepancy_points`).
- **Where to see them:** the Data Quality dashboard, `GET /api/v1/quality/issues`, and the `quality_status` of each observation in the API.

Run the checks now:

```bash
docker compose run --rm migrate python -m app.cli run data_quality_check
```

## Monitoring and Alerts

Prometheus scrapes the backend's metrics (`/metrics`) every 30 seconds and evaluates the alert rules; Alertmanager groups and routes the alerts. The alerts cover the backend and database being down, API errors, failing or overdue collection jobs, providers down, degraded, rate limited or out of budget, and stale data.

No notification channel is configured: alerts appear in Alertmanager, Prometheus and the System Monitoring and Collection Failures dashboards. [docs/monitoring.md](docs/monitoring.md) lists every metric and alert with what to do about it, and shows how to add email or webhook notifications.

Check the Prometheus and Alertmanager configuration and unit-test the alert rules (needs Docker):

```bash
scripts/check_monitoring.sh
```

## Restart and Recovery

- **Stored data** lives in the `db` volume and survives restarts and rebuilds. Only `docker compose down -v` deletes it.
- **Interrupted runs:** a job run cut off by a restart is left as `running`. At start-up the backend closes it as `failed`, with the error "interrupted: the process stopped before the run finished". The job's lock goes with the dead process's database connection, so the next run starts normally.
- **Schedules** come from the database at start-up, and every job resumes at its next scheduled time. Runs missed while the backend was down are not repeated; anything they would have collected that is still available shows up as a gap, which backfill re-fetches.
- **Rate limits** are counted in memory and start from zero after a restart. A provider that answers HTTP 429 pauses its requests until its allowance returns, and the startup budget check keeps planned daily and monthly use within quota.
- **Provider outages** are contained: the failing provider's runs are recorded as failed, its health goes from `degraded` to `down` after three failures in a row, and its series become stale. Other providers carry on. When it recovers, its health returns to `healthy`, the stale issues close, and the missed range is backfilled where the provider keeps history.

## Development Roadmap

See [ROADMAP.md](ROADMAP.md).

## Agent Instructions

AI coding agents must read:

- [AGENTS.md](AGENTS.md)
- [PRD.md](PRD.md)
- [SKILLS.md](SKILLS.md)
- [ROADMAP.md](ROADMAP.md)

Development must follow the roadmap sequentially.

## License

To be determined.
