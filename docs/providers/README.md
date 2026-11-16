# Provider Integration Requirements

How to add an external data provider. Every provider is an independent adapter built on the shared framework in `backend/app/providers/`. The framework is described in [architecture.md](../architecture.md); this document is the checklist an integration must meet.

## What the Framework Does for You

An adapter does not implement any of these. It gets them by making every request through the `ProviderHttpClient` it is given.

| Concern | Behaviour |
| --- | --- |
| Authentication | Adds the credential as a query parameter or header, as configured. The key is read from an environment variable. |
| Timeout | Every request has a timeout (10 seconds by default). |
| Retries | Timeouts, connection failures, HTTP 5xx and HTTP 429 are retried with exponential backoff. Other responses are not. |
| Rate limits | Every attempt, including a retry, takes credits from the provider's rate limiter before it is sent. |
| HTTP 429 | Honours `Retry-After`. Without it, waits for the next period boundary of any limit marked `resets_on_boundary`. Either way the whole limiter is paused until then, so other requests to the provider wait too. If the wait is longer than `backoff_max_seconds`, the request fails instead of retrying early. |
| Precision | JSON numbers are decoded as `Decimal`, never `float`. |
| Errors | Failures are raised as `ProviderError` subclasses, with no credential or URL in the message. |

## Writing an Adapter

Create `backend/app/providers/<name>.py`:

```python
from app.providers.base import Capability, Provider
from app.providers.factory import register_adapter


@register_adapter
class ExampleProvider(Provider):
    name = "example"  # must equal the provider's name in seed.toml
    capabilities = frozenset({Capability.QUOTES})

    async def fetch_quotes(self, symbols):
        raw = await self._client.get_json("/quotes", {"symbols": ",".join(symbols)}, cost=len(symbols))
        parsed = self._validate(ExampleQuotesResponse, raw)
        return [Quote(...) for item in parsed.data]
```

A complete small adapter, used by the framework's own tests, is in `backend/tests/providers_support.py`. The real adapters, [Twelve Data](twelve_data.md), [CoinGecko](coingecko.md) and the [ECB](ecb.md), are fuller examples.

Add the module to the imports in `backend/app/providers/__init__.py`, which is what registers it.

### Rules

1. **Implement only what the provider offers.** Override `fetch_quotes`, `fetch_quote_history`, `fetch_candles`, `fetch_indicator` or `search_symbols` and list the matching `Capability`. Methods left alone raise `UnsupportedOperationError`.
2. **Return normalized schemas only.** `Quote`, `Candle`, `IndicatorValue` and `SymbolMatch` from `app/schemas/normalized.py`. No provider-specific shape leaves the adapter.
3. **Model the provider's response with Pydantic and validate it** with `self._validate(Model, raw)`. A response that does not match becomes a `ProviderResponseError`. This includes error bodies sent with HTTP 200.
4. **Parse values with the helpers in `app/providers/parsing.py`.**
   - `parse_decimal` for every number. It refuses floats.
   - `parse_utc_datetime` for timestamps with an offset.
   - `assume_utc` only where the provider documents its timestamps as UTC.
   - `parse_utc_date` for date-only values.
   - `parse_epoch_seconds` and `parse_epoch_milliseconds` for epoch times.
5. **Set every field explicitly.** State `observation_type`, `unit`, `freshness`, `quote_kind` and, for money amounts, `currency`, from the provider's documentation. Do not rely on defaults or assume USD.
6. **Honour `observation_types`** in `fetch_quotes`: return only those types, and skip requests that would only serve other types.
7. **Pass the real `cost`.** If the provider charges one credit per symbol, a batched request for ten symbols is `cost=10`.
8. **Prefer batched requests** where the provider supports them.
9. **Never fill gaps.** A missing value is omitted, not estimated or carried forward.
10. **Take only settled history.** If the provider revises recent values, leave them out of history; stored observations cannot be corrected later.
11. **Do not touch the database.** The adapter returns data; the ingestion service stores it.

