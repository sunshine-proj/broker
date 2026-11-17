# ROADMAP — Financial Market Data Platform

## Development Strategy

Development must follow the phases sequentially.

Each phase should result in a working application. Do not start the next phase until the current phase is implemented, tested, and documented.

Use `[ ]` for tasks that are not complete. Mark a task `[x]` only after it has been completed and verified.

## Phase 0 — Project Foundation

**Goal:** Establish project structure and development environment.

### Tasks

- [x] Create repository structure.
- [x] Create Python backend project.
- [x] Configure Python dependencies.
- [x] Configure Ruff.
- [x] Configure pytest.
- [x] Create `.env.example`.
- [x] Create `.gitignore`.
- [x] Create Docker Compose configuration.
- [x] Configure PostgreSQL container.
- [x] Configure TimescaleDB.
- [x] Configure Grafana container.
- [x] Configure persistent database volumes.
- [x] Add service health checks.
- [x] Verify all containers start successfully.
- [x] Document local development setup.

### Acceptance Criteria

- Docker Compose starts all initial services.
- PostgreSQL accepts connections.
- Grafana is accessible.
- Backend starts successfully.
- Basic tests pass.

## Phase 1 — Backend Foundation

**Goal:** Implement the core backend architecture.

### Tasks

- [x] Create FastAPI application.
- [x] Create application configuration module.
- [x] Implement environment-based settings.
- [x] Configure structured logging.
- [x] Create API versioning structure.
- [x] Implement `/api/v1/health`.
- [x] Implement database connection management.
- [x] Configure SQLAlchemy.
- [x] Configure Alembic.
- [x] Create database session management.
- [x] Create initial database migration.
- [x] Implement global exception handling with the standard error response shape.
- [x] Configure the integration-test database (a separate database on the Compose `db` service).
- [x] Add API tests.
- [x] Add a single check script running Ruff and pytest.
- [ ] Add a continuous integration workflow that runs the check script.
- [x] Document backend architecture.

### Acceptance Criteria

- FastAPI starts successfully.
- Health endpoint works.
- Database connection works.
- Database migrations execute.
- Tests pass, locally and in continuous integration.

## Phase 2 — Financial Data Model

**Goal:** Implement common financial data structures as specified in PRD section 7.

### Tasks

- [x] Define asset type, observation type, freshness, quote kind, and quality status enumerations.
- [x] Define exchange model.
- [x] Define instrument model.
- [x] Define provider model.
- [x] Define instrument provider mapping model.
- [x] Define market observation model.
- [x] Define OHLCV model.
- [x] Define economic indicator model.
- [x] Define economic observation model.
- [x] Define collector execution model.
- [x] Define provider health model.
- [x] Implement database migrations.
- [x] Configure TimescaleDB hypertables.
- [x] Add appropriate indexes.
- [x] Implement repository interfaces.
- [x] Implement instrument repository.
- [x] Implement market data repository with the deduplication rules.
- [x] Implement economic data repository with the revision rules.
- [x] Implement the seed file loader for exchanges, instruments, provider mappings, and indicators.
- [x] Implement database integration tests.

### Acceptance Criteria

- All core models exist.
- Migrations execute successfully.
- Time-series data can be inserted.
- Historical data can be queried.
- Re-inserting an existing market observation writes nothing.
- A revised economic value is stored alongside the earlier one.
- Observations from two providers for the same instrument and time are both kept.
- Applying the seed file twice produces no duplicates.

## Phase 3 — Provider Integration Framework

**Goal:** Build a reusable architecture for external providers.

### Tasks

