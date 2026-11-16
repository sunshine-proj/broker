# CoinGecko

Adapter: [backend/app/providers/coingecko.py](../../backend/app/providers/coingecko.py). Provider name in the seed file: `coingecko`.

Facts below were checked on **2026-11-16** against CoinGecko's documentation and against live responses from the keyless public API. Re-check them when adding a key, changing plan, or when the adapter misbehaves.

## Supported Asset Classes

Cryptocurrency only. Values are aggregated across exchanges, so prices are stored as `indicative`, not `traded`. Exchange-specific, tradable prices come from Binance in Phase 8.

| Observation type | Unit | Currency | Source field |
| --- | --- | --- | --- |
| `price` | `currency` | vs currency | `current_price` |
| `market_cap` | `currency` | vs currency | `market_cap` |
| `volume` | `currency` | vs currency | `total_volume` (period not stated in the API reference) |
| `circulating_supply` | `count` | none | `circulating_supply` |
| `total_supply` | `count` | none | `total_supply` |
| `dominance` | `percent` | none | `/global` `market_cap_percentage` |

## Symbols

The provider symbol is `<coin id>/<vs currency>`, for example `bitcoin/usd`:

- The coin id is CoinGecko's own id. It is not the ticker: `ripple` for XRP, `binancecoin` for BNB.
- Find ids with `python -m app.cli search coingecko QUERY`.
- The vs currency is the currency values are quoted in, and must match the instrument's `currency`.

## Endpoints Used

| Endpoint | Used for | Requests |
| --- | --- | --- |
| `GET /coins/markets` | Price, market cap, volume, supply | 1 per vs currency per 250 coins |
| `GET /global` | Dominance | 1, only when dominance is requested |
| `GET /coins/{id}/market_chart/range` | History of price, market cap and volume | 1 per coin |
| `GET /search` | Finding coin ids | 1 |

Every call costs one credit, whichever endpoint it is.

### Current Values

- The adapter requests `precision=full`. JSON numbers are decoded as `Decimal`, so no digits are lost.
- The timestamp stored is each coin's `last_updated`.
- **Missing values:**
  - CoinGecko sends `null` for a missing value, and nothing is stored for it.
  - A market capitalization or circulating supply of `0` is also treated as missing, since a listed coin cannot have either. A volume of `0` is stored.
- **Unknown ids:** an id CoinGecko does not know is silently left out of the response. The adapter logs a warning naming the symbol.
- **Dominance:**
  - `/global` lists dominance for the ten largest coins only, keyed by lower-case ticker. Other coins get no dominance figure; nothing is estimated.
  - The figure's timestamp is `/global`'s own `updated_at`.
  - When two tracked coins share a ticker, dominance is skipped for them, because it cannot be told apart.

### History

- **Spacing:** CoinGecko chooses it. Ranges of 2 to 90 days come back hourly. Longer ranges come back daily, at 00:00 UTC. Timestamps are milliseconds.
- **Revisions:** CoinGecko states that historical values "may be revised for up to 2 days after the date". Stored observations are immutable, so the adapter takes only points older than 2 days. More recent values come from the regular current-value collection.
- **365-day limit:** the free plans serve history for the past 365 days only. The adapter shortens an older request to that limit and logs a warning.
- **Refusal beyond the limit:** if CoinGecko still refuses, it answers HTTP 401 with error code `10012`. The adapter reports that as a range error, not a credential error.
- **Volume period:** history volumes are the `total_volumes` series; the API reference does not state its period.

### Candles

Not implemented. On the free plans, `/coins/{id}/ohlc` picks the candle length from the range: 30 minutes, 4 hours, or 4 days. It cannot give daily or hourly candles. Crypto candles will come from Binance (Phase 8).

## Authentication

| Mode | How | Limits |
| --- | --- | --- |
| Keyless | No key at all | About 5 calls a minute, varying with load, shared by everyone on the same IP address. Not recommended by CoinGecko for production. |
| Demo (free) | Header `x-cg-demo-api-key` | 100 calls a minute, 10,000 successful calls a month |

