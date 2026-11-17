# AGENTS.md — Development Instructions

## 1. Project Overview

Build a modular financial market data aggregation and visualization platform.

The application collects financial data from multiple external providers, normalizes it into a common internal format, stores historical data, and exposes it through Grafana and REST APIs.

### Supported Asset Classes

- Stocks and shares
- Exchange-traded funds (ETFs)
- Cryptocurrencies
- Foreign exchange (Forex)
- Government bonds and bond yields
- Interest rates
- Commodities
- Economic indicators
- Market indices

Refer to [PRD.md](PRD.md) for functional and technical requirements.

Refer to [ROADMAP.md](ROADMAP.md) for implementation phases and development priorities.

## 2. General Development Rules

- Follow the roadmap in sequential order.
- Do not implement future phases unless explicitly requested.
- Complete and test the current phase before moving forward.
- Keep the application modular and provider-independent.
- Avoid unnecessary abstractions and overengineering.
- Prefer simple, maintainable implementations.
- Never hardcode API keys, credentials, or secrets.
- Never commit `.env` files containing secrets.
- Document configuration changes.
- Update [README.md](README.md) when installation or usage instructions change.

## 3. Technology Stack

Use the following technologies unless a requirement explicitly justifies a change.

| Component | Technology |
| --- | --- |
| Language | Python 3.12+ |
| Backend | FastAPI |
| HTTP client | httpx (async client) for all provider calls |
| Data validation | Pydantic v2 |
| Settings | pydantic-settings |
| Database | PostgreSQL |
| Time-series database | TimescaleDB |
| ORM | SQLAlchemy 2.x, async API |
| Database driver | asyncpg |
| Migrations | Alembic |
| Scheduling | APScheduler 3.x (`AsyncIOScheduler`) |
| Testing | pytest |
| Linting and formatting | Ruff |
| Visualization | Grafana |
| Deployment | Docker Compose |
| Logging | Python `logging` with a JSON formatter |

Use compatible stable versions and pin dependencies.

Add a dependency in the phase that first uses it, not earlier.

The FastAPI test client is a development-only dependency. Starlette is moving its test client from `httpx` to `httpx2`; use whichever the pinned Starlette version requires. This does not change the HTTP client used for provider calls.

## 4. Architecture Principles

### Provider Independence

- External data providers must be implemented as independent adapters.
- Business logic must never depend directly on a particular provider's response format.
- All provider responses must be converted into common internal schemas.

### Separation of Responsibilities

Keep these responsibilities separate:

- API endpoints
- Data providers
- Data collection
- Data normalization
- Business services
- Database repositories
- Database models
- Scheduling
- Data integrity

Every market observation should preserve:

- Instrument identifier
- Provider identifier
- Observation timestamp
- Data ingestion timestamp
- Value
- Currency or measurement unit
- Data source
- Data quality/status where applicable

Never silently overwrite observations from different providers.

The data model, including which table holds which kind of value and the deduplication and revision rules, is specified in [PRD.md](PRD.md) section 7. Follow it; change the PRD first if the model needs to change.

### Historical Data

- Historical observations must be stored persistently.
- Do not depend on external providers to retain historical information.

### Resilience

External provider failures must not crash the entire application.

Implement:

- Connection timeouts
- Retry policies
- Rate-limit handling
- Error logging
- Provider health status
- Graceful degradation

Retries must respect provider rate limits.

Provider quotas are configuration. Scheduled jobs, manual triggers, and retries share one per-provider rate budget (PRD FR-011). Before adding instruments or shortening an interval, check that the result still fits the provider's quota.

## 5. Financial Data Rules

- Always distinguish real-time, delayed, and end-of-day data.
- Store timestamps in UTC.
- Preserve original provider timestamps when available.
- Do not assume every price is denominated in USD.
- Store currency explicitly.
- Do not mix indicative rates with executable trading prices.
- Distinguish bond prices from bond yields.
- Distinguish market prices from NAV.
- Preserve historical observations.
- Do not fabricate missing values.

Every provider integration must document:

