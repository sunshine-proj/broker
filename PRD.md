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

### 3.4 Forex

Collect:

- Base currency
- Quote currency
- Exchange rate
- Historical exchange rate
- Provider timestamp

Support major, minor, and selected exotic currency pairs.

Two kinds of rate are collected and must stay distinguishable:

- Intraday market rates (Twelve Data).
- Daily reference rates (ECB), published once per business day. These are indicative, not executable.

### 3.5 Bonds

In scope for the initial release: **government benchmark yields** (for example the US Treasury curve and euro-area government yields), collected as yield observations.

Deferred: **individual bond data** (identifier, issuer, maturity date, coupon, price, yield to maturity). No initial provider supplies it. It will be specified when a suitable provider is added, and must then be kept distinct from benchmark yields.

### 3.6 Interest Rates

Collect:

- Central bank policy rates
- Federal funds rate
- SOFR
- €STR
- Historical rate observations

EURIBOR is licensed by its administrator and is not published by the ECB. It is out of scope unless a licensed provider is added.

### 3.7 Commodities

Collect (from Phase 8):

- Gold
- Silver
- Crude oil
- Natural gas
- Other supported commodities

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

Index data is subject to licensing. Only series the configured provider is permitted to distribute are collected.

## 4. External Data Providers

| Provider | Primary Purpose | Introduced |
| --- | --- | --- |
| Twelve Data | Stocks, ETFs, intraday forex, selected commodities | Phase 4 |
| CoinGecko | Cryptocurrency prices and market data | Phase 4 |
| ECB | Daily reference exchange rates, €STR, euro-area economic data | Phase 4 |
| FRED | US economic indicators, Treasury yields, federal funds rate | Phase 4 |
| Binance | Exchange-specific cryptocurrency market data at high frequency | Phase 8 |
| New York Fed | US money market rates (SOFR, EFFR) from the primary source | Phase 8 |

Additional providers may be introduced later.

Provider integrations must be independent.

Provider quotas, plans, and licensing terms change. Each integration documents the limits it was built against, and the limits themselves are configuration, not code.

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
- `GET /providers`
- `GET /collectors/status`
- `POST /collectors/{job}/run`

`GET` endpoints are unauthenticated and rely on the service being bound to a trusted interface (loopback by default).

`POST /collectors/{job}/run` triggers a manual collection. It requires the `X-API-Key` header to match the `ADMIN_API_KEY` environment variable, and is disabled when that variable is unset.

#### API Conventions

- **Errors:** every error response has the shape `{"error": {"code": "<machine_readable_code>", "message": "<human readable>", "details": <optional object>}}`.
- **Pagination:** list endpoints accept `limit` (default 100, maximum 1000) and `offset`, and return `{"items": [...], "limit": n, "offset": n, "has_more": bool}`.
- **Time ranges:** time-series endpoints accept `start` and `end` as ISO 8601 timestamps and return items in ascending time order.
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

Jobs for exchange-traded instruments run only during the relevant exchange's trading hours, so quota is not spent while markets are closed.

### FR-008: Grafana Integration

Grafana must visualize stored financial information.

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

- At startup, the application computes the calls each provider's enabled jobs will make (instruments × polls per period) and refuses to schedule a configuration that exceeds the quota, reporting which provider is over budget.
- At runtime, a per-provider rate limiter enforces the quota across scheduled jobs, manual triggers, and retries together.
- Requests that fetch several instruments in one call are preferred wherever the provider supports them.
- A request that would have to wait longer than a configured maximum for capacity is refused, not queued. An exhausted daily or monthly quota therefore fails fast.

The runtime limiter counts usage in memory, so its counts restart from zero when the backend restarts. When the provider nevertheless answers HTTP 429, all requests to that provider pause until its allowance is expected back. The startup check is what keeps planned usage inside daily and monthly quotas; the limiter guards against bursts, retries and manual triggers on top of it.

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

Raw observations are kept indefinitely by default; nothing is deleted automatically. Compression of older time-series chunks and any optional retention policy are configured in Phase 9.

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
- `quality_status`: `ok`, `suspect`.
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

One run of a collection job.

Fields:

- `id`
- `job_name`
- `provider_id`
- `started_at`
- `finished_at`
- `status` (`running`, `success`, `failed`)
- `records_written`
- `error`

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
| Crypto prices and market data | CoinGecko | 5 minutes | Free plan allows 10,000 calls per month; one batched call every 5 minutes uses about 8,640 |
| Exchange-specific crypto prices | Binance (Phase 8) | 1 minute | Public market-data limits are generous; availability varies by region |
| Intraday forex | Twelve Data | 15 minutes | Shares the Twelve Data daily quota |
| Stocks and ETFs | Twelve Data | 15 minutes, during trading hours | Shares the Twelve Data daily quota |
| Reference exchange rates | ECB | Daily | Published once per business day |
| Government yields and policy rates | FRED, ECB | Daily | Source data is daily |
| Money-market rates | New York Fed (Phase 8) | Daily | Source data is daily |
| Economic indicators | FRED, ECB | Daily check | Most series update monthly or quarterly |
| Historical data | All | On demand | Counts against the same quotas |

### Twelve Data Budget

The free plan allows 8 credits per minute and 800 per day, at one credit per symbol per request. The default instrument set is sized to fit:

| Group | Instruments | Polls per day | Credits per day |
| --- | --- | --- | --- |
| Stocks and ETFs (15 minutes over a 6.5-hour session) | 10 | 26 | 260 |
| Forex pairs (15 minutes, 24 hours) | 5 | 96 | 480 |
| **Total** | | | **740 of 800** |

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