- The key is read from `COINGECKO_API_KEY`. The authentication is marked `optional`, so the adapter sends the key when the variable is set and runs keyless otherwise.
- Both modes use `https://api.coingecko.com/api/v3`.
- Paid plans use a different host and header, which this adapter does not support.

## Rate Limits

| Limit | Value | Notes |
| --- | --- | --- |
| Per minute | 5 (keyless) or 100 (Demo) | Every request counts, including errors |
| Per month | 10,000 (Demo) | Only successful requests count. Counted from the first to the last day of the calendar month. |

- The seed configures 5 a minute, which suits both modes. With a Demo key it can be raised.
- The keyless API answers HTTP 429 with `Retry-After: 60`, and the client waits that long before retrying. `backoff_max_seconds` and `rate_limit_max_wait_seconds` are set above 60 for this.

### Budget

Monthly, for the 10 default coins:

| Collection | Requests |
| --- | --- |
| Current values every 5 minutes, all coins in one request | 8,640 |
| Dominance hourly | 720 |
| History refresh once a day, one request per coin | 300 |
| **Total** | **9,660 of 10,000** |

Adding coins costs nothing for current values (up to 250 per request), but 30 history requests a month each. Ten more coins with a daily history refresh would exceed the Demo allowance.

## Data Refresh Limitations

- CoinGecko describes its endpoints as "cached to around 1 to 5 minutes". The Demo plan's freshness is listed as "from 60 sec"; only paid plans are listed as real-time.
- All observations are therefore stored with `freshness = delayed`.

## Licensing Restrictions

- **Attribution is required** on the Demo and keyless plans. The API terms ask for "Powered by CoinGecko" to be displayed prominently, in a legible font no smaller than size 10. CoinGecko's attribution guide also accepts wordings such as "Data provided by CoinGecko". Grafana dashboards showing CoinGecko data must carry it (Phase 7).
- No redistribution: access to the API, or part of it, may not be sold, sub-licensed, re-distributed or syndicated.
- **Storage:**
  - The terms discourage caching or storing data. Where it is stored, they ask for it to be refreshed at least every 24 hours.
  - **Open question:** whether keeping a historical archive, as this platform does, is permitted under the free plans is not settled here. Check CoinGecko's terms before relying on it.
- The Demo plan is presented as being for testing and exploration. Commercial use is licensed on paid plans.

## Historical Data Availability

- The past 365 days on the free plans. Paid plans go back further.
- Daily points beyond 90 days, hourly within.

## Test Fixtures

`backend/tests/fixtures/coingecko/` holds unmodified responses recorded from the keyless API on 2026-11-16:

| Fixture | Response |
| --- | --- |
| `coins_markets.json` | `/coins/markets` for four coins plus an unknown id |
| `global.json` | `/global` |
| `search.json` | `/search` for "bitcoin" |
| `market_chart_hourly.json` | Two days of hourly bitcoin history |
| `market_chart_daily.json` | 100 days of daily ethereum history |
| `market_chart_unknown_coin.json` | History for an unknown coin (HTTP 404) |
| `market_chart_beyond_history.json` | History older than 365 days (HTTP 401, code 10012) |
| `rate_limited.json` | HTTP 429 body (sent with `Retry-After: 60`) |

## Sources

- [API documentation (Demo and public)](https://docs.coingecko.com/v3.0.1/reference/introduction)
- [Keyless public API](https://docs.coingecko.com/docs/keyless-public-api)
- [Rate limits and errors](https://docs.coingecko.com/docs/common-errors-rate-limit)
- [Pricing and FAQ](https://www.coingecko.com/en/api/pricing)
- [API terms](https://www.coingecko.com/en/api_terms)
- [Attribution guide](https://brand.coingecko.com/resources/attribution-guide)
