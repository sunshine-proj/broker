# PRD — Financial Market Data Platform

## 1. Product Overview

### Product Name

Financial Market Data Platform

### Vision

Create a centralized platform for collecting, normalizing, storing, and visualizing financial market data from multiple external sources.

The platform should provide a unified view of global financial markets through Grafana dashboards and REST APIs.

The system should be modular, extensible, and suitable for continuous operation.

### Primary Objectives

- Aggregate financial data from multiple providers.
- Support different financial asset classes.
- Store current and historical observations.
- Normalize data into a common format.
- Provide reliable data collection and monitoring.
- Visualize financial information through Grafana.
- Allow new providers and instruments to be added easily.

## 2. Target Users

### Primary User

Individual investor or financial market enthusiast interested in monitoring global markets.

### Secondary Users

- Quantitative analysts
- Financial researchers
- Developers
- Portfolio managers
- System administrators

## 3. Supported Asset Classes

Fields marked *plan-dependent* are collected only when the configured provider plan supplies them. A missing field is stored as absent, never estimated.

### 3.1 Stocks

Collect:

- Symbol
- Company name
- Exchange
- Current price
- Open
- High
- Low
- Close
- Trading volume
- Daily percentage change
- Market capitalization (*plan-dependent*)
- Historical prices

### 3.2 ETFs

Collect:

- Symbol
- Fund name
- Exchange
- Price
- NAV (*plan-dependent*)
- Trading volume
- Assets under management (*plan-dependent*)
- Historical performance

### 3.3 Cryptocurrency

Collect:

- Symbol
- Name
- Price
- Market capitalization
- Trading volume
- Circulating supply
- Total supply
- Market dominance
- Historical prices
- Exchange-specific prices (from Phase 8, with Binance)

Prices from CoinGecko are averages across exchanges, served from a cache of 1 to 5 minutes. They are stored as indicative and delayed.

### 3.4 Forex

Collect:

- Base currency
- Quote currency
- Exchange rate
- Historical exchange rate
- Provider timestamp

Support major, minor, and selected exotic currency pairs.

Two kinds of rate are collected and must stay distinguishable. Where both exist for a pair (EUR/USD, EUR/GBP), both are stored on the same instrument under their own provider:

- Intraday market rates (Twelve Data).
- Daily reference rates (ECB), published once per business day. These are indicative, not executable.

### 3.5 Bonds

In scope for the initial release: **government benchmark yields** (for example the US Treasury curve and euro-area government yields), collected as yield observations.

- The US Treasury par yield curve comes from the US Treasury, and also from FRED where FRED is enabled.
- Euro-area yields are the ECB's estimated yield curves (AAA-rated issuers, and all issuers), not individual countries. Per-country long-term rates are only published as monthly averages, which are not point-in-time yields.
- Each yield instrument records its `country`, `maturity` and `curve` in its metadata.

Deferred: **individual bond data** (identifier, issuer, maturity date, coupon, price, yield to maturity). No initial provider supplies it. It will be specified when a suitable provider is added, and must then be kept distinct from benchmark yields.

### 3.6 Interest Rates

Collect:

- Central bank policy rates
- Federal funds rate
- SOFR
- €STR
- Historical rate observations

EURIBOR is licensed by its administrator and is not published by the ECB. It is out of scope unless a licensed provider is added.

€STR and the ECB's key interest rates come from the ECB. The key rates are recorded on the dates they change; a rate holds until its next recorded change, and no daily values are filled in.

### 3.7 Commodities

Collect (from Phase 8):

- Gold
- Silver
- Crude oil
- Natural gas
- Other supported commodities

Coverage depends on the provider's plan and licensing. Twelve Data's free plan supplies spot gold only; silver, crude oil and natural gas need its Grow plan. Free government sources (EIA) republish oil and gas spot prices under third-party licences (Refinitiv, NGI), so they are not used. Commodities not available under a suitable licence are not collected.

### 3.8 Economic Indicators

Collect:

- Inflation
- GDP
- Unemployment
- Consumer confidence
- Industrial production
- Government debt
- Other macroeconomic indicators

PMI series are proprietary and are out of scope unless a licensed provider is added.

### 3.9 Market Indices

Collect (from Phase 8):

- Index symbol
- Name
- Index level
- Historical levels

