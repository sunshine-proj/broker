# Backend Architecture

This document describes the backend as it is built today (through Phase 7). It grows with each phase; planned components are in [ROADMAP.md](../ROADMAP.md), and the rules each package follows are in [SKILLS.md](../SKILLS.md).

## Request Flow

```text
HTTP request
    |
    v
uvicorn  ->  FastAPI app (app/main.py: create_app)
                |
                +-- exception handlers (app/api/errors.py)
                |
                v
             /api/v1 router (app/api/v1/)
                |
                v
             endpoint  --Depends(get_session)-->  AsyncSession (app/database.py)
                                                       |
                                                       v
                                              PostgreSQL + TimescaleDB
```

## Components

| Path | Responsibility |
| --- | --- |
| `app/main.py` | `create_app()` factory. Builds the FastAPI app, registers exception handlers and routers. Its lifespan configures logging, creates the database engine on startup, and disposes of it on shutdown. |
| `app/config/settings.py` | `Settings`, the only place environment variables are read. `get_settings()` returns a cached instance. |
| `app/config/logging.py` | Routes all logs, including uvicorn's, to stdout as one JSON object per line. |
| `app/database.py` | Async SQLAlchemy engine and session factory, and the `get_session` dependency (one session per request). |
| `app/api/errors.py` | `AppError` and the global exception handlers. |
| `app/api/v1/` | Version 1 routers. `__init__.py` mounts them all under `/api/v1`. |
| `app/schemas/` | Pydantic response models. |
| `app/models/` | SQLAlchemy models and the enumerations. `base.py` holds the declarative base with a constraint naming convention. |
| `app/repositories/` | All SQL. `interfaces.py` defines the protocols services depend on; the `Sql*` classes implement them. |
| `app/services/seed.py` | Loads and applies the seed file. |
| `app/providers/` | The provider framework: adapter base class, shared HTTP client, rate limiter, errors, parsing helpers, factory. |
| `app/services/ingestion.py`, `validation.py` | Validates normalized provider data and stores it. |
| `app/services/provider_health.py` | Records provider health and contains provider failures. |
| `app/services/collection.py` | One collection run for one provider: fetch, validate, store. |
| `app/services/budget.py` | The startup rate budget check. |
| `app/services/quality.py`, `backfill.py` | Data-quality checks and gap backfill (PRD FR-012). |
| `app/repositories/quality.py` | Data-quality issues, and the queries the checks run. |
| `app/services/storage_policies.py` | TimescaleDB compression and retention policies. |
| `app/collectors/` | Job runner and scheduler. |
| `app/api/v1/market.py`, `collectors.py`, `quality.py` | Read endpoints; providers, collector status and the manual job trigger; data-quality issues. |
| `app/api/v1/params.py` | Shared paging, time-range and dependency definitions. |
| `app/services/reader_role.py` | Sets up Grafana's read-only database role. |
| `app/services/read_api.py` | Defaults, validation and response shaping for the read endpoints. |
| `app/repositories/read_queries.py` | The queries behind the read endpoints. |
| `app/schemas/api.py` | Response models. |
| `app/providers/twelve_data.py`, `coingecko.py`, `binance.py`, `ecb.py`, `nyfed.py`, `us_treasury.py`, `fred.py` | Provider adapters. |
| `app/cli.py` | Administrative commands: `seed`, `search`, `collect`, `jobs`, `run`, `reader-role`, `storage-policies`. |
| `migrations/` | Alembic environment and revisions. |

## Application Factory

There is no module-level `app` object. uvicorn is started with `--factory` and calls `create_app()`, which reads settings at that moment. Tests call `create_app(settings)` with their own settings, so importing the application never requires environment variables.

## Configuration

`Settings` reads environment variables, and falls back to the repository-root `.env` file when running outside Docker. Real environment variables take precedence. The database password is a `SecretStr`, so it is masked when settings are printed or logged.

## Database Access

- The engine uses the `asyncpg` driver and is created once per application, in the lifespan. It opens no connection until first used, so the application starts even while the database is down.
- Connections are checked before use (`pool_pre_ping`) and connection attempts time out after `database_connect_timeout_seconds` (5 seconds by default).
- Endpoints receive a session through `Depends(get_session)`. The session is closed when the request ends.

## Migrations

Schema changes are made only through Alembic revisions in `backend/migrations/versions/`.