- [x] Define common provider interface.
- [x] Implement provider configuration.
- [x] Implement API authentication handling from environment variables.
- [x] Implement shared HTTP client.
- [x] Configure request timeouts.
- [x] Implement retry mechanism.
- [x] Implement exponential backoff.
- [x] Implement per-provider rate limiter with configurable quotas.
- [x] Implement provider error handling.
- [x] Implement response validation.
- [x] Implement common normalized response schemas.
- [x] Implement ingestion validation (timestamp, currency consistency, decimal parsing).
- [x] Implement the ingestion service that persists normalized responses.
- [x] Implement provider health monitoring.
- [x] Add provider unit tests.
- [x] Document provider integration requirements.

### Acceptance Criteria

- Providers can be implemented independently.
- Provider failures do not crash the application.
- Responses are validated; invalid observations are rejected and logged.
- Rate limits are respected, including by retries.
- Common schemas are used.

## Phase 4 — First Market Data Providers

**Goal:** Collect initial real financial market data.

Phase 4 is delivered as four sub-phases, one provider each. Complete, test, and document each sub-phase before starting the next. Each sub-phase adds the provider's instruments and mappings to the seed file and documents the provider as required by AGENTS.md section 5.

### Phase 4A — Twelve Data

- [x] Implement Twelve Data adapter.
- [x] Implement symbol search.
- [x] Implement latest price collection using batched requests.
- [x] Implement historical OHLCV collection.
- [x] Implement stock support.
- [x] Implement ETF support.
- [x] Implement forex support.
- [x] Add seed instruments sized to the Twelve Data budget.
- [x] Add provider tests.

### Phase 4B — CoinGecko

- [x] Implement CoinGecko adapter.
- [x] Implement cryptocurrency listings.
- [x] Implement current price collection.
- [x] Implement market capitalization collection.
- [x] Implement trading volume collection.
- [x] Implement supply and dominance collection.
- [x] Implement historical price collection.
- [x] Add provider tests.

### Phase 4C — ECB

- [x] Implement ECB SDMX integration.
- [x] Implement daily reference rate collection, stored as reference quotes.
- [x] Implement historical exchange rate collection.
- [x] Implement €STR collection.
- [x] Implement European economic data collection.
- [x] Add provider tests.

### Phase 4D — FRED

- [x] Implement FRED adapter.
- [x] Implement series observations.
- [x] Implement Treasury yield collection.
- [x] Implement Federal Funds rate collection.
- [x] Implement economic indicator collection, including revised values.
- [x] Add provider tests.

Verified with tests against FRED's documented response format. Not yet run against the live API: no key was available. FRED's terms prohibit storing or archiving FRED content; see [docs/providers/fred.md](docs/providers/fred.md).

### Acceptance Criteria

- Four provider adapters are implemented.
- At least three asset classes can be collected.
- Data is normalized.
- Observations are persisted.
- Provider errors are handled.
- Automated tests pass.

## Phase 5 — Collection Engine and Scheduler

**Goal:** Automate financial data collection.

### Tasks

- [x] Implement collector service.
- [x] Implement collection job configuration.
- [x] Implement scheduled jobs.
- [x] Implement manual collection trigger protected by the administrator key.
- [x] Implement configurable collection intervals.
- [x] Implement provider-specific scheduling.
- [x] Implement startup validation of the provider rate budget.
- [x] Implement trading-hours awareness for exchange-traded instruments.
- [x] Implement execution history.
- [x] Implement collection status tracking.
- [x] Implement retry handling.
- [x] Implement duplicate job prevention.
- [x] Implement failed job reporting.
- [x] Implement collector integration tests.

### Acceptance Criteria

- Collection runs automatically.
- Manual collection works, and is rejected without the administrator key.
- A configuration that exceeds a provider quota is refused at startup.
- Failed jobs do not stop other collectors.
- Execution history is stored.
- Duplicate jobs are prevented.

## Phase 6 — REST API

**Goal:** Expose normalized financial data.

### Tasks