Index data is subject to licensing. Only series the configured provider is permitted to distribute are collected. None of the configured providers' free plans include index levels (Twelve Data offers them from its Grow plan), so no index is collected yet; index-tracking ETFs are collected as ETFs.

## 4. External Data Providers

| Provider | Primary Purpose | Introduced |
| --- | --- | --- |
| Twelve Data | US stocks, ETFs, intraday forex, spot gold | Phase 4 |
| CoinGecko | Cryptocurrency prices and market data | Phase 4 |
| ECB | Daily reference exchange rates, €STR, euro-area economic data | Phase 4 |
| FRED | US economic indicators, Treasury yields, federal funds rate | Phase 4 |
| Binance | Exchange-specific cryptocurrency market data at high frequency | Phase 8 |
| New York Fed | US money market rates (SOFR, EFFR, OBFR) from the primary source | Phase 8 |
| US Treasury | Daily Treasury par yield curve, from the primary source | Phase 8 |

Additional providers may be introduced later.

Provider integrations must be independent.

Provider quotas, plans, and licensing terms change. Each integration documents the limits it was built against, and the limits themselves are configuration, not code.

**Open licensing question:** FRED's terms prohibit storing, caching or archiving FRED content, which conflicts with this platform's permanent history. The FRED series used here are published by US government agencies and could be sourced from them directly. See [docs/providers/fred.md](docs/providers/fred.md).

## 5. Functional Requirements

### FR-001: Data Collection

The application must collect financial data from configured providers.

Collection must support:

- Scheduled collection
- Manual collection
- Configurable intervals
- Historical data imports where available

### FR-002: Data Normalization

Provider-specific data must be converted into common internal schemas.

Normalization must cover:

- Instrument identifiers
- Currency
- Timestamps
- Price units
- Measurement units
- Asset classes

Normalization also validates each observation before it is stored: the timestamp is timezone-aware, in UTC, and not in the future (5 minutes of clock difference are tolerated); the currency matches the instrument's currency; the value parses as a finite decimal; a candle's high and low are consistent with its other prices. Observations that fail validation are rejected and logged, not corrected.

### FR-003: Historical Storage

The application must persist historical observations.

Historical records must not be silently replaced by newer observations. Section 7 defines the deduplication and revision rules.

### FR-004: Instrument Management

Tracked instruments are managed through a version-controlled seed file (`backend/seed.toml`), applied idempotently on every startup, after database migrations and before the backend starts. An invalid seed file stops the startup. Entries removed from the file are not deleted, because history refers to them; they are disabled with `enabled = false`. The seed file is the source of truth for instruments, provider mappings, economic indicators, and collection jobs.

Instrument configuration includes:

- Symbol
- Name
- Asset class
- Exchange
- Currency
- Provider mapping (the symbol each provider uses for the instrument)
- Enabled/disabled status

The REST API exposes this configuration read-only in the initial release. Write endpoints are not provided, because they would require user authentication, which is deferred to Phase 12.

### FR-005: Provider Management

The system must maintain provider configuration.

Provider configuration should include:

- Provider name
- API endpoint
- Authentication configuration
- Enabled status
- Rate limits
- Last successful synchronization
- Last error
- Health status

Credentials are supplied only through environment variables (for example `TWELVE_DATA_API_KEY`). The database stores non-secret provider settings and, at most, the name of the environment variable that holds a credential. Credentials are never stored in the database, logged, or returned by the API.

### FR-006: API

Expose normalized financial data through REST APIs.

Base URL: `/api/v1`.

Initial endpoints:

- `GET /health`
- `GET /instruments`
- `GET /instruments/{id}`
- `GET /prices/latest`
- `GET /prices/history`
- `GET /ohlcv`
- `GET /forex/rates`
- `GET /bonds/yields`
- `GET /indicators`
- `GET /indicators/{code}/observations`
- `GET /providers`
- `GET /collectors/status`
- `GET /collectors/executions`
- `POST /collectors/{job}/run`

`/indicators/{code}/observations` returns an indicator's history, current values or every revision. `/collectors/executions` returns the run history, newest first.

`GET` endpoints are unauthenticated and rely on the service being bound to a trusted interface (loopback by default).

`POST /collectors/{job}/run` triggers a manual collection. It requires the `X-API-Key` header to match the `ADMIN_API_KEY` environment variable, and is disabled when that variable is unset.