- `migrations/env.py` builds the database URL from `Settings`; no URL is stored in `alembic.ini`.
- In Docker Compose, the one-shot `migrate` service runs `alembic upgrade head`, applies the seed file, applies the storage policies and sets up Grafana's role, and the backend starts only after all succeed.
- Revision `0001` enables the TimescaleDB extension; `0002` creates the data model; `0003` restricts candle intervals to the normalized set; `0004` adds collection jobs, execution triggers and exchange sessions; `0005` adds the reporting views; `0006` adds data-quality issues, the `quality` and `backfill` job kinds and their views; `0007` enables compression of the hypertables; `0008` adds the backup record.
- A migration spells out its own enumeration values instead of importing them, so it does not change when the application does.
- An integration test compares the models with the migrated schema and fails on any difference, so a model change without a migration is caught. A second test compares check-constraint names exactly, which the first does not cover.

Create a revision from `backend/`:

```bash
alembic revision -m "describe the change"
alembic upgrade head
```

## Data Model

The model is specified in [PRD.md](../PRD.md), section 7. This is how it is implemented.

| Table | Kind | Primary key |
| --- | --- | --- |
| `exchange`, `provider`, `instrument`, `economic_indicator`, `collector_execution`, `collection_job`, `data_quality_issue` | Entity | UUID `id`, generated by the database |
| `instrument_provider_mapping` | Link | (`instrument_id`, `provider_id`) |
| `provider_health` | One row per provider | `provider_id` |
| `market_observation` | Hypertable, 7-day chunks | (`instrument_id`, `provider_id`, `observation_type`, `observed_at`) |
| `ohlcv` | Hypertable, 30-day chunks | (`instrument_id`, `provider_id`, `interval`, `timestamp`) |
| `economic_observation` | Regular table | (`indicator_id`, `observed_at`, `ingested_at`) |
| `instrument_list`, `yield_spread`, `portfolio`, `portfolio_holding` | Entity (Phase 12) | UUID `id` |
| `instrument_list_member` | Link | (`list_id`, `instrument_id`) |
| `daily_observation` | Continuous aggregate of `market_observation` | (`instrument_id`, `provider_id`, `observation_type`, `day`) |

- Enumerations are text columns with a check constraint, not PostgreSQL enum types, so adding a value is an ordinary migration.
- Financial values are unconstrained `NUMERIC`, which stores every digit a provider sends.
- An instrument is unique on (`asset_type`, `symbol`, `exchange_id`), and two null exchanges count as equal.
- Each hypertable has one secondary index, on instrument, type or interval, and time, for queries that span providers.
- `economic_observation` is a regular table because it stays small.
- Stored observations and candles are immutable, except `market_observation.quality_status`, which the data-quality checks set to `suspect`.

### Storage

- Migration `0007` enables TimescaleDB compression (the columnstore) on both hypertables, segmented by series (`instrument_id, provider_id, observation_type`, or `interval` for candles) and ordered newest first.
- `python -m app.cli storage-policies` replaces the compression policy (`COMPRESS_AFTER_DAYS`, default 30) and the optional retention policies (`OBSERVATION_RETENTION_DAYS`, `CANDLE_RETENTION_DAYS`, unset by default) at every start.
- Compressed chunks still accept inserts, including `ON CONFLICT` checks, and the `quality_status` update; an integration test covers both.

## Repositories

Repositories hold all SQL and never commit; the caller owns the transaction.

| Repository | Covers |
| --- | --- |
| `SqlProviderRepository` | Providers |
| `SqlInstrumentRepository` | Exchanges, instruments, provider mappings |
| `SqlMarketDataRepository` | Market observations and candles |
| `SqlEconomicDataRepository` | Economic indicators and observations |

`SqlProviderHealthRepository` covers provider health. The repository for collector executions arrives with Phase 5.

### Writing Market Observations and Candles

`add_observations` and `add_candles` implement the immutability rule and return a `WriteResult`:

| Count | Meaning |
| --- | --- |
| `inserted` | New rows written |
| `unchanged` | Key already stored with the same values; nothing written |
| `conflicting` | Key already stored with different values; the stored row is kept and a warning is logged with both values |

Rows are inserted in batches of 1,000 with `ON CONFLICT DO NOTHING`. Keys that were not inserted are then read back and compared, so a repeat costs one extra query per batch. Values are compared as decimals, so `1.08` equals `1.0800`.

### Writing Economic Observations

`add_observations` implements the revision rule and returns a `RevisionWriteResult`:

| Count | Meaning |
| --- | --- |
| `inserted` | First value for a period |
| `revised` | Different value for a period that already had one; a new row is added with a later `ingested_at` and the earlier row is kept |
| `unchanged` | Same value as the current one; nothing written |

`get_observations` returns the current value of each period by default, and every stored value with `include_revisions=True`.

### Reading

Time-series reads take `start` and `end`, return rows with `start <= time < end`, oldest first, and are paginated with `limit` and `offset`.