- [x] Implement instrument listing endpoint.
- [x] Implement instrument details endpoint.
- [x] Implement latest prices endpoint.
- [x] Implement historical prices endpoint.
- [x] Implement OHLCV endpoint.
- [x] Implement forex rates endpoint.
- [x] Implement bond yields endpoint.
- [x] Implement economic indicators endpoint.
- [x] Implement provider listing endpoint.
- [x] Implement collector status endpoint.
- [x] Add pagination.
- [x] Add filtering.
- [x] Add request validation.
- [x] Add API integration tests.
- [x] Document API endpoints.

### Acceptance Criteria

- All endpoints return normalized data.
- Responses follow the API conventions in PRD FR-006.
- Historical queries work.
- Pagination works.
- Swagger documentation is available.
- API tests pass.

## Phase 7 — Grafana Integration

**Goal:** Create the initial financial market dashboards.

### Tasks

- [x] Configure PostgreSQL data source with a read-only database role.
- [x] Configure Grafana provisioning.
- [x] Create Global Market Overview dashboard.
- [x] Create Stock Market dashboard.
- [x] Create ETF Monitor dashboard.
- [x] Create Cryptocurrency Market dashboard.
- [x] Create Forex Monitor dashboard.
- [x] Create Government Bond Yields dashboard.
- [x] Create Interest Rates dashboard.
- [x] Create Economic Indicators dashboard.
- [x] Create Collector Monitoring dashboard.
- [x] Add dashboard variables.
- [x] Add time-range controls.
- [x] Add data freshness indicators.
- [x] Show the attribution each provider's licence requires (CoinGecko: "Powered by CoinGecko"; ECB: cite the ECB as the source; FRED: the API notice).
- [x] Version-control dashboard definitions.
- [x] Test dashboard queries.

### Acceptance Criteria

- Grafana connects to PostgreSQL.
- Dashboards load automatically.
- Financial data is displayed.
- Historical charts work.
- Dashboard configuration is stored in Git.

## Phase 8 — Additional Market Coverage

**Goal:** Expand supported financial instruments.

### Tasks

- [x] Implement Binance market data adapter.
- [x] Add cryptocurrency exchange-specific prices at 1-minute intervals.
- [x] Implement New York Fed adapter.
- [x] Add SOFR and EFFR from the New York Fed.
- [ ] Add additional stock exchanges. *Blocked: Twelve Data's free plan covers US listings only (for example, 4,329 of 4,331 London listings need the Grow plan).*
- [x] Add more ETF instruments.
- [x] Add additional forex pairs.
- [x] Add European government yields.
- [x] Add US Treasury yield curve.
- [x] Add commodities.
- [ ] Add gold and silver. *Gold is done; silver needs Twelve Data's Grow plan.*
- [ ] Add oil and natural gas. *Blocked: WTI and Brent need Twelve Data's Grow plan, natural gas is not offered, and the EIA republishes both under third-party licences.*
- [x] Create Commodities dashboard.
- [ ] Add market indices where licensing permits. *None is available: every index on Twelve Data needs a paid plan, and no other configured provider is licensed to supply index levels.*
- [x] Add additional macroeconomic indicators.
- [x] Improve instrument metadata.
- [x] Add integration tests.

Delivered beyond the list: a US Treasury adapter (the primary source of the yield curve, public domain and keyless, so the curve does not depend on FRED), and OBFR from the New York Fed.

### Acceptance Criteria

- Additional asset classes are supported.
- New providers use the common adapter interface.
- All additions fit the provider rate budgets.
- Existing functionality remains operational.

## Phase 9 — Data Quality and Reliability

**Goal:** Improve data integrity and production readiness.

### Tasks

- [x] Implement stale data detection.
- [x] Implement missing data detection.
- [x] Implement gap backfill that re-fetches detected missing ranges.
- [x] Implement duplicate observation checks.
- [x] Implement provider discrepancy reporting.
- [x] Implement data quality status updates.
- [x] Implement collection latency metrics.
- [x] Configure compression for older time-series chunks.
- [x] Implement optional database retention policies.
- [x] Test application recovery after restart.
- [x] Test provider outage scenarios.

