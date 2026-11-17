# Federal Reserve Bank of New York

Adapter: [backend/app/providers/nyfed.py](../../backend/app/providers/nyfed.py). Provider name in the seed file: `nyfed`.

Facts below were checked on **2026-11-17** against the New York Fed's Terms of Use (last updated 2023-06-09), its reference-rate pages, its "additional information about reference rates" page (last updated 2026-10-05), and live responses from the Markets Data API. The API's own documentation page did not render when checked, so the endpoint behaviour here comes from live responses. Re-check when the adapter misbehaves.

## Supported Data

| Rate | Symbol | API group | Stored as |
| --- | --- | --- | --- |
| Secured overnight financing rate | `SOFR` | `secured` | Instrument quotes: `rate`, `reference`, `end_of_day`, unit `percent` |
| Effective federal funds rate | `EFFR` | `unsecured` | as above |
| Overnight bank funding rate | `OBFR` | `unsecured` | as above |
| Tri-party general collateral rate | `TGCR` | `secured` | supported, not in the seed |
| Broad general collateral rate | `BGCR` | `secured` | supported, not in the seed |

The New York Fed administers these rates. It is the primary source; FRED republishes EFFR as `DFF`. Both are stored on the `EFFR` instrument under their own provider.

## Endpoints Used

Base URL: `https://markets.newyorkfed.org/api`.

| Endpoint | Used for |
| --- | --- |
| `GET /rates/{group}/{rate}/last/5.json` | Current values: the last five business days, so a missed day is filled in |
| `GET /rates/{group}/{rate}/search.json?startDate=&endDate=` | History; both dates are inclusive |

- The response is `{"refRates": [...]}`, newest first. Each entry has `effectiveDate`, `type`, `percentRate`, percentiles, `volumeInBillions`, and for EFFR `targetRateFrom` and `targetRateTo`.
- The value stored is `percentRate`, dated at midnight UTC of `effectiveDate`, the business day the rate applies to.
- The percentiles, volume and target range are kept in the observation's metadata, as exact strings.
- A range with no business days returns `{"refRates": []}`. An unknown rate returns HTTP 400.

## Authentication

None.

## Rate Limits

None published. The seed sets a courtesy limit of 30 requests a minute. Collection needs one request per rate per day.

## Publication and Revisions

- SOFR is published at about 08:00 New York time, and EFFR and OBFR at about 09:00, each for the previous business day.
- A published rate may be revised the same day at about 14:30 New York time, if the change exceeds one basis point, with a footnote. Summary statistics (percentiles, volume) are updated later, after each quarter.
- Stored observations are immutable, so the job runs at 20:30 UTC, after the revision window. A revision after that would be kept out and logged as a conflict. The API's `revisionIndicator` is kept in the metadata when set.
- No rate is published on days SIFMA recommends a full market close.

## Licensing

- The Terms of Use grant "a non-exclusive license ... to use, copy, and distribute Content for your personal or business purposes", including storing it, provided copyright notices and source identifiers are kept and no more restrictive terms are imposed.
- Using or distributing reference-rate data requires this notice: "The [rate] is subject to the Terms of Use posted at newyorkfed.org. The New York Fed is not responsible for publication of the [rate] by [publisher], does not sanction or endorse any particular republication, and has no liability for your use."
- SOFR and BGCR are "calculated using data provided under a license granted to the New York Fed by DTCC Solutions LLC".
- The Interest Rates and Global Market Overview dashboards show this notice, naming this platform as the publisher.
- The New York Fed's name may not be used in advertising or as an endorsement.

## Historical Data Availability

SOFR from April 2018; EFFR from 2000 in its current form; OBFR from March 2016.

## Test Fixtures

`backend/tests/fixtures/nyfed/` holds unmodified responses recorded from the live API on 2026-11-17:

| Fixture | Response |
| --- | --- |
| `sofr_last.json` | Last 3 SOFR values |
| `effr_last.json` | Last 3 EFFR values, with the target range |
| `sofr_search.json` | SOFR from 2026-11-02 to 2026-11-06 |

## Sources

- [Terms of Use](https://www.newyorkfed.org/privacy/termsofuse.html)
- [SOFR](https://www.newyorkfed.org/markets/reference-rates/sofr), [EFFR](https://www.newyorkfed.org/markets/reference-rates/effr), [OBFR](https://www.newyorkfed.org/markets/reference-rates/obfr)
- [Additional information about reference rates](https://www.newyorkfed.org/markets/reference-rates/additional-information-about-reference-rates)