## Provider Framework

This is the machinery each adapter is built on. The rules for writing one are in [providers/README.md](providers/README.md). Integrated so far: [Twelve Data](providers/twelve_data.md), [CoinGecko](providers/coingecko.md), [Binance](providers/binance.md), the [ECB](providers/ecb.md), the [New York Fed](providers/nyfed.md), the [US Treasury](providers/us_treasury.md) and [FRED](providers/fred.md).

```text
ProviderFactory.build(provider row)
    |
    v
adapter (subclass of Provider)  -- returns Quote / Candle / IndicatorValue
    |
    v
ProviderHttpClient.get_json()
    |-- RateLimiter.acquire(cost)      every attempt, retries included
    |-- httpx GET with timeout and credential
    |-- classify status, decode JSON (numbers as Decimal)
    `-- retry transient failures with exponential backoff
```

### Configuration and Credentials

A provider's row holds `base_url` and a `configuration` object, validated as `ProviderSettings`: timeout, retry count, backoff, rate limits and authentication. The seed file is validated against the same schema, so a mistyped setting stops the startup.

The configuration never contains a credential. It names an environment variable (ending in `_API_KEY` or `_TOKEN`), and `Settings.credential()` reads it. A provider whose variable is unset cannot be built.

### HTTP Client

| Outcome | Raised as | Retried |
| --- | --- | --- |
| Timeout, connection failure, HTTP 5xx | `ProviderUnavailableError` | Yes |
| HTTP 429 | `ProviderRateLimitedError` | Yes, after `Retry-After` if given |
| HTTP 401 or 403 | `ProviderAuthError` | No |
| Any other non-2xx status, or a body that is not JSON | `ProviderResponseError` | No |
| Body that does not match the adapter's model | `ProviderResponseError` | No |
| Our own rate limiter refuses | `RateBudgetExceededError` | No |

The delay before retry `n` is `backoff_base_seconds * 2^n`, capped at `backoff_max_seconds`, multiplied by a random factor between 0.5 and 1 so that concurrent retries spread out.

Error messages and log lines carry the request path, never the full URL, and httpx's own request logging is turned down to warnings, because a URL can contain an API key.

### Rate Limiter

Each provider has one `RateLimiter`, owned by the `ProviderFactory` and shared by every adapter instance built for that provider. It enforces every configured limit over a rolling window.

- A request that fits is granted at once.
- A request that does not fit waits, if capacity frees within `rate_limit_max_wait_seconds`; callers are served in order.
- Otherwise it is refused with `RateBudgetExceededError`. An exhausted daily or monthly quota fails fast instead of blocking.
- Usage is counted in memory and starts from zero after a restart. Keep one factory per process.
- Because our count can be behind the provider's (after a restart, or when another process uses the same key), an HTTP 429 pauses the limiter until the provider's allowance is expected back: the `Retry-After` time, or the next period boundary of a limit marked `resets_on_boundary`.

### Ingestion

`IngestionService` is the only path from an adapter to the database.

1. Resolve each provider symbol to an enabled instrument, or series code to an enabled indicator, through the seed mappings.
2. Validate each value (see the table in [providers/README.md](providers/README.md)).
3. Log and drop what fails. Values are never corrected.
4. Hand the rest to the repositories, which apply the deduplication and revision rules.

It returns an `IngestionResult` with counts of received, written, unchanged, conflicting and rejected values. The caller commits.

### Provider Health

`ProviderHealthService.guard(provider_id, operation)` runs a provider call and records the outcome in `provider_health`.

| Event | Effect |
| --- | --- |
| Success | Status `healthy`, failure count reset, `last_success_at` set. The last error stays visible. |
| Provider error | Failure count incremented, `last_error` and `last_error_at` set. Status `degraded`, or `down` from the third consecutive failure. The error is logged and `guard` returns `None`. |
| Any other exception | Not caught: it is a bug, not a provider failure. |

Because a provider error never leaves `guard`, one provider's failure cannot stop another provider's collection.

## Collection

`CollectionService` runs one collection for one provider: it takes the symbols of the provider's enabled mappings, calls the adapter inside `ProviderHealthService.guard`, and passes the result to ingestion.

- `collect_quotes` fetches every symbol in one call; the adapter splits it into batches. It can be limited to some observation types, which lets a cheap, frequent poll skip data that is fetched less often (CoinGecko dominance).
- `collect_candles` and `collect_quote_history` fetch symbol by symbol, so one failing symbol does not stop the rest.
- `collect_indicators` fetches each enabled economic indicator of the provider, series by series.
- A missing credential or other configuration problem is recorded as a provider failure, like any other.

The same collections can be run by hand from the command line, outside any job and without execution history:

```bash
python -m app.cli collect quotes twelve_data
python -m app.cli collect quotes coingecko --types price,market_cap
python -m app.cli collect candles twelve_data --interval 1day --days 30
python -m app.cli collect history coingecko --days 30
python -m app.cli collect indicators ecb --days 730
python -m app.cli search coingecko "cardano"
```

`collect` exits with 0 on success, 1 if it could not run, and 2 if a provider call failed. The failure is logged and recorded in `provider_health` either way.

## Collection Engine

Scheduled collection is organised as **jobs**. A job is one kind of collection (`quotes`, `candles`, `history` or `indicators`) for one provider, on one schedule. Two further kinds serve data quality: `quality` runs the checks over every provider's data and is the only job without a provider, and `backfill` re-fetches one provider's gaps. Jobs are defined in the seed file and stored in `collection_job`.

```text
seed.toml [[jobs]] --apply_seed--> collection_job
                                          |
             CollectorScheduler (APScheduler, in the backend) --+
             POST /api/v1/collectors/{job}/run -----------------+--> JobRunner
             python -m app.cli run JOB --------------------------+      |
                                                                          v
                                                  advisory lock, collector_execution row,
                                                  CollectionService, outcome recorded