### Acceptance Criteria

- Stale data is identifiable.
- Provider failures are visible.
- Data quality problems are reported.
- Historical observations are preserved.
- Restart recovery is documented.

## Phase 10 — Monitoring and Alerting

**Goal:** Monitor the health of the complete platform.

### Tasks

- [x] Add Prometheus metrics endpoint.
- [x] Expose collector metrics.
- [x] Expose provider request metrics.
- [x] Expose API request metrics.
- [x] Configure Prometheus.
- [x] Configure Grafana Prometheus data source.
- [x] Create system monitoring dashboard.
- [x] Create provider availability dashboard.
- [x] Create collection failure dashboard.
- [x] Configure data freshness alerts.
- [x] Configure collector failure alerts.
- [x] Configure database availability alerts.
- [x] Document alert configuration.

### Acceptance Criteria

- Application metrics are available.
- Provider health is visible.
- Failed collectors generate alerts.
- Critical infrastructure failures are detectable.

## Phase 11 — Security and Production Deployment

**Goal:** Prepare the application for reliable deployment.

### Tasks

- [x] Review secret management.
- [x] Remove development credentials.
- [x] Restrict database network access.
- [x] Configure production environment variables.
- [x] Configure secure Grafana access.
- [x] Review API exposure, including the unauthenticated read endpoints.
- [x] Configure HTTPS through reverse proxy.
- [x] Add container resource limits.
- [x] Review restart policies.
- [x] Configure database backups.
- [x] Test restoring from a backup.
- [x] Add deployment documentation.
- [x] Perform dependency security review.
- [x] Perform final integration tests.
- [x] Perform deployment verification.

### Acceptance Criteria

- Production configuration is documented.
- Secrets are protected.
- Database is not publicly exposed.
- Persistent storage is configured.
- Backup and recovery procedures are documented and tested.

## Phase 12 — Advanced Features

**Goal:** Extend the platform beyond basic market monitoring.

### Tasks

- [x] Implement watchlists.
- [x] Implement custom instrument groups.
- [x] Implement normalized currency conversion.
- [x] Implement percentage performance calculations.
- [x] Implement daily/weekly/monthly returns.
- [x] Implement volatility indicators.
- [x] Implement moving averages.
- [x] Implement relative asset performance.
- [x] Implement yield spread calculations.
- [x] Implement yield curve visualization.
- [x] Implement portfolio tracking.
- [ ] Implement optional custom frontend. Not required: Grafana is the interface (PRD section 10, decided 2026-11-17).
- [x] Implement user authentication if required. Accounts, sessions, API tokens and one login for the API and Grafana (PRD FR-017, 2026-11-17).
- [ ] Implement instrument management write endpoints once authentication exists. Not required: instruments are managed in the seed file (PRD FR-004, section 10).

### Acceptance Criteria

- Advanced calculations are tested.
- Watchlists work.
- Custom instrument groups work.
- Additional features do not compromise core collection reliability.

## Release Milestones

### MVP

**Target phases:** 0–7.

**Deliverable:** A working self-hosted application collecting stock, crypto, forex, and economic data, storing historical observations, and displaying them in Grafana.

### Version 1.0

**Target phases:** 8–11.

**Deliverable:** A reliable multi-provider financial market monitoring platform with broader market coverage, monitoring, alerting, and production deployment support.

### Future Development

Phase 12 and subsequent enhancements.

## Agent Execution Rules

- Work on one phase at a time.
- Do not skip phases.
- Review existing implementation before modifying files.
- Implement actual functionality rather than placeholder code.
- Add tests for every significant feature.
- Run tests before marking tasks complete.
- Update checkboxes only after verification.
- Update documentation when necessary.
- Do not break previously completed phases.
- Report completed tasks and remaining issues at the end of each phase.

The roadmap is the source of truth for implementation progress.
