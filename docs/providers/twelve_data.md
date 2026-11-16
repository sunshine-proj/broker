# Twelve Data

Adapter: [backend/app/providers/twelve_data.py](../../backend/app/providers/twelve_data.py). Provider name in the seed file: `twelve_data`.

Facts below were checked on **2026-11-16** against Twelve Data's documentation and against live responses from a free Basic plan key. Re-check them when changing plan or when the adapter misbehaves.

## Supported Asset Classes

| Asset class | Endpoint | Stored as |
| --- | --- | --- |
| Stocks | `/quote`, `/time_series` | `price`, `traded`, currency from the response |
| ETFs | `/quote`, `/time_series` | `price`, `traded`, currency from the response. This is the market price, not NAV. |
| Forex | `/quote`, `/time_series` | `rate`, `indicative`, currency taken from the pair's quote currency |

Twelve Data also covers crypto, commodities and indices. They are not used here: crypto comes from CoinGecko (Phase 4B), and commodities and indices are Phase 8.

## Endpoints Used

| Endpoint | Used for | Cost |
| --- | --- | --- |
| `GET /quote` | Latest price or rate. Several symbols per request, comma-separated. | 1 credit per symbol |
| `GET /time_series` | Historical candles | 1 credit per request |
| `GET /symbol_search` | Finding symbols for the seed file (`python -m app.cli search twelve_data QUERY`) | 1 credit per request |

### `/quote`

- One symbol is answered with the quote object itself. Several symbols are answered with an object keyed by symbol.
- The value stored is `close`, the latest price. The timestamp stored is `last_quote_at`, the time of the last one-minute candle. `timestamp` is not used, because it is the open time of the daily bar.
- Stocks and ETFs carry `currency`, `exchange` and `mic_code`. Forex pairs carry none of them, so the quote currency is taken from the `BASE/QUOTE` symbol.
- Outside trading hours, stocks return the last close with its original `last_quote_at`. Re-collecting it writes nothing.

### `/time_series`

The adapter requests `timezone=UTC`, `order=asc`, `adjust=none` and `outputsize=5000`, and pages through longer ranges.

- **Unadjusted prices.** Stored candles are immutable, so they must not change when a later split or dividend rewrites adjusted history. The provider's default is split-adjusted.
- **Daily bars** have a date-only `datetime`, stored as midnight UTC of that date. The provider ignores `timezone` for daily and longer bars and uses the exchange's local date.
- **Intraday bars** are returned in UTC as requested.
- **Forming bar.** The response includes the bar that is still open. The adapter drops any bar whose interval has not ended.
- **Volume and currency.** Forex bars have no `volume`, and their `meta` gives currency names, not codes. The quote currency is taken from the symbol instead.
- **Weekend forex bars.** The provider returns forex daily bars for Saturdays and Sundays, with very small price changes. They are stored as received.
- **No data.** When no bars exist for the dates, the provider answers HTTP 400 with the message `No data is available on the specified dates`. The adapter treats this as an empty result.

## Authentication

An API key, sent as the header `Authorization: apikey <key>`. The header is used, not the `apikey` query parameter, so the key never appears in a URL.

The key is read from the environment variable `TWELVEDATA_API_KEY`. Without it the provider cannot be built and collection is recorded as a provider failure.

## Rate Limits

Free Basic plan:

| Limit | Value | Resets |
| --- | --- | --- |
| Per minute | 8 credits | At the start of each minute |
| Per day | 800 credits | At midnight UTC |

- Every request, including one rejected with HTTP 429, uses credits.
- Each response carries `api-credits-used` and `api-credits-left` headers. The adapter does not read them.
- The configuration in `seed.toml` mirrors these limits, with `resets_on_boundary = true`. After a 429, the client pauses all Twelve Data requests until the next minute instead of retrying early.
- `max_batch_size = 8`: a quote request never asks for more symbols than one minute allows.

### Budget

PRD section 8: 10 stocks and ETFs polled every 15 minutes over a 6.5-hour session use 260 credits a day. 5 forex pairs polled every 15 minutes around the clock use 480. Together that is 740 of 800 a day.

On-demand candle collection and symbol searches come out of the remaining 60. A daily candle refresh for all 15 instruments costs 15.

## Errors

| Response | Meaning | Handling |
| --- | --- | --- |
| HTTP 429, `code: 429` | Credits exhausted | Pause until the next minute, then retry |
| HTTP 401, `code: 401` | Missing or invalid key | `ProviderAuthError`, not retried |
| HTTP 404, `code: 404` | Unknown symbol (single-symbol request) | `ProviderResponseError` |
| HTTP 200 with a per-symbol `{"status": "error", "code": 404}` | Unknown symbol inside a batch | That symbol is logged and skipped; the others are kept |
| HTTP 400, "No data is available..." | Valid request, no bars | Empty result |
| Other HTTP 400 | Invalid parameter | `ProviderResponseError` |
| HTTP 200 with `{"status": "error"}` at the top level | Error reported in the body | Raised by its `code` |

## Data Refresh Limitations

- The Basic plan is documented as real-time for US equities, ETFs and forex. Observations are stored with `freshness = real_time`. If the plan changes, update the adapter.
- Only US markets are included in the Basic plan.
- Forex rates are aggregated quotes, not executable prices.

## Licensing Restrictions

- Individual plans are for personal, internal, non-commercial use. Use by a company needs a Business plan, even internally.
- The Basic plan's pricing entry reads "internal non-display usage only".
- **Open question:** whether showing the data on a personal Grafana dashboard counts as display under the Basic plan is not settled here. Check Twelve Data's terms or ask them before relying on it.
- Data may not be redistributed. Do not expose this platform's API or Grafana to other people with Twelve Data data in it.

## Historical Data Availability

- End-of-day data back to the earliest records the provider holds.
- More than 20 years of forex history on free plans.
- Intraday history is available but its depth is not stated for the Basic plan.

At most 5,000 bars come back per request; the adapter pages through longer ranges.

## Test Fixtures

`backend/tests/fixtures/twelve_data/` holds unmodified responses recorded from the live API on 2026-11-16, with no key in them:

| Fixture | Response |
| --- | --- |
| `quote_batch.json` | `/quote` for `AAPL,EUR/USD,SPY` |
| `quote_single.json` | `/quote` for `USD/JPY` |
| `quote_unknown_symbol.json` | `/quote` for a batch with one unknown symbol |
| `quote_single_unknown.json` | `/quote` for one unknown symbol (HTTP 404) |
| `time_series_daily.json` | AAPL daily bars |
| `time_series_forex.json` | EUR/USD hourly bars, including the forming one |
| `time_series_no_data.json` | Dates with no bars (HTTP 400) |
| `symbol_search.json` | Search for "apple" |
| `rate_limited.json` | HTTP 429 |
| `unauthorized.json` | HTTP 401 |

## Sources

- [API documentation](https://twelvedata.com/docs)
- [Pricing](https://twelvedata.com/pricing)
- [API credits and limits](https://support.twelvedata.com/en/articles/5194820-api-credits-limits)
- [Credits](https://support.twelvedata.com/en/articles/5615854-credits)