#### API Conventions

- **Errors:** every error response has the shape `{"error": {"code": "<machine_readable_code>", "message": "<human readable>", "details": <optional object>}}`.
- **Pagination:** list endpoints accept `limit` (default 100, maximum 1000) and `offset`, and return `{"items": [...], "limit": n, "offset": n, "has_more": bool}`.
- **Time ranges:** time-series endpoints accept `start` (inclusive) and `end` (exclusive) as ISO 8601 timestamps and return items in ascending time order. Missing bounds default to now and to a per-endpoint window. The execution history is a log, not a time series, and is returned newest first.
- **Decimals:** financial values are serialized as JSON strings to preserve precision.
- **Timestamps:** ISO 8601 in UTC with a `Z` suffix.

### FR-007: Scheduling

Collection jobs must run automatically.

Each job must support:

- Configurable interval
- Enable/disable
- Execution status
- Error reporting
- Last execution time
- Next scheduled execution

Jobs for exchange-traded instruments run only during the relevant exchange's trading hours, so quota is not spent while markets are closed. Exchange holidays are not modelled.

Jobs are defined in the seed file (`[[jobs]]`) with a schedule of either a clock-aligned interval or a daily UTC time. A failed run may be retried once after a configured delay. A job never runs twice at the same time, across processes. A manual run (API or command line) collects every instrument of the job, also outside trading hours.

### FR-008: Grafana Integration

Grafana must visualize stored financial information.

Dashboards must show the attribution a provider's licence requires for the data they display.

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
- Commodities (delivered in Phase 8, together with commodity data)
- Data Quality (delivered in Phase 9)
- System Monitoring, Provider Availability and Collection Failures (delivered in Phase 10, from Prometheus and the database)

### FR-009: Monitoring

The platform must provide visibility into:

- Provider availability
- Collector execution
- API errors
- Collection latency
- Database availability
- Last successful update
- Data freshness

### FR-010: Configuration

- Application settings must be configurable using environment variables.
- Secrets must not be stored in source code.

### FR-011: Provider Rate Budget

Each provider has a configured quota (per minute, per day, or per month, as the provider defines it).

- At startup, before the seed file is applied, the application computes the calls each provider's enabled jobs will make (instruments × polls per period) and refuses a configuration that exceeds the quota, reporting which provider is over budget. Market-hours jobs are counted as if every day were a trading day, retries as if every run were retried, and a month as 31 days.
- At runtime, a per-provider rate limiter enforces the quota across scheduled jobs, manual triggers, and retries together.
- Requests that fetch several instruments in one call are preferred wherever the provider supports them.
- A request that would have to wait longer than a configured maximum for capacity is refused, not queued. An exhausted daily or monthly quota therefore fails fast.

The runtime limiter counts usage in memory, so its counts restart from zero when the backend restarts. When the provider nevertheless answers HTTP 429, all requests to that provider pause until its allowance is expected back. The startup check is what keeps planned usage inside daily and monthly quotas; the limiter guards against bursts, retries and manual triggers on top of it.

### FR-012: Data Quality

The platform detects and reports data-quality problems. It never corrects or deletes a stored value.

Checks run on a schedule (a `quality` job in the seed file, every 15 minutes by default) and record each problem as a data-quality issue:

| Issue | Detected when | Closed when |
| --- | --- | --- |
| `stale` | A series a job collects has no value newer than the job's `stale_after_minutes`. For market-hours jobs, only while the exchange's session has been open at least that long. | A newer value arrives |
| `gap` | Two consecutive values of a series are further apart than the job's `gap_after_minutes`, within the check's lookback window | A backfill run has re-fetched the range: `filled`, `no_data` (the provider has nothing for it, such as a holiday) or `unrecoverable` (the provider cannot re-fetch it, or failed three times) |
| `conflict` | A provider re-sends a stored observation or candle with a different value (section 7). The stored value is kept. | Recorded closed: it is an event |
| `spike` | A price-like value differs from the previous value of its series by more than `spike_percent` (or, for values in percent, `spike_points`) | Recorded closed: it is an event |
| `discrepancy` | Two providers' values of the same instrument and observation type at the same time differ by more than `discrepancy_percent` (or `discrepancy_points` for values in percent). The older of the two latest values is compared with the other provider's value at that time (its newest at or before it, at most a day earlier). | The values agree again |