```

### Job Configuration

| Field | Meaning |
| --- | --- |
| `interval_minutes` | Run every N minutes, aligned to the clock in UTC. N must divide 60 (every 15 minutes fires at :00, :15, :30, :45), or be whole hours dividing 24 (fired on the hour). |
| `daily_at` | Run once a day at this UTC time. Exactly one of the two schedules is set. |
| `asset_types` | Only instruments of these asset types. |
| `observation_types` | Quotes jobs: only these observation types. |
| `candle_interval` | Candles jobs: the candle length. |
| `lookback_days` | Candles, history and indicators jobs: how far back each run fetches. Values already stored are not written again. |
| `market_hours_only` | Scheduled runs poll only instruments whose exchange is in its regular session. When every exchange is closed, the run is skipped and nothing is recorded. |
| `retry_minutes` | After a failed scheduled run, run once more this many minutes later, unless the next regular run comes sooner. A retry is not retried. |
| `enabled` | Disabled jobs are not scheduled and cannot be triggered. |
| `stale_after_minutes`, `gap_after_minutes`, `business_days_only` | Quotes, candles and history jobs: data-quality thresholds for the series the job collects. |
| `max_requests` | Backfill jobs: the most gaps re-fetched per run. Counted in the budget check as that many candle requests. |
| `spike_percent`, `spike_points`, `discrepancy_percent`, `discrepancy_points` | The quality job's thresholds. |

Jobs removed from the seed file are disabled the next time it is applied. Instruments are not handled this way, because stored history refers to them.

### Trading Sessions

Exchanges have `trading_open` and `trading_close` in local time, Monday to Friday. Both ends count as open, so a 15-minute job polls 27 times between 09:30 and 16:00 New York time. Daylight saving time follows the exchange's time zone.

Holidays are not modelled: on an exchange holiday, a market-hours job polls and finds unchanged prices. This costs credits, but no more than the budget already allows.

### Rate Budget Check

Before the seed file is applied, `services/budget.py` adds up the planned use of every enabled job and compares it with each limit of its provider (PRD FR-011). An over-budget configuration is refused with a message naming the provider, the limit and each job's share, and nothing is written. Because the `migrate` service applies the seed, the stack does not start.

- **Credits per run:** each adapter's `planned_credits`. Twelve Data charges per symbol, CoinGecko per batched request plus one for dominance, the ECB per dataflow, Binance by request weight, and the US Treasury two feed requests per run.
- **Runs per day:** counted on a trading day, both in winter and summer time, taking the larger. A job with a retry counts double.
- **Periods:** a month counts 31 days. Limits shorter than a day are compared with the average use; bursts within a minute are paced by the rate limiter at run time.

### Running a Job

`JobRunner` runs a job in two steps, so that a manual trigger can answer at once:

1. **Prepare.** Take a PostgreSQL advisory lock for the job on a dedicated connection, insert a `collector_execution` row with status `running`, and set the job's `last_started_at`. If the lock is taken, the run is refused (`JobAlreadyRunningError`). This holds across processes, so a command-line run and the scheduler cannot overlap either.
2. **Execute.** Run the collection, then record the outcome on the execution and the job (`last_status`, `last_finished_at`, `last_error`), and release the lock.

| Outcome | Execution status |
| --- | --- |
| Every provider call succeeded | `success` |
| One or more provider calls failed | `failed`, with a summary such as `1 of 15 provider calls failed: ...`. Data that did arrive is kept. |
| Unexpected exception (a bug) | `failed`, with the exception type and message. The traceback is logged. The scheduler keeps running. |

Executions left `running` by a process that stopped are closed as failed ("interrupted") when the scheduler starts.

### Scheduler

`CollectorScheduler` wraps APScheduler's `AsyncIOScheduler` in the backend process. It is started by the application lifespan when `SCHEDULER_ENABLED` is true; Docker Compose sets it for the backend.

- Each enabled job gets a cron trigger in UTC, `max_instances=1` and `coalesce=True`. A run due while the previous one is still going is dropped, not queued.
- After each run, the job's `next_run_at` is updated.
- Run one scheduler per deployment. The job lock prevents overlapping runs, but two schedulers would each attempt every run and spend the provider budget twice.

### Manual Trigger

`POST /api/v1/collectors/{job}/run` starts a run in the background and answers `202` with the execution id. It requires the header `X-API-Key` to match `ADMIN_API_KEY`, and is disabled while that is unset.

| Situation | Status | `error.code` |
| --- | --- | --- |
| Run started | 202 | |
| `ADMIN_API_KEY` not set | 403 | `manual_trigger_disabled` |
| Header missing or wrong | 401 | `invalid_api_key` |
| No such job | 404 | `job_not_found` |
| Job disabled | 409 | `job_disabled` |
| Job already running | 409 | `job_running` |

A manual run collects every instrument of the job, also outside trading hours, and draws on the same provider budget. From the command line, `python -m app.cli run JOB` does the same and waits for the result; `python -m app.cli jobs` lists every job with its schedule, last outcome and next run.

## Data Quality

Specified in PRD FR-012. `QualityService.check` runs inside the `quality` job and reads only the database.

| Check | How |
| --- | --- |
| Stale | For each quotes, candles and history job: the newest value of each series it collects (one index lookup per series), against `stale_after_minutes`. With `business_days_only`, ages count Monday to Friday UTC only. A market-hours series is checked only once its session has been open longer than the threshold; while it is closed, an open issue is left open. |
| Gap | Consecutive values (a `lag()` window over the series) further apart than `gap_after_minutes`, observed since the later of the lookback and the job's creation. History imported from before the job existed is often coarser than the job's interval, so it is not checked. At most 20 gaps per series per check, newest first. |
| Spike | Price-like values stored since the previous check, against the value before them (one lateral lookup each). |
| Discrepancy | The view `v_provider_discrepancy` pairs each instrument's providers: the older of the two latest values against the other provider's newest value at or before that time, at most a day earlier. |
| Conflict | Recorded by `IngestionService` when the repository reports a re-sent row with different values. |

- Rates with `published_on_change = true` in their instrument metadata are skipped by the stale and gap checks.
- Each issue has a `fingerprint` naming the problem (kind, provider, instrument, series, and for gaps the start). Detecting it again updates the row. Stale and discrepancy issues that closed are reopened; a closed gap stays closed, so a gap backfill could not fill is not reported again.
- Conflicts and spikes are events: they are stored closed, with resolution `recorded`, and mark the observation `suspect`.

`BackfillService` runs inside each `backfill` job. It takes the provider's open gaps, oldest first, up to `max_requests`, and re-fetches each through `CollectionService` (candles, or quote history), so the rate limiter, provider health and ingestion apply as usual. It then counts the values now in the range and closes the gap as `filled` or `no_data`. A provider without the needed capability, or an untracked instrument, closes it as `unrecoverable` without a request; three failed attempts do too.

The views `v_quality_issue`, `v_provider_discrepancy` and `v_collection_latency` (median, 95th percentile and longest delay from observation to storage over the last 24 hours) serve the Data Quality dashboard.

## Seed File

`backend/seed.toml` is the source of truth for what is tracked (PRD FR-004). `python -m app.cli seed` validates it and applies it in one transaction.

- Validation rejects unknown keys, unknown enumeration values, duplicates, invalid provider configuration, a `base_url` that is not `https`, and references to an exchange or provider the file does not define. Nothing is written if validation fails.
- Each entry is created or updated on its natural key, so applying the file twice changes nothing.
- Entries absent from the file are left alone, except jobs, which are disabled.
- The rate budget is checked first; an over-budget file is refused.

## Read API

The endpoints are listed in the [README](../README.md#api). Every request passes through three layers:

```text
router (app/api/v1)          parses and validates query parameters
  -> read_api (services)     defaults, existence checks, response shaping
    -> SqlReadRepository     one SQL query per request part
