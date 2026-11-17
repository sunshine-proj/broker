# European Central Bank (ECB)

Adapter: [backend/app/providers/ecb.py](../../backend/app/providers/ecb.py). Provider name in the seed file: `ecb`.

Facts below were checked on **2026-11-16** (the yield curves on **2026-11-17**) against live responses from the ECB Data Portal API and against the ECB's pages on reference rates, €STR and reuse. The Data Portal's own API help pages returned HTTP 503 during the check, so the API behaviour documented here comes from the live responses. Re-check when the adapter misbehaves.

## Supported Data

| Data | Dataflow | Stored as |
| --- | --- | --- |
| Euro foreign exchange reference rates | `EXR` | Instrument quotes: `rate`, `reference`, `end_of_day`, in the quote currency |
| Euro short-term rate (€STR) | `EST` | Instrument quotes: `rate`, `reference`, `end_of_day`, unit `percent` |
| ECB key interest rates | `FM` | Instrument quotes, as for €STR |
| Euro-area government yield curves | `YC` | Instrument quotes: `yield`, `reference`, `end_of_day`, unit `percent` |
| Euro-area economic statistics | any other dataflow, for example `ICP`, `LFSI`, `MNA`, `GFS` | Economic observations |

## Symbols and Series Codes

Every series is written `<dataflow>/<series key>`, exactly as the ECB names it:

| Series | Meaning |
| --- | --- |
| `EXR/D.USD.EUR.SP00.A` | Daily US dollars per euro |
| `EST/B.EU000A2X2A25.WT` | €STR |
| `FM/B.U2.EUR.4F.KR.DFR.LEV` | Deposit facility rate |
| `YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y` | 10-year spot rate of the AAA euro area yield curve |
| `ICP/M.U2.N.000000.4.ANR` | Euro-area HICP inflation, annual rate |

- Every position of the key must be filled. Wildcards and `+` are not allowed in the seed file; the adapter builds combined keys itself.
- Series can be browsed at <https://data.ecb.europa.eu>.

## Endpoint

`GET https://data-api.ecb.europa.eu/service/data/{dataflow}/{key}`, with `format=jsondata` and `detail=dataonly` (SDMX-JSON, observation values only).

| Purpose | Parameters |
| --- | --- |
| Current values | `lastNObservations=5`: the last five observations of each series. A day missed by an earlier collection is filled in, and values already stored are not written again. |
| History and indicators | `startPeriod` and `endPeriod` as dates. Both are inclusive, so the adapter asks for the day before the exclusive end. |

### Batching

The `+` operator selects several values at one key position. `EXR/D.USD+GBP+JPY+CHF.EUR.SP00.A` returns four series in one response.

- The adapter combines all series of one dataflow into one request, one position at a time, and drops any extra series the combination matches.
- Current values for the default series take 4 requests: one each for `EXR`, `EST`, `FM` and `YC`. Indicators take one request per series.

### Response Format

- `structure.dimensions.series` lists each key position with its values.
- `structure.dimensions.observation` lists `TIME_PERIOD`.
- `dataSets[0].series` is keyed by value indices, such as `0:3:0:0:0`. Each series maps an observation index to `[value]`.
- Values are JSON numbers and are decoded as `Decimal`. A `null` value is skipped.

### Periods

Each observation is dated by the first day of its period, at midnight UTC:

| Frequency | Example | Stored as |
| --- | --- | --- |
| Daily, business-daily | `2026-11-13` | 2026-11-13 |
| Monthly | `2026-09` | 2026-09-01 |
| Quarterly | `2026-Q1` | 2026-01-01 |
| Half-yearly | `2026-S1` | 2026-01-01 |
| Annual | `2025` | 2025-01-01 |

### Responses

| Response | Meaning | Handling |
| --- | --- | --- |
| HTTP 200, empty body | The series exists but has no observations in the range | Empty result |
| HTTP 404, `application/problem+json` | No series matches the key | `ProviderResponseError` |
| HTTP 400, HTML page | Malformed key | `ProviderResponseError` |

## What the Values Mean

- **Exchange rates:**
  - These are units of the quote currency per euro, for 29 currencies.
  - The ECB sets them around 14:10 CET and publishes them around 16:00 CET on every working day except TARGET closing days.
  - They are published for information only, and the ECB discourages using them for transactions. They are stored as `reference` quotes, beside any market rate another provider supplies for the same pair.
  - They are stored at midnight UTC of the reference date, not at the time they were set.