- Job settings for stale and gap detection have defaults: three intervals for interval jobs, four days for daily jobs. `business_days_only` counts only Monday to Friday (UTC) time, for markets that close at weekends. Instruments with `published_on_change = true` in their metadata (rates recorded only when they change) are not checked for gaps.
- A conflicting stored row and a spiking value are marked `quality_status = suspect`. That is the only change ever made to a stored observation; its value never changes.
- Gaps are re-fetched by a `backfill` job per provider, at most `max_requests` requests per run, so backfill fits the provider's rate budget (FR-011).
- Issues are readable through `GET /api/v1/quality/issues` and the Data Quality dashboard.

Collection latency is reported per provider and kind of data: the delay between a value's own timestamp and its storage, and each job run's duration.

### FR-013: Metrics and Alerting

The backend exposes Prometheus metrics at `GET /metrics` (text exposition format, no authentication; it contains no credentials and is not under `/api/v1`, because it is not part of the data API).

| Area | Metrics |
| --- | --- |
| API | Requests by method, route template and status; request duration by method and route |
| Providers | Requests by provider and outcome (`success`, `rate_limited`, `server_error`, `client_error`, `auth_error`, `timeout`, `connection_error`), request duration, retries, time spent waiting for the rate limiter, requests refused by the rate budget |
| Collectors | Runs by job, kind, trigger and status; run duration; records written; runs in progress |
| From the database, read at each scrape | Database availability; the health status and consecutive failures of each provider in use (enabled, with an enabled job); each enabled job's last outcome, last success and expected interval; age of the newest data per provider, asset type and observation type; open data-quality issues by kind, and stale series per provider |
| Backups | When the last successful backup finished, its size, whether the newest run failed, the backup interval |
| Process | CPU, memory, open files and garbage collection of the backend process |

Counters and histograms cover the backend process, so runs started from the command line appear only in the database-derived metrics.

Prometheus scrapes the backend, itself and Alertmanager, and evaluates the alert rules. Alertmanager groups and routes alerts. No notification channel is configured by default: alerts are visible in Prometheus, Alertmanager and Grafana, and a receiver (email, webhook, chat) is added in the Alertmanager configuration.

| Alert | Fires when | Severity |
| --- | --- | --- |
| `BackendDown` | Prometheus cannot scrape the backend for 2 minutes | critical |
| `DatabaseDown` | The backend cannot reach the database for 1 minute | critical |
| `MonitoringTargetDown` | Another scrape target is down for 5 minutes | warning |
| `ApiErrorRateHigh` | More than 5 % of API requests fail with 5xx for 10 minutes | warning |
| `CollectorJobFailed` | A job's last run failed | warning |
| `CollectorJobNotSucceeding` | A job that does not follow market hours has had no successful run for twice its interval plus an hour | critical |
| `ProviderDown` | A provider's health is `down` for 5 minutes | critical |
| `ProviderDegraded` | A provider's health is `degraded` for an hour | warning |
| `ProviderRateLimited` | A provider answered HTTP 429 more than 3 times in an hour | warning |
| `ProviderBudgetExhausted` | The rate limiter refused a request because the provider's budget was exhausted | warning |
| `DataStale` | A provider has stale series (FR-012) for 30 minutes | warning |
| `BackupFailed` | The newest backup run failed | warning |
| `BackupMissing` | The last successful backup is older than the backup interval plus six hours | critical |
| `BackupNeverRun` | The database is up but no backup has ever succeeded, for 2 hours | critical |

## 6. Non-Functional Requirements

### Performance

Targets apply to a single-host Docker Compose deployment with the expected data volume below.

- One year of daily observations for one instrument is returned in under 200 ms (95th percentile).
- 30 days of 1-minute observations for one instrument is returned in under 1 second (95th percentile).
- Latest-price lookups for all enabled instruments complete in under 200 ms.
- Instrument and timestamp lookups are indexed.
- External HTTP communication is asynchronous.
- Large API responses are paginated.

### Expected Data Volume

The design target is up to 100 enabled instruments. The largest contributor is 1-minute crypto data: 20 symbols produce about 29,000 rows per day, or roughly 10 million rows per year. All other data types together add well under 1 million rows per year.

### Retention

Raw observations are kept indefinitely by default; nothing is deleted automatically.