### What Each Normalized Field Means

| Field | Meaning |
| --- | --- |
| `provider_symbol` | The provider's own symbol. Ingestion maps it to an instrument through the seed file. |
| `observed_at` | The provider's timestamp for the value, in UTC. Not the time of the request. |
| `freshness` | `real_time`, `delayed` or `end_of_day`, as the provider's plan delivers it. |
| `quote_kind` | `traded` for a market price, `indicative` for a non-executable quote, `reference` for an official fixing such as an ECB reference rate. |
| `unit` | `currency` for money amounts, `percent` for yields and rates, `points` for index levels, `count` for supply and volume. |
| Candle `timestamp` | The candle's open time. Return closed candles only. |
| Candle `interval` | One of `1min`, `5min`, `15min`, `30min`, `1h`, `4h`, `1day`, `1week`, `1month`. Map the provider's own notation to these. |

## Configuring the Provider

Add the provider to `backend/seed.toml`. The file's header documents every setting.

```toml
[[providers]]
name = "example"
base_url = "https://api.example.com"

[providers.configuration]
rate_limits = [
    { limit = 8, period = "minute" },
    { limit = 800, period = "day" },
]
auth = { type = "query", name = "apikey", env = "EXAMPLE_API_KEY" }
```

- `base_url` must use `https`.
- `rate_limits` must be the provider's documented quota for the plan in use. List every limit that applies. Mark a limit `resets_on_boundary = true` when the provider restores it at the start of each period (on the minute, at midnight UTC).
- `max_batch_size` caps the symbols per batched request. Set it to no more than the smallest per-period limit when each symbol costs a credit.
- `auth.env` is the name of an environment variable, never the key. It must end in `_API_KEY` or `_TOKEN`.
- `auth.optional = true` makes the key optional, for providers that also serve anonymous requests.
- Add the variable to `.env.example` (empty), to the `backend-environment` block in `docker-compose.yml`, and to the configuration table in the README.

Then add the provider's instruments and indicators to the seed file, with the symbol the provider uses for each, sized to fit the rate limits (PRD FR-011).

## What Ingestion Rejects

The ingestion service validates everything an adapter returns. A rejected value is logged with its reason and is not stored or corrected.

| Rejected when | Applies to |
| --- | --- |
| No enabled instrument or indicator is mapped to the symbol | All |
| The timestamp is more than 5 minutes in the future | All |
| The currency differs from the instrument's currency | Quotes, candles |
| A price, NAV or market capitalization has no currency | Quotes |
| `high` is below, or `low` is above, another price of the candle | Candles |
| Volume is negative | Candles |

Naive timestamps and non-finite numbers cannot be put into a normalized schema at all.

## Tests an Adapter Must Have

Tests mock HTTP with `httpx.MockTransport` and fixture responses under `backend/tests/fixtures/<name>/`. They must not call the real API. Cover at least:

- a successful response for each capability, asserting every normalized field;
- a malformed response and an error body, each raising `ProviderResponseError`;
- an HTTP 429 response;
- a timeout;
- the `cost` charged for a batched request.

Record fixtures from real responses, with any credential removed.

## Provider Document

Each provider has `docs/providers/<name>.md` covering, with the date the facts were checked against the provider's own documentation:

- supported asset classes;
- endpoints used;
- authentication method;
- rate limits, per plan;
- data refresh limitations (real-time, delayed, end-of-day);
- licensing restrictions;
- historical data availability.

## Checklist

- [ ] Adapter module with `@register_adapter`, importing nothing from other adapters.
- [ ] Response models validated with `self._validate`.
- [ ] Provider, instruments and indicators in `seed.toml`, within the rate budget.
- [ ] Credential variable in `.env.example`, `docker-compose.yml` and the README.
- [ ] Tests with fixtures; no live calls.
- [ ] `docs/providers/<name>.md`.
- [ ] `scripts/check.sh` passes.