```

- **Paging:** list queries fetch `limit + 1` rows. The extra row only sets `has_more`, so no count query is needed.
- **Serialization:** response models serialize `Decimal` with `str()`, so every stored digit is sent. Timestamps are serialized in UTC with a `Z`.
- **Latest values:** `/prices/latest`, `/forex/rates` and `/bonds/yields` combine each instrument-provider mapping with the requested observation types. For each combination, a `LATERAL` subquery takes the newest row through the hypertable's primary key, read backwards. The cost grows with the number of series, not with the history; on the live data the query takes about 4 ms.
- **Indicators:** `/indicators` takes each indicator's most recent period the same way, choosing the latest revision. `/indicators/{code}/observations` uses `DISTINCT ON` for current values, or returns every revision with `include_revisions`.
- **Main value:** `/prices/history` defaults to each instrument's main observation type: `price` for stocks, ETFs, crypto and commodities, `rate` for forex and interest rates, `yield` for government bond yields, and `index_level` for indices.
- **Unknown names:** a provider name in a filter that does not exist is an error (`unknown_provider`), not an empty result, so a typo is noticed.
- **Providers:** `/providers` shows only the rate limits from a provider's configuration. Authentication settings, including the name of the key variable, are never returned.

## Analytics

Phase 12 (PRD FR-014 to FR-016). Every calculation is a database view or function, created by migration `0009`, so the API and Grafana compute the same numbers. Nothing calculated is stored.

```text
market_observation --(continuous aggregate, every 30 min + real time)--> daily_observation
ohlcv (1day) + daily_observation --> v_daily_close        one close per instrument, provider, type, UTC day
v_daily_close --> v_daily_metrics      daily return, SMA 20/50/200, volatility 20/60 days
              --> v_period_return      weekly and monthly returns
              --> v_performance        latest close and its 1w, 1m, 3m, YTD, 1y returns
              --> v_yield_spread, v_yield_curve, relative_performance()