- Market observations and candles older than `COMPRESS_AFTER_DAYS` (default 30) are compressed by TimescaleDB. Compressed data stays queryable and can still be inserted into.
- Optional retention: `OBSERVATION_RETENTION_DAYS` and `CANDLE_RETENTION_DAYS` drop older market observations and candles. Both are unset by default. Economic observations are never dropped.
- The policies are applied at every startup from these environment variables.

### Reliability

- Automatic retries for transient failures.
- Provider-specific failure isolation.
- Persistent historical storage.
- Graceful recovery after restart.

### Security

- No exposed API credentials.
- Restricted database access.
- Secure secret management.
- Input validation.
- No unnecessary public service exposure.

How the deployment meets these (Phase 11; operated as in docs/deployment.md):

- **Secrets** are environment variables from a `.env` file that is never committed, readable by its owner only. The repository ships no passwords; `scripts/generate_env.py` creates random ones and `scripts/check_env.py` refuses a production `.env` with empty, short, reused or placeholder secrets.
- **Database roles:** the running backend connects as its own role, which can read, insert and update but not delete, truncate or change the schema. Grafana's role can only read. Migrations, the seed file, backups and the command line use the owner. The restricted roles are recreated with exactly these privileges on every start.
- **Network:** the database and Prometheus are on a network with no route outside. In production, the only published ports are the reverse proxy's 80 and 443, with HTTPS from Let's Encrypt and HSTS.
- **Exposure:** in production Grafana is served behind its own login, with sign-up, anonymous access and public or external sharing disabled. The REST API is served behind HTTP basic authentication: the providers' licences do not allow redistributing their data, so the API is for the people given its credentials. `/metrics`, Prometheus and Alertmanager are not published. User accounts for the API remain Phase 12.
- **Containers** have memory and CPU limits, rotated logs, `no-new-privileges`, and, where the image allows, no capabilities and a read-only root filesystem.

### Backups

- The database is backed up every `BACKUP_INTERVAL_HOURS` (24 by default) with `pg_dump`, and the newest `BACKUP_KEEP` (7) backups are kept. Each is verified and checksummed before it counts, and each run is recorded in the database (section 7, Database Backup).
- A failed backup, a missed backup and a backup service that never ran raise alerts (FR-013).
- Restoring follows TimescaleDB's procedure, and is tested by `scripts/test_backup_restore.sh`.
- Backups must also be copied off the server; that copy is the operator's responsibility (docs/deployment.md).

### Maintainability

- Modular provider architecture.
- Automated testing.
- Database migrations.
- Version-controlled dashboards.
- Documented configuration.

### Scalability

The system should support adding providers, asset classes, and instruments without major architectural changes.

## 7. Data Model

### Conventions

- Entity tables use UUID primary keys.
- Time-series tables (`market_observation`, `ohlcv`) are TimescaleDB hypertables. They have no surrogate `id`; their primary key is the natural key listed below, which includes the time column as TimescaleDB requires.
- Financial values use `NUMERIC`. Timestamps are timezone-aware and stored in UTC.

### Which Table Holds What

- **Market observations** hold any value quoted for an instrument: prices, exchange rates, benchmark yields, policy and money-market rates, index levels, market capitalization, supply.
- **Economic observations** hold periodic macroeconomic statistics that are published for a period and may later be revised: inflation, GDP, unemployment, and similar.

So a Treasury yield or SOFR is an instrument with market observations; CPI is an economic indicator. `GET /bonds/yields` reads market observations of type `yield`; `GET /indicators` reads economic observations.

### Enumerations

- `asset_type`: `stock`, `etf`, `crypto`, `forex`, `government_bond_yield`, `interest_rate`, `commodity`, `index`.
- `observation_type`: `price`, `nav`, `rate`, `yield`, `index_level`, `market_cap`, `volume`, `circulating_supply`, `total_supply`, `dominance`.
- `freshness`: `real_time`, `delayed`, `end_of_day`.
- `quote_kind`: `traded`, `indicative`, `reference`.
- `quality_status`: `ok`, `suspect`. Set by the data-quality checks (FR-012); every other column of a stored observation is immutable.
- `frequency` (economic indicators): `daily`, `weekly`, `monthly`, `quarterly`, `annual`.
- `interval` (candles): `1min`, `5min`, `15min`, `30min`, `1h`, `4h`, `1day`, `1week`, `1month`.

