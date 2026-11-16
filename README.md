# Financial Market Data Platform

A self-hosted financial market data aggregation platform supporting stocks, ETFs, cryptocurrencies, forex, government bond yields, interest rates, commodities, market indices, and economic indicators.

**Status:** Phases 0 to 3 and Phases 4A to 4C are complete. The Docker Compose stack starts, migrations create the full data model, the seed file is applied, and `/api/v1/health` works. Twelve Data (stocks, ETFs, forex), CoinGecko (crypto) and the ECB (reference exchange rates, €STR, key rates, euro-area statistics) are integrated, and data can be collected from the command line. Scheduled collection, the data endpoints and dashboards are not implemented yet. See [ROADMAP.md](ROADMAP.md) for progress; the sections below marked *planned* describe the target.

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

## Data Providers (planned)

| Provider | Data | Roadmap phase |
| --- | --- | --- |
| Twelve Data | Stocks, ETFs, intraday forex, selected commodities | 4 |
| CoinGecko | Cryptocurrency prices and market data | 4 |
| ECB | Daily reference exchange rates, €STR, euro-area economic data | 4 |
| FRED | US Treasury yields, federal funds rate, economic indicators | 4 |
| Binance | Exchange-specific cryptocurrency prices | 8 |
| New York Fed | Money market rates (SOFR, EFFR) | 8 |

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

Default development credentials are documented in `.env.example` (Grafana: `admin` / `change_me`) and must be changed before deployment.

All published ports bind to `127.0.0.1` by default, so PostgreSQL (`localhost:5432`) is reachable only from the host itself.

Data is stored in the named volumes `db_data` and `grafana_data` and survives `docker compose down`. Use `docker compose down -v` only if you intend to delete it.

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
    dashboards/
    provisioning/

scripts/            # check.sh, check-docker.sh
docs/
    architecture.md
    providers/      # integration requirements, and one document per provider (from Phase 4)
```

## API (planned)

Base path: `/api/v1`.

Initial API endpoints:

| Method | Endpoint | Description |
| --- | --- | --- |
| GET | `/health` | Health status |
| GET | `/instruments` | List instruments |
| GET | `/instruments/{id}` | Instrument details |
| GET | `/prices/latest` | Latest prices |
| GET | `/prices/history` | Historical prices |
| GET | `/ohlcv` | OHLCV observations |
| GET | `/forex/rates` | Currency exchange rates |
| GET | `/bonds/yields` | Bond yields |
| GET | `/indicators` | Economic indicators |
| GET | `/providers` | Configured providers |
| GET | `/collectors/status` | Collector status |
| POST | `/collectors/{job}/run` | Trigger a collection manually (requires the administrator key) |

Read endpoints are unauthenticated and rely on the service being bound to loopback. Response conventions (errors, pagination, decimals as strings, UTC timestamps) are defined in [PRD.md](PRD.md), FR-006.

Interactive API documentation is served by Swagger UI at <http://localhost:8000/docs>.

## Grafana (planned)

Grafana connects to PostgreSQL/TimescaleDB.

Initial dashboards:

- Global Market Overview
- Stock Market
- ETF Monitor
- Cryptocurrency Market
- Forex Monitor
- Government Bond Yields
- Interest Rates
- Economic Indicators
- Collector Monitoring
- Commodities (with commodity data, in Phase 8)

Dashboard definitions are version-controlled under `grafana/`.

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
| `SEED_FILE` | `backend/seed.toml` | Seed file to apply; in Docker, a path inside the backend image |
| `COINGECKO_API_KEY` | (empty) | Optional [CoinGecko](https://www.coingecko.com/en/api) Demo key. Without it, CoinGecko's keyless API is used, at about 5 calls a minute. |
| `TWELVEDATA_API_KEY` | (empty) | [Twelve Data](https://twelvedata.com/pricing) API key. The free Basic plan is enough for the default instruments. Without it, Twelve Data collection fails and is reported as a provider failure. |
| `GRAFANA_ADMIN_USER` | `admin` | Grafana administrator user |
| `GRAFANA_ADMIN_PASSWORD` | `change_me` | Grafana administrator password (required) |

Database and Grafana credentials are applied when their volumes are first created; changing them later in `.env` does not alter an existing volume.

Further provider API keys and the administrator key (`ADMIN_API_KEY`) will be added to `.env.example` in the phases that introduce them. A provider's timeouts, retries, rate limits and the name of its key variable are set in `backend/seed.toml`; adding a provider is described in [docs/providers/README.md](docs/providers/README.md). Credentials are read from the environment only and are never stored in the database.

Never commit API credentials or production secrets.

## Tracked Instruments

What the platform tracks is defined in [backend/seed.toml](backend/seed.toml): exchanges, providers, instruments, the symbol each provider uses for an instrument, and economic indicators. The file documents its own format.

- It is applied on every startup, and applying it again changes nothing.
- An entry is matched on its natural key and updated in place.
- Removing an entry does not delete it, because stored history refers to it. Set `enabled = false` to stop tracking something.
- An invalid file stops the startup, and the `migrate` service log says what is wrong.

After editing the file, rebuild and restart: `docker compose up -d --build`.

The file currently tracks:

- 7 US stocks, 3 ETFs and 5 forex pairs through Twelve Data;
- 10 cryptocurrencies through CoinGecko;
- 4 euro reference exchange rates, €STR, two ECB key rates and 4 euro-area economic indicators through the ECB.

Each is sized to the provider's free plan. Other providers are added in later sub-phases of Phase 4. To find the symbol a provider uses:

```bash
docker compose run --rm migrate python -m app.cli search twelve_data "apple"
```

## Collecting Data

Scheduled collection arrives in Phase 5. Until then, collect on demand:

```bash
docker compose run --rm migrate python -m app.cli collect quotes twelve_data
docker compose run --rm migrate python -m app.cli collect candles twelve_data --interval 1day --days 30
docker compose run --rm migrate python -m app.cli collect quotes coingecko
docker compose run --rm migrate python -m app.cli collect history coingecko --days 90
docker compose run --rm migrate python -m app.cli collect quotes ecb
docker compose run --rm migrate python -m app.cli collect history ecb --days 365
docker compose run --rm migrate python -m app.cli collect indicators ecb
```

Each run uses provider credits:

- **Twelve Data:** 15 for quotes or candles of the default instruments. The free plan allows 8 a minute, so a run takes about two minutes.
- **CoinGecko:** 2 for current values and dominance, and 10 for history, one per coin. History is limited to the past 365 days and stops 2 days short of today, because CoinGecko may still revise recent values.

- **ECB:** no key and no published limit. A collection takes a few requests.

CoinGecko's free plans require attribution ("Powered by CoinGecko") wherever its data is displayed, and ECB data must cite the ECB as its source. Provider limits and licensing are documented in [docs/providers/](docs/providers/).

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
