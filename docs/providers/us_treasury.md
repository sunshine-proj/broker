# US Department of the Treasury

Adapter: [backend/app/providers/us_treasury.py](../../backend/app/providers/us_treasury.py). Provider name in the seed file: `us_treasury`.

Facts below were checked on **2026-11-17** against live responses from the Treasury's interest-rate data feed. Re-check when the adapter misbehaves.

## Supported Data

Daily Treasury Par Yield Curve Rates: the Treasury's par yields for constant maturities from 1 month to 30 years, stored as instrument quotes: `yield`, `reference`, `end_of_day`, unit `percent`.

They are the Treasury's own publication of the yields FRED republishes as `DGS1MO` to `DGS30`. Where both are configured, both are stored on the same instrument under their own provider.

## Symbols

Symbols are the field names of the XML feed, one per maturity:

| Field | Maturity | Field | Maturity |
| --- | --- | --- | --- |
| `BC_1MONTH` | 1 month | `BC_2YEAR` | 2 years |
| `BC_1_5MONTH` | 6 weeks | `BC_3YEAR` | 3 years |
| `BC_2MONTH` | 2 months | `BC_5YEAR` | 5 years |
| `BC_3MONTH` | 3 months | `BC_7YEAR` | 7 years |
| `BC_4MONTH` | 4 months | `BC_10YEAR` | 10 years |
| `BC_6MONTH` | 6 months | `BC_20YEAR` | 20 years |
| `BC_1YEAR` | 1 year | `BC_30YEAR` | 30 years |

`BC_30YEARDISPLAY` repeats the 30-year value and is not used. Some maturities were introduced later (4 months in 2022, 6 weeks in 2025). For earlier dates the field is missing, and nothing is stored.

## Endpoint

`GET https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml?data=daily_treasury_yield_curve` with either:

| Parameter | Used for |
| --- | --- |
| `field_tdr_date_value_month=YYYYMM` | Current values: this month and last month, so the last five business days are always covered. 2 requests. |
| `field_tdr_date_value=YYYY` | History: one request per calendar year, for each maturity asked for. |

- The response is an Atom feed of OData entries. Each `m:properties` element has `d:NEW_DATE` (the business day, stored as midnight UTC) and one `d:BC_*` element per maturity, as text.
- Values are parsed from the text as exact decimals. An element with `m:null="true"` or no text is skipped.
- A month with no data returns a feed with no entries.
- A year's feed takes about 20 seconds to arrive, so the seed sets a 60-second timeout.
- The XML is parsed with Python's standard library. It does not resolve external entities, and Python 3.12's expat refuses entity-expansion attacks.

## Authentication

None.

## Rate Limits

None published. The seed sets a courtesy limit of 10 requests a minute. Collection needs 2 requests a day.

## Data Refresh Limitations

- One curve per business day, based on closing market bid prices at about 15:30 New York time, posted later that afternoon. The job runs at 23:30 UTC.
- No values on US federal holidays or market closures.
- The Treasury may correct a published value. Stored observations are immutable, so a later correction is logged as a conflict and not stored.

## Licensing

The data are works of the US government and in the public domain. The dashboards name the Treasury as the source.

## Historical Data Availability

From 1990, by calendar year. A 400-day backfill of 11 maturities made 22 requests and took about 7 minutes, because each maturity downloads the year's feed again.

## Test Fixtures

`backend/tests/fixtures/us_treasury/` holds unmodified feeds recorded on 2026-11-17:

| Fixture | Response |
| --- | --- |
| `yield_curve_202611.xml` | November 2026, 10 business days |
| `yield_curve_202110.xml` | October 2021, before the 4-month bill existed |
| `yield_curve_empty.xml` | A month with no data |

## Sources

- [Daily Treasury Par Yield Curve Rates](https://home.treasury.gov/resource-center/data-chart-center/interest-rates/TextView?type=daily_treasury_yield_curve)
- [Interest rate data feeds](https://home.treasury.gov/treasury-daily-interest-rate-xml-feed)