Enumerations are stored as text with a check constraint, so adding a value needs a migration.

### Core Entities

#### Exchange

A trading venue.

Fields:

- `id`
- `code` (unique, for example a market identifier code)
- `name`
- `country`
- `timezone`
- `created_at`

#### Instrument

Represents a financial instrument.

Fields:

- `id`
- `symbol`
- `name`
- `asset_type`
- `exchange_id` (null for instruments without a venue, such as forex pairs)
- `currency` (the currency values are quoted in; null where a value has no currency, such as a yield)
- `metadata`
- `enabled`
- `created_at`

Unique on (`asset_type`, `symbol`, `exchange_id`), with null exchanges treated as equal. Forex and crypto pairs use the symbol form `BASE/QUOTE`, and `currency` is the quote currency.

#### Provider

Represents an external financial data provider.

Fields:

- `id`
- `name` (unique)
- `base_url`
- `enabled`
- `configuration` (non-secret settings only, including rate limits)
- `created_at`

#### Instrument Provider Mapping

Links an instrument to the symbol a provider uses for it.

Fields:

- `instrument_id`
- `provider_id`
- `provider_symbol`
- `enabled`
- `metadata`

Unique on (`instrument_id`, `provider_id`).

#### Market Observation

Represents a collected financial observation.

Fields:

