# Binance

Adapter: [backend/app/providers/binance.py](../../backend/app/providers/binance.py). Provider name in the seed file: `binance`.

Facts below were checked on **2026-11-17** against Binance's Spot API documentation (market-data endpoints, general API information and limits, and the "market data only" FAQ, last modified 2026-11-16) and against live responses from `data-api.binance.vision`. Re-check them when the adapter misbehaves.

## Supported Asset Classes

| Data | Endpoint | Stored as |
| --- | --- | --- |
| Last trade price of a spot pair | `/api/v3/ticker/24hr` | `price`, `traded`, `real_time`, in the quote asset |
| 24-hour turnover of a pair | `/api/v3/ticker/24hr` | `volume`, `traded`, in the quote asset (supported, not collected by default) |
| Candles of a pair | `/api/v3/klines` | OHLCV, prices in the quote asset, volume in the base asset |

Only cryptocurrency spot pairs. These are prices on one exchange. CoinGecko's prices are averages across exchanges in US dollars, and stay separate instruments.

## Symbols

- The seed writes each pair as `BASE/QUOTE` in Binance's asset codes, for example `BTC/USDT`. The adapter sends `BTCUSDT`.
- Writing the pair out keeps the quote asset known: Binance's ticker and kline responses do not state it.
- Binance quotes most pairs against Tether (`USDT`), a stablecoin, not against US dollars. The instruments are `BTC/USDT` and so on, with currency `USDT`, never merged with `BTC/USD`.
- Find pairs with `python -m app.cli search binance QUERY`, which matches base or quote assets among trading spot pairs.

## Endpoints Used

Base URL: `https://data-api.binance.vision/api/v3`. Binance documents this host for public market data only, with no authentication. `api.binance.com` serves the same endpoints.

| Endpoint | Used for | Request weight |
| --- | --- | --- |
| `GET /ticker/24hr?symbols=[...]` | Latest prices, up to 20 pairs per request | 2 for 1 to 20 pairs; 40 for 21 to 100; at most 100 per request |
| `GET /klines` | Candles, up to 1,000 per request, paged | 2 |
| `GET /exchangeInfo` | Symbol search | 20 |

### Ticker

- `symbols` is a JSON array without spaces, for example `["BTCUSDT","ETHUSDT"]`.
- The value stored is `lastPrice`. The timestamp stored is `closeTime`, the end of the rolling 24-hour window, which is when the statistics were taken.
- `quoteVolume` is the turnover over the last 24 hours, in the quote asset.

### Klines

- `startTime` and `endTime` are milliseconds, in UTC. With both, the oldest candles from `startTime` are returned, not past `endTime`. The adapter asks up to 1 ms before the exclusive end.
- Each row is `[open time, open, high, low, close, volume, close time, ...]`, with prices and volume as strings.
- The last row may still be forming. The adapter drops any candle whose close time has not passed.
- Daily candles open at 00:00 UTC.
- Intervals map as `1min` → `1m`, `1h` → `1h`, `1day` → `1d`, `1week` → `1w`, `1month` → `1M`.

## Authentication

None. No key is sent.

## Rate Limits

- `REQUEST_WEIGHT` is limited per IP address, not per key: 6,000 a minute in the documentation's example and in the live `exchangeInfo` response.
- Each response carries `X-MBX-USED-WEIGHT-1M`.
- Exceeding the limit gives HTTP 429 with `Retry-After`. Ignoring 429s gets the address banned with HTTP 418, for 2 minutes up to 3 days for repeat offenders, also with `Retry-After`.
- HTTP 403 means Binance's web application firewall refused the request, which can also be a rate-limit violation.

The seed sets 1,200 weight a minute, a fifth of the limit, so that other software on the same address keeps its share. Retries are few (`max_retries = 1`) and slow, because a ban is costly.

### Budget

One ticker request a minute for the 5 default pairs is weight 2: 2,880 a day, an average of 2 a minute. Daily candles add 2 per pair. A 400-day candle backfill for 5 pairs took 5 requests.

## Errors

| Response | Meaning | Handling |
| --- | --- | --- |
| HTTP 400, `code: -1121` | A symbol Binance does not list | `ProviderResponseError` "does not list a requested symbol"; the whole batch fails |
| HTTP 429 | Weight limit reached | Retried after `Retry-After` |
| HTTP 418 | IP address banned | `ProviderResponseError`, not retried |
| HTTP 403 | Firewall refusal | `ProviderResponseError`, not retried |
| HTTP 451 | Binance refuses this location | `ProviderResponseError`. Not in Binance's documentation; observed elsewhere for restricted regions. |

## Data Refresh Limitations

- Prices are real-time last trades on Binance, collected once a minute.
- Binance trades around the clock, so there is no session filter.

## Licensing Restrictions

- The market-data endpoints are public and need no account.
- No published terms specifically govern reuse or display of public market data. The Binance Vision dataset terms forbid using the Binance name or branding to market your own project.
- **Open question:** Binance's general Terms of Use were not reviewed for display rights. The dashboards name Binance as the source of these prices.

## Historical Data Availability

Candles go back to each pair's listing, for every interval. Ticker values are not historical: past prices come from candles.

## Test Fixtures

`backend/tests/fixtures/binance/` holds unmodified responses recorded from `data-api.binance.vision` on 2026-11-17:

| Fixture | Response |
| --- | --- |
| `ticker_24hr.json` | `/ticker/24hr` for BTCUSDT and ETHUSDT |
| `unknown_symbol.json` | HTTP 400, `code: -1121` |
| `klines_1m.json` | 3 one-minute BTCUSDT candles, the last still forming |
| `klines_1d.json` | 3 daily BTCUSDT candles, the last still forming |
| `exchange_info.json` | `/exchangeInfo` for BTCUSDT, ETHUSDT and ETHBTC |

## Sources

- [Market data endpoints](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints)
- [General API information and limits](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/limits)
- [Market data only URLs](https://developers.binance.com/docs/binance-spot-api-docs/faqs/market_data_only)