- Supported asset classes
- Available endpoints
- Authentication method
- Rate limits
- Data refresh limitations
- Licensing restrictions
- Historical data availability

Write this in `docs/providers/<provider>.md`, and record the date the limits were checked against the provider's own documentation.

Do not add data the configured provider is not licensed to supply (for example EURIBOR or PMI).

## 6. API Development

- Use REST endpoints with versioning.
- Base path: `/api/v1`.
- Use Pydantic schemas for request and response validation.

Provide:

- Pagination where appropriate
- Consistent error responses
- OpenAPI documentation
- Input validation
- Health endpoints

Follow the API conventions in PRD FR-006 for the error shape, pagination, time ranges, decimal serialization, and timestamps.

Do not expose provider API credentials through public endpoints.

Provider credentials are read from environment variables only. Never store them in the database.

Every endpoint except the health check requires a signed-in user (PRD FR-017); include new routers with the `require_user` dependency in `app/api/v1/__init__.py`. The API is read-only except for the manual collection trigger, which also requires the administrator key. A new write endpoint must be in the PRD first and check the user's role.

## 7. Database Development

- Use PostgreSQL and TimescaleDB.
- Use Alembic migrations for schema changes.
- Never modify production database schemas manually without a migration.

Prefer:

- UUID primary keys for entity tables
- Natural composite primary keys that include the time column for hypertables (TimescaleDB requires this; do not add a standalone surrogate `id`)
- Foreign key constraints
- Appropriate indexes
- Hypertables for large time-series tables
- Unique constraints for deduplication
- Explicit decimal/numeric types for financial values

Avoid floating-point types for financial prices where precision matters.

## 8. Testing Requirements

Every implementation phase must include appropriate tests.

Test:

- Business logic
- Data normalization
- Provider adapters
- API endpoints
- Database operations
- Error handling
- Rate-limit handling

External APIs should be mocked during automated tests.

Never require paid external API access to run the basic test suite.

Unit tests need neither network nor database. Database tests are marked `integration` and run against a separate test database on the Compose `db` service, because the schema depends on TimescaleDB and cannot run on SQLite.

## 9. Grafana

- Grafana must connect to the internal database.
- Do not make Grafana responsible for collecting and storing the primary financial dataset.
- Dashboards should be version-controlled and provisioned automatically.
- Use reusable SQL queries and meaningful dashboard names.

## 10. Docker

The complete application should eventually run using Docker Compose.

Services should have:

- Health checks
- Persistent storage
- Environment-based configuration
- Restart policies where appropriate
- Internal networking

Do not expose databases publicly by default.

## 11. Documentation

Maintain:

- `README.md`: installation and usage
- `PRD.md`: requirements and product behavior
- `ROADMAP.md`: implementation status
- `SKILLS.md`: coding conventions
- `AGENTS.md`: instructions for coding agents
- `docs/providers/`: one document per provider integration

When two documents disagree, the PRD decides requirements and the data model, the roadmap decides order and status, and this file decides process. Fix the disagreement in the same change.

Update documentation whenever implementation changes affect documented behavior.

## 12. Agent Workflow

### Before Implementing a Task

- Read [AGENTS.md](AGENTS.md).
- Read [PRD.md](PRD.md).
- Read [ROADMAP.md](ROADMAP.md).
- Read [SKILLS.md](SKILLS.md).
- Inspect the existing codebase.
- Identify the current development phase.
- Review relevant tests.
- Create a short implementation plan.

### During Implementation

- Make small, focused changes.
- Follow existing conventions.
- Avoid unrelated modifications.
- Add or update tests.
- Update documentation if necessary.

### After Implementation

- Run relevant tests.
- Run formatting and linting.
- Check Docker configuration if affected.
- Report completed tasks.
- Identify limitations and outstanding work.
- Update [ROADMAP.md](ROADMAP.md) checkboxes only for completed and verified tasks.

## 13. Definition of Done

A task is complete only when:

- Implementation is finished.
- Relevant tests pass.
- No known regressions are introduced.
- Configuration is documented.
- Security requirements are respected.
- Related roadmap tasks are updated.

Never mark a task complete merely because code has been generated.