- `instrument_id`
- `provider_id`
- `observation_type`
- `observed_at` (the provider's timestamp for the value)
- `ingested_at`
- `value`
- `currency` (null where not applicable)
- `unit` (for example `currency`, `percent`, `points`, `count`)
- `freshness`
- `quote_kind`
- `quality_status`
- `metadata`

Primary key: (`instrument_id`, `provider_id`, `observation_type`, `observed_at`).

#### OHLCV

Represents market candle data. Only closed candles are stored.

Fields:

- `instrument_id`
- `provider_id`
- `interval`
- `timestamp` (candle open time)
- `open`
- `high`
- `low`
- `close`
- `volume`
- `currency`
- `ingested_at`

Primary key: (`instrument_id`, `provider_id`, `interval`, `timestamp`).

#### Economic Indicator

Defines an economic series from one provider.

Fields:

- `id`
- `code` (unique internal code)
- `name`
- `country`
- `unit`
- `frequency`
- `provider_id`
- `provider_series_code`
- `enabled`
- `metadata`

#### Economic Observation

One published value of an economic indicator for one period.

Fields:

- `indicator_id`
- `observed_at` (the period the value refers to)
- `ingested_at`
- `value`
- `metadata` (including the provider's publication or vintage date where available)

Primary key: (`indicator_id`, `observed_at`, `ingested_at`).

#### Collector Execution

One run of a collection job. A `quality` job has no provider; every other job kind (`quotes`, `candles`, `history`, `indicators`, `backfill`) belongs to one provider.

Fields:

- `id`
- `job_name`
- `provider_id`
- `started_at`
- `finished_at`
- `status` (`running`, `success`, `failed`)
- `records_written`
- `error`

#### Data Quality Issue

One data-quality problem (FR-012).

Fields:

- `id`
- `kind` (`stale`, `gap`, `conflict`, `spike`, `discrepancy`)
- `status` (`open`, `closed`)
- `resolution` (`cleared`, `filled`, `no_data`, `unrecoverable`, `recorded`; null while open)
- `fingerprint` (unique: identifies the problem, so that detecting it again updates the same issue)
- `instrument_id`, `provider_id`, `other_provider_id` (discrepancies), `observation_type`, `interval` (candle gaps), all nullable
- `range_start`, `range_end` (the affected period, where there is one)
- `message`, `details`
- `detected_at`, `last_seen_at`, `closed_at`

A `stale` or `discrepancy` issue that closes and recurs is reopened. A closed gap is not reopened.

#### Database Backup

One run of the backup service.

Fields:

- `id`
- `started_at`, `finished_at`
- `status` (`success`, `failed`)
- `file_name`, `size_bytes` (successful runs)
- `interval_seconds` (how often the service runs)
- `error` (failed runs)

#### Provider Health

Current health of a provider, one row per provider.

Fields:

- `provider_id`
- `status` (`healthy`, `degraded`, `down`)
- `last_success_at`
- `last_error_at`
- `last_error`
- `consecutive_failures`
- `updated_at`

### Deduplication and Revisions

- **Different providers never collide.** The provider is part of every key, so two providers' values for the same instrument and time are both kept.
- **Market observations and OHLCV are immutable.** Re-collecting a row whose key already exists writes nothing. If the incoming value differs from the stored one, the stored value is kept and the difference is logged as a data-quality warning. Values are compared numerically, so `1.08` and `1.0800` are the same value.
- **Economic observations are revisable.** If a provider reports a different value for a period that is already stored, a new row is inserted with a later `ingested_at`. Earlier values are kept. The current value for a period is the row with the latest `ingested_at`. An unchanged value writes nothing.

## 8. Data Refresh Strategy

Default intervals are set so that the default instrument set fits each provider's free quota. They are configuration, validated by FR-011.

| Data Type | Provider | Default Interval | Constraint |
| --- | --- | --- | --- |
| Crypto prices and market data | CoinGecko | 5 minutes | Free Demo plan allows 10,000 calls per month; one batched call every 5 minutes uses 8,640 |
| Crypto dominance | CoinGecko | 1 hour | 720 calls per month |
| Crypto price history | CoinGecko | Daily | One call per coin: 300 per month for 10 coins. Only values older than 2 days, which CoinGecko no longer revises. |
| Exchange-specific crypto prices | Binance | 1 minute | One request (weight 2) per minute; Binance allows 6,000 weight a minute per IP address. Availability varies by region. |
| Intraday forex | Twelve Data | 30 minutes | Shares the Twelve Data daily quota |
| Spot gold | Twelve Data | 30 minutes | Shares the Twelve Data daily quota |
| Stocks and ETFs | Twelve Data | 15 minutes, during trading hours | Shares the Twelve Data daily quota |
| Reference exchange rates | ECB | Daily | Published once per business day |
| Government yields and policy rates | US Treasury, ECB, FRED | Daily | Source data is daily |
| Money-market rates | New York Fed | Daily, after 14:30 New York time | Rates may be revised the same day until then |
| Economic indicators | FRED, ECB | Daily check | Most series update monthly or quarterly |
| Historical data | All | On demand | Counts against the same quotas |

### Twelve Data Budget

The free plan allows 8 credits per minute and 800 per day, at one credit per symbol per request. The default instrument set is sized to fit:

| Group | Instruments | Polls per day | Credits per day |
| --- | --- | --- | --- |
| Stocks and ETFs (15 minutes over a 6.5-hour session, counting both ends) | 14 | 27 | 378 |
| Forex pairs (30 minutes, 24 hours) | 5 | 48 | 240 |
| Spot gold (30 minutes, 24 hours) | 1 | 48 | 48 |
| Daily candles, counted twice for their retry | 20 | 2 | 40 |
| **Total** | | | **706 of 800** |

Forex moved from 15 to 30 minutes in Phase 8 to make room for more ETFs and gold. The ECB's daily reference rates cover further currency pairs at no cost to this quota.

Tracking more instruments or polling faster requires a paid plan or longer intervals.

These are application targets, not guarantees of provider availability. Provider rate limits and data licensing take precedence. Quotas were last checked in November 2026 and must be re-checked when a provider is integrated.

## 9. User Interface

Grafana is the primary visualization interface.

The first version does not require a custom frontend.

Grafana should provide:

- Time-series charts
- Tables
- Market summary panels
- Percentage changes
- Comparative charts
- Dashboard variables
- Time-range selection

## 10. Out of Scope for Initial Release

- Automated trading
- Brokerage integrations
- Order execution
- Portfolio management
- Investment recommendations
- Machine-learning price predictions
- Proprietary exchange connectivity
- High-frequency trading infrastructure
- Individual bond data
- Licensed benchmarks and surveys (EURIBOR, PMI)
- API write endpoints for instrument management, and user authentication

## 11. Success Criteria

The initial release is successful when:

- The application starts using Docker Compose.
- At least three independent data providers are integrated.
- At least three asset classes are supported.
- Data is normalized and stored in PostgreSQL/TimescaleDB.
- Historical prices can be queried.
- Grafana can display stored market data.
- Collectors run automatically.
- Provider errors are visible.
- The application can restart without losing historical data.
- Automated tests pass.
