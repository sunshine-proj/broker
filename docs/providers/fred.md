# FRED (Federal Reserve Bank of St. Louis)

Adapter: [backend/app/providers/fred.py](../../backend/app/providers/fred.py). Provider name in the seed file: `fred`.

Facts below were checked on **2026-11-16** against FRED's API documentation and terms. Series ids and recent values were confirmed through FRED's public CSV download. **The adapter has not yet been run against the live API**: no API key was available. See [Verification Status](#verification-status).

## Licensing: Read This First

FRED's terms put this integration's purpose in question.

- **Storing data:** the full FRED terms of use (<https://fred.stlouisfed.org/legal/>) list among the prohibitions for FRED Services and FRED Content: "Store, cache, or archive any portion of the FRED® Services or FRED® Content". They state that the FRED Services include the API. This platform stores every observation permanently, which is what that clause prohibits.
- **Third-party data:** series "may be owned by third parties and subject to copyright restrictions". Use other than personal use needs the owner's permission. Copyrighted series say "Copyright" in their notes. The series in the seed file are from US government sources (Federal Reserve Board, BLS, BEA, Treasury), not copyrighted third parties.
- **Required notice:** any application using the API must display "This product uses the FRED® API but is not endorsed or certified by the Federal Reserve Bank of St. Louis."
- **Machine learning:** FRED content may not be used to develop or train software or machine-learning systems.
- **Keys:** each application needs its own key, and each user their own.

The data in the seed file is published by US government agencies, and the same series can be fetched directly from them:

| Data | Original source |
| --- | --- |
| Treasury yields | US Treasury, Federal Reserve Board H.15 release |
| Effective federal funds rate | New York Fed, Phase 8 |
| CPI, unemployment, payrolls | BLS |
| GDP | BEA |
| Industrial production | Federal Reserve Board |

**Decision needed:** keep FRED, which is convenient but whose terms prohibit archiving, or source these series from their publishers. Until it is made, treat FRED collection as for personal evaluation only.

## Supported Data

| Data | Series | Stored as |
| --- | --- | --- |
| US Treasury constant maturity yields | `DGS1MO`, `DGS3MO`, `DGS2`, `DGS5`, `DGS10`, `DGS30` | Instrument quotes: `yield`, `reference`, `end_of_day`, unit `percent` |
| Effective federal funds rate | `DFF` | Instrument quote: `rate`, otherwise as above |
| US economic indicators | `CPIAUCSL`, `UNRATE`, `PAYEMS`, `INDPRO`, `GDP`, `GFDEGDQ188S` | Economic observations |

- A series collected as an instrument quote must be a rate or yield in percent.
- Series named `DGS…` (Treasury constant maturity) are stored as yields, and every other quote series as a rate.
- Any FRED series can be an economic indicator.

## Symbols and Series Codes

FRED series ids, for example `DGS10`. Find them with `python -m app.cli search fred QUERY`, or on <https://fred.stlouisfed.org>.

## Endpoints Used

| Endpoint | Used for | Parameters |
| --- | --- | --- |
| `GET /fred/series/observations` | Current values | `sort_order=desc&limit=5`: the last five observations, so a missed day is filled in |
| `GET /fred/series/observations` | History and indicators | `observation_start` and `observation_end` as dates. Both are inclusive, so the adapter asks for the day before the exclusive end. |
| `GET /fred/series/search` | Finding series ids | `search_text`, `limit=30`, `order_by=popularity` |

- Every request sends `file_type=json` and the key. FRED has no multi-series request, so it takes one request per series.
- **Response format:**
  - `observations` is a list of `{realtime_start, realtime_end, date, value}`.
  - `value` is a string, decoded exactly as a decimal.
  - A date without a value, such as a market holiday, has the value `.` and is skipped.
  - `date` is `YYYY-MM-DD`; monthly and quarterly values are dated by the first day of the period.

## Revisions

FRED returns the values current today. Each observation's `realtime_start` is the date that value was first published in its current form, and it is stored in the observation's metadata.

When a later collection finds a different value for a period already stored, the value is kept as a revision beside the earlier one (PRD section 7). The two can be told apart by `realtime_start`.

The full revision history of a series (ALFRED vintages, `output_type=2`) is not imported. Revisions are recorded from the time collection starts.

## Authentication

A free API key: 32 lower-case letters and digits, sent as the query parameter `api_key`. It is required for every request. Request one at <https://fredaccount.stlouisfed.org/apikeys>.

- The key is read from `FRED_API_KEY`. Without it, collection fails and the provider is recorded as down with that reason.
- The key travels in the URL. Error messages and logs never contain the URL, and httpx's request logging is turned off.
- FRED answers an unregistered key with HTTP 400 (not 401). The adapter recognises its error message and reports a credential error.

## Rate Limits

- **Documented limit:** 120 requests a minute, after which FRED answers HTTP 429. Ignoring the limit can lead to a temporary block.
- **Seed configuration:** 100 a minute.
- **Usage:** collection uses 7 requests for current values and 6 for indicators.

## Errors

| Response | Meaning | Handling |
| --- | --- | --- |
| HTTP 400, `api_key` in `error_message` | Missing or unregistered key | `ProviderAuthError` |
| HTTP 400, other message | For example, a series that does not exist | `ProviderResponseError` |
| HTTP 429 | Rate limit | Retried with backoff |

Error bodies are `{"error_code": 400, "error_message": "..."}`. The message for a non-existent series is not documented; the fixture assumes "The series does not exist."

## Data Refresh Limitations

- Treasury yields and the federal funds rate are daily, published with a lag of one business day. Market holidays have no value.
- Economic indicators are monthly or quarterly, with release lags of weeks, and are revised.

## Historical Data Availability

The full history of each series. Treasury yields go back to 1962, CPI to 1947.

## Verification Status

- **Tests:** the adapter is covered by tests against fixtures under `backend/tests/fixtures/fred/`.
- **Fixtures are not recordings.** They follow the response format in FRED's API documentation, filled with real values taken from FRED's public CSV download on 2026-11-16.
- **Live check pending:** once `FRED_API_KEY` is set, run `python -m app.cli collect quotes fred` and `collect indicators fred`. Then replace the fixtures with recorded responses, with the key removed, and update this section.

## Sources

- [API documentation](https://fred.stlouisfed.org/docs/api/fred/)
- [Series observations](https://fred.stlouisfed.org/docs/api/fred/series_observations.html)
- [Errors and rate limits](https://fred.stlouisfed.org/docs/api/fred/errors.html)
- [Real-time periods](https://fred.stlouisfed.org/docs/api/fred/realtime_period.html)
- [API key](https://fred.stlouisfed.org/docs/api/api_key.html)
- [API terms of use](https://fred.stlouisfed.org/docs/api/terms_of_use.html)
- [FRED legal terms](https://fred.stlouisfed.org/legal/)