v_performance + instrument_list --> v_group_performance   equal-weighted, per horizon
portfolio_holding --> v_portfolio_holding, portfolio_value_history()   with fx_rate()
```

- **Daily closes:** a daily candle closes an instrument's main observation type (`primary_observation_type()`); other types, such as an ETF's NAV, keep their own series from the day's last observation. Each provider's series stays separate.
- **Continuous aggregate:** `daily_observation` is refreshed every 30 minutes over its whole range (only changed buckets are recomputed, including late inserts into compressed chunks) and completes the newest data in real time. Truncating `market_observation` does not empty it; the test cleanup truncates it directly.
- **Lookups:** `daily_close_at()` finds a series' newest close at or before a time, within a maximum age. It and `fx_direct()` are PL/pgSQL so that their plans are cached; as non-inlined SQL functions they were planned again on every call, which made a year of portfolio history take seconds.
- **Currency conversion:** `fx_rate(from, to, day)` tries a direct pair (or its inverse) from any provider, preferring the ECB, then a cross through EUR or USD. Pairs are found by the `base` and `quote` metadata of forex and crypto instruments, so `USDT` converts through `USDT/USD`. No rate gives an empty value.
- **Portfolio history:** `portfolio_value_history()` is set-based: each holding's closes are carried forward over a calendar of days with window functions, and each currency is converted once per day, not once per holding and day.
- **Seed and portfolio files:** watchlists, groups and spreads are applied with the seed by `SqlListRepository`; lists and spreads removed from the file are disabled. `python -m app.cli portfolio` replaces every portfolio from the private file through `SqlPortfolioRepository`, after checking every holding, so a bad file changes nothing.
- **API:** `app/api/v1/analytics.py` -> `services/analytics.py` -> `SqlAnalyticsRepository`, which only filters and orders the views.

## Authentication

PRD FR-017. Accounts, sessions and API tokens are in `app_user`, `user_session` and `api_token` (migration `0010`).

```text
browser --cookie marketdata_session--+
script  --Authorization: Bearer------+--> current_user (app/api/auth.py) --> AuthService --> SqlAuthRepository
                                                |
             /api/v1 routers: Depends(require_user)  (health excepted)
             /docs, /openapi.json: signed-in only (FastAPI's own docs routes are off)
proxy --forward_auth /auth/verify--> 204 + X-WEBAUTH-USER / X-WEBAUTH-ROLE --> Grafana auth proxy
```

- **Passwords:** scrypt from the standard library (N=2^15, r=8, p=3), with the parameters in the stored string. Hashing runs in a thread, off the event loop. An unknown user name is checked against a dummy hash, so it takes as long as a wrong password.
- **Tokens:** session and API tokens are 32 random bytes; only their SHA-256 is stored. A request is looked up by that hash, and only while the token is unrevoked, unexpired and its user enabled, so disabling a user takes effect on the next request.
- **Login form:** parsed without a form library. It rejects posts whose `Origin` is another site, and only ever redirects to a local path (`safe_next`). The cookie is `HttpOnly`, `SameSite=Lax` (so links from elsewhere open signed in), and `Secure` behind HTTPS.
- **Lockout:** failed logins are counted on the account; reaching `LOGIN_MAX_FAILURES` sets `locked_until`. Failure counts are committed even though the login fails.
- **Rights:** the backend's role reads and updates accounts and inserts sessions; deleting old sessions is the owner's, at every start (`prune-sessions`). Grafana's read-only role cannot read the three tables at all.
- **Grafana:** in production, Caddy drops any client-sent `X-Webauth-*` headers, calls `/auth/verify` for every `/grafana` request, and copies the user and role from the answer. Grafana (`GF_AUTH_PROXY_*`) accepts them only from the proxy's fixed address on the `grafana_auth` network, creates the user on first visit, and syncs the role (`admin` -> Admin, `viewer` -> Viewer).
- **Tests:** API tests sign in through `tests.conftest.signed_in`, which overrides `current_user`. `tests/test_auth.py` checks that every route in the OpenAPI schema but health refuses anonymous requests; `tests/test_auth_integration.py` signs in for real.

## Grafana

Grafana reads the database directly, through reporting views, as a read-only role, and Prometheus for the monitoring dashboards. It never writes and never calls the API.

```text
grafana/provisioning/datasources   -> datasource "Market Data" (uid marketdata), as GRAFANA_DB_USER,
                                      and "Prometheus" (uid prometheus)
grafana/provisioning/dashboards    -> loads grafana/dashboards/*.json into the "Market Data" folder
scripts/build_dashboards.py        -> generates grafana/dashboards/*.json
```

### Reporting Views

Migration `0005` defines the SQL the dashboards share:

| View | Contents |
| --- | --- |
| `v_observation` | Every market observation with symbol, asset type and provider name |
| `v_latest_observation` | The newest observation per instrument, provider and observation type, found per series through the index (as in the API), with its age |
| `v_candle` | Candles with symbol and provider name |
| `v_indicator_value` | Each indicator period's current value, with its number of stored revisions |
| `v_data_freshness` | Newest value and collection time per provider, asset type and observation type |
| `v_provider_status`, `v_job_status`, `v_execution` | Provider health, jobs and runs, with names joined in |

`v_latest_observation` lists the observation types it covers; a new type must be added there by a migration.

The analytics dashboards (Market Analytics, Watchlists and Groups, Portfolio) read the views and functions of migration `0009`, described under [Analytics](#analytics).

### Read-Only Role

`python -m app.cli reader-role` runs in the `migrate` service after migrations and the seed. It:

1. creates the role `GRAFANA_DB_USER` if needed and sets its password from `GRAFANA_DB_PASSWORD`;
2. makes it a login role without any administrative right, with `default_transaction_read_only = on` and `statement_timeout = 30s`;
3. replaces its table privileges with `SELECT` on every table and view in `public`, except `alembic_version` and the account tables (`app_user`, `user_session`, `api_token`).

Running after migrations means new tables and views are covered. Identifiers and the password are quoted by PostgreSQL's `format()`, not by Python.

### Dashboards

`scripts/build_dashboards.py` builds the dashboards from shared panel definitions:

- a latest-values table;
- series charts, with one line per instrument and provider;
- relative performance since the start of the range;
- a freshness table with thresholds suited to each dashboard;
- the attribution text required by each provider whose data a dashboard shows.

All dashboards use UTC, carry the `market-data` tag (which links them to each other), and are not editable in the interface.

### Testing

- `tests/test_dashboards.py` checks that the JSON matches the generator, and that each dashboard has its freshness table and its providers' attribution.
- The same file runs every variable and panel query against the test database, as a read-only role set up the same way, with Grafana's macros and variables expanded. It also checks that the role cannot write.
- `scripts/check_dashboards.py` runs every query through a running Grafana's query API, so Grafana's own macro handling is covered too.

## Error Handling

Every error response has the same shape (PRD FR-006):

```json
{"error": {"code": "not_found", "message": "Not Found", "details": {}}}
```

`details` is omitted when there is nothing to add.

| Source | Status | `code` |
| --- | --- | --- |
| `AppError` raised by application code | As raised | As raised |
| HTTP errors (unknown route, wrong method) | As raised | Derived from the status, for example `not_found` |
| Request validation failure | 422 | `validation_error`; `details.errors` lists location, message and type, without echoing the input |
| Any other exception | 500 | `internal_error`; the exception is logged with its traceback and never returned to the client |

Application code signals an expected failure by raising `AppError(code, message, status_code, details)`.

## Health Endpoint

`GET /api/v1/health` runs `SELECT 1` against the database.

| Situation | Status | Body |
| --- | --- | --- |
| Database reachable | 200 | `{"status": "ok", "version": "...", "database": "ok"}` |
| Database unreachable | 503 | `{"status": "degraded", "version": "...", "database": "unavailable"}` |

The Compose health check for the backend calls this endpoint.

## Logging

Each log line is a JSON object with `timestamp` (UTC), `level`, `logger` and `message`. Fields passed through `extra=` are added as top-level keys, and exceptions appear under `exception`. Set `LOG_FORMAT=text` for plain lines during local development.

## Monitoring

Specified in PRD FR-013; operated as described in [monitoring.md](monitoring.md).

| Part | Where |
| --- | --- |
| Process metrics: counters, histograms, gauges in prometheus_client's default registry | `app/monitoring/metrics.py`, updated in the API middleware (`app/main.py`), `ProviderHttpClient._get` and `RunHandle.execute` |
| Platform state read from the database at each scrape | `app/monitoring/database_metrics.py`: a snapshot rendered through its own registry, so nothing stale is left behind |
| `GET /metrics` | `app/api/metrics.py`. Outside `/api/v1`, not in the OpenAPI schema. If the database cannot be read within 10 seconds, it reports `marketdata_database_up 0` and still answers. |
| Prometheus, alert rules and their tests | `prometheus/` |
| Alertmanager | `alertmanager/` |

- **Route labels:** the API middleware labels requests by route template, never by raw path, so path parameters cannot multiply series. The template is rebuilt from the request path's router prefixes and the matched route's own template, because FastAPI keeps routes relative to their router.
- **Label names:** job names are labelled `job_name`; Prometheus reserves `job` for the scrape target.
- **Scope:** the process metrics cover the backend process only. Runs from the command line reach Prometheus through the database-derived job metrics.
- **Checks:** `scripts/check_monitoring.sh` runs `promtool check config`, `promtool test rules` and `amtool check-config` in the images Compose uses; CI runs it too. A test checks that every metric the dashboards query is one the backend defines.

## Deployment

Operated as described in [deployment.md](deployment.md).

| File | Role |
| --- | --- |
| `docker-compose.yml` | The stack. No published ports; an `internal` network without a route out, and `egress` for the services that need the internet. Limits, log rotation and hardening. |
| `docker-compose.override.yml` | Development, loaded automatically: publishes the ports on `BIND_ADDRESS` |
| `docker-compose.prod.yml` | Production, selected by `COMPOSE_FILE`: the Caddy reverse proxy (`proxy/Caddyfile`) and Grafana's HTTPS settings |
| `db/Dockerfile` | TimescaleDB with Alpine's security updates, for the database and the backup service |
| `backup/` | The backup service: `loop.sh` schedules, `run-backup.sh` dumps, verifies, checksums, prunes and records |
| `scripts/generate_env.py`, `check_env.py` | Create a `.env` with random secrets; check one for production |
| `scripts/restore.sh`, `test_backup_restore.sh`, `smoke_test.sh` | Restore a backup; test backup and restore; verify a production deployment from outside |

- **Roles:** `services/reader_role.py` sets up both restricted roles: `ensure_app_role` for the backend (`python -m app.cli app-role`) and `ensure_reader_role` for Grafana. An integration test runs collection, quality checks, backfill, the API and `/metrics` as the backend's role, and checks that it cannot delete, truncate or change the schema.
- **Backups** are recorded in `database_backup` (migration `0008`), which feeds the backup metrics and alerts.

## Testing

- **Unit tests** build the app with fixed settings and replace `get_session` with a fake, so they need neither network nor database.
- **Provider tests** answer HTTP requests with `httpx.MockTransport` and use a fake clock, so retries, backoff and rate-limit waits are tested without real waiting or network.
- **Integration tests** are marked `integration`. They create a separate database (`POSTGRES_TEST_DB`, default `marketdata_test`) on the same server, migrate it with Alembic, and run against it. The application database is never touched.
- Database tests use the `session` fixture, which wraps each test in a transaction that is rolled back, so tests do not see each other's rows.
- Collection engine tests commit for real, because the runner opens its own connections. They empty the tables they use before and after each test, in the test database only.
- When the database is unreachable, integration tests are skipped. Setting `REQUIRE_DATABASE=1` (as continuous integration does) turns that skip into a failure.