- **€STR:**
  - Published each TARGET2 business day for the previous business day's transactions. The stored date is the date the rate refers to.
  - The ECB's correction policy for published rates is not covered here. Collect after its 08:00 CET publication.
- **Key interest rates:** the `FM` series list a value only on the dates a rate changed, not every day. A rate holds until its next observation. Nothing is filled in between.
- **Yield curves:**
  - The ECB estimates them each TARGET business day from euro area central government bonds (Svensson model). `G_N_A` uses AAA-rated issuers only, `G_N_C` all issuers.
  - `SR_<maturity>` is the spot (zero-coupon) rate. Par yields (`PY_`) and instantaneous forward rates (`IF_`) are also published and could be added the same way.
  - They are model estimates, so they are stored as `reference` yields with every decimal the ECB sends (10 places).
  - Country curves (Germany, France, Italy) are not in `YC`. The ECB's per-country long-term interest rates (`IRS`) are monthly averages, which are not point-in-time yields, so they are not collected.
- **Economic statistics** may be revised. A changed value for a period already stored is kept as a revision (PRD section 7).

## Authentication

None.

## Rate Limits

None published. The seed sets a courtesy limit of 30 requests a minute, far above what collection needs (4 requests for current values, one per indicator).

## Data Refresh Limitations

- Exchange rates, €STR and the yield curves are daily. Collecting them once a day after 16:00 CET is enough.
- Key interest rates change only at ECB monetary policy decisions.
- Economic statistics are monthly, quarterly or annual, with publication lags of weeks to months. Industrial production (`STS`) was last published for September 2025 when checked on 2026-11-17; M3 (`BSI`) for September 2026.
- **Euro-area HICP inflation (`ICP/M.U2.N.000000.4.ANR`, and core HICP `ICP/M.U2.N.XEF000.4.ANR`) currently has no values after December 2025**, under either euro-area code (`U2`, `I9`), and a search of the `ICP` dataflow found no 2026 data for any item. The series is still collected for its history. Check whether the ECB has moved current HICP data to a new series key.

## Licensing

- The ECB permits free reuse of the information on its website, provided the ECB is cited as the source and the information is reproduced accurately.
- Modified information must be marked as modified.
- Dashboards showing ECB data must name the ECB as the source (Phase 7).

## Historical Data Availability

The full history of each series. Exchange rates go back to 1999, and €STR to October 2019.

## Test Fixtures

`backend/tests/fixtures/ecb/` holds unmodified responses recorded from the live API on 2026-11-16:

| Fixture | Response |
| --- | --- |
| `exr_latest.json` | Last 3 USD, GBP, JPY and CHF reference rates, one combined request |
| `estr_latest.json` | Last 3 €STR values |
| `dfr_latest.json` | Last 2 deposit facility rate changes |
| `key_rates.json` | Deposit facility and main refinancing rates, combined request |
| `hicp.json` | HICP inflation from January 2025 |
| `unemployment.json` | Unemployment rate from January 2026 |
| `gdp.json` | GDP growth from 2025 |
| `government_debt.json` | Government debt from 2020 |
| `yield_curve_latest.json` | Last 2 AAA 2-year and 10-year spot rates, combined request (recorded 2026-11-17) |
| `no_data.json` | Empty body for a range without observations |
| `unknown_series.json` | HTTP 404 for a key that matches no series |
| `bad_key.html` | HTTP 400 for a malformed key |

## Sources

- [ECB Data Portal](https://data.ecb.europa.eu)
- [Euro foreign exchange reference rates](https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/index.en.html)
- [Euro short-term rate (€STR)](https://www.ecb.europa.eu/stats/financial_markets_and_interest_rates/euro_short-term_rate/html/index.en.html)
- [Key ECB interest rates](https://www.ecb.europa.eu/stats/key-ecb-interest-rates/html/index.en.html)
- [Euro area yield curves](https://www.ecb.europa.eu/stats/financial_markets_and_interest_rates/euro_area_yield_curves/html/index.en.html)
- [Disclaimer and copyright](https://www.ecb.europa.eu/services/disclaimer/html/index.en.html)
