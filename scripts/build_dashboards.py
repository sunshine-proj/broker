#!/usr/bin/env python3
"""Generate the Grafana dashboards in grafana/dashboards/.

The dashboards are built from shared panel definitions, on top of the
reporting views created by migration 0005, so every dashboard reads data the
same way. Edit this script, not the JSON, then regenerate:

    python3 scripts/build_dashboards.py          # write the files
    python3 scripts/build_dashboards.py --check  # fail if they are out of date

Standard library only, so it runs without the backend's environment.
"""

import argparse
import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

OUTPUT = Path(__file__).resolve().parents[1] / "grafana" / "dashboards"

DATASOURCE = {"type": "grafana-postgresql-datasource", "uid": "marketdata"}
TAG = "market-data"
SCHEMA_VERSION = 41

# Shown wherever a provider's data is displayed, as its licence requires.
ATTRIBUTION = {
    "twelve_data": (
        "Stock, ETF, intraday currency and gold data: [Twelve Data](https://twelvedata.com)."
    ),
    "binance": "Exchange prices: [Binance](https://www.binance.com) spot market.",
    "coingecko": "**Powered by [CoinGecko](https://www.coingecko.com)**",
    "ecb": (
        "Source: [European Central Bank](https://data.ecb.europa.eu). ECB reference rates "
        "are published for information purposes only."
    ),
    "fred": (
        "**This product uses the FRED® API but is not endorsed or certified by the Federal "
        "Reserve Bank of St. Louis.** Underlying data: US Treasury, Federal Reserve Board, "
        "BLS, BEA, via [FRED](https://fred.stlouisfed.org)."
    ),
    # The notice the New York Fed's Terms of Use require for reference rates.
    "nyfed": (
        "SOFR, EFFR and OBFR are subject to the [Terms of Use](https://www.newyorkfed.org/"
        "privacy/termsofuse.html) posted at newyorkfed.org. The New York Fed is not responsible "
        "for publication of these rates by this platform, does not sanction or endorse any "
        "particular republication, and has no liability for your use. SOFR is calculated using "
        "data provided under a license granted to the New York Fed by DTCC Solutions LLC."
    ),
    "us_treasury": (
        "Source: [U.S. Department of the Treasury](https://home.treasury.gov), "
        "Daily Treasury Par Yield Curve Rates."
    ),
}

PRICE_LIKE = "('price', 'nav', 'rate', 'yield', 'index_level')"

Panel = dict[str, Any]


# --- Building blocks -------------------------------------------------------------------------


def sql(text: str) -> str:
    """Strip indentation so the stored SQL reads cleanly in Grafana's editor."""
    lines = [line for line in text.strip("\n").split("\n")]
    indent = min(len(line) - len(line.lstrip()) for line in lines if line.strip())
    return "\n".join(line[indent:] for line in lines)


def target(query: str, fmt: str = "table", ref: str = "A") -> dict[str, Any]:
    return {
        "refId": ref,
        "datasource": DATASOURCE,
        "editorMode": "code",
        "format": fmt,
        "rawQuery": True,
        "rawSql": sql(query),
    }


def thresholds(*steps: tuple[str, float | None]) -> dict[str, Any]:
    return {"mode": "absolute", "steps": [{"color": c, "value": v} for c, v in steps]}


def override(field: str, **properties: Any) -> dict[str, Any]:
    return {
        "matcher": {"id": "byName", "options": field},
        "properties": [{"id": key.replace("__", "."), "value": v} for key, v in properties.items()],
    }


def age_override(field: str, warn: int, crit: int) -> dict[str, Any]:
    """Colour an age in seconds green, orange past `warn` and red past `crit`."""
    return override(
        field,
        unit="s",
        decimals=0,
        custom__cellOptions={"type": "color-background", "mode": "basic"},
        thresholds=thresholds(("green", None), ("orange", warn), ("red", crit)),
    )


def panel(
    kind: str,
    title: str,
    queries: list[dict[str, Any]] | None = None,
    *,
    width: int = 24,
    height: int = 8,
    description: str = "",
    unit: str | None = None,
    decimals: int | None = None,
    overrides: list[dict[str, Any]] | None = None,
    options: dict[str, Any] | None = None,
    custom: dict[str, Any] | None = None,
    mappings: list[dict[str, Any]] | None = None,
) -> Panel:
    defaults: dict[str, Any] = {}
    if unit is not None:
        defaults["unit"] = unit
    if decimals is not None:
        defaults["decimals"] = decimals
    if custom:
        defaults["custom"] = custom
    if mappings:
        defaults["mappings"] = mappings
    result: Panel = {
        "type": kind,
        "title": title,
        "description": description,
        "gridPos": {"w": width, "h": height},
        "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
        "options": options or {},
    }
    if queries is not None:
        result["datasource"] = DATASOURCE
        result["targets"] = queries
    return result


def table(title: str, query: str, **kwargs: Any) -> Panel:
    kwargs.setdefault("options", {"showHeader": True, "cellHeight": "sm"})
    return panel("table", title, [target(query)], **kwargs)


def timeseries(
    title: str, query: str, *, step: bool = False, bars: bool = False, **kwargs: Any
) -> Panel:
    custom: dict[str, Any] = {"lineWidth": 1, "fillOpacity": 0, "showPoints": "never"}
    if step:
        custom["lineInterpolation"] = "stepAfter"
    if bars:
        custom.update({"drawStyle": "bars", "fillOpacity": 80, "stacking": {"mode": "normal"}})
    kwargs.setdefault("custom", custom)
    kwargs.setdefault(
        "options",
        {
            "legend": {"displayMode": "table", "placement": "right", "calcs": ["lastNotNull"]},
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
    )
    return panel("timeseries", title, [target(query, "time_series")], **kwargs)


def text(title: str, content: str, height: int = 3) -> Panel:
    return panel(
        "text", title, height=height, options={"mode": "markdown", "content": content.strip()}
    )


def attribution(*providers: str) -> Panel:
    lines = "  \n".join(ATTRIBUTION[p] for p in providers)
    return text("Data sources", lines, height=2 + len(providers))


def freshness(title: str, where: str, warn: int, crit: int, description: str) -> Panel:
    """When each kind of data last changed; the age column is coloured by the thresholds."""
    return table(
        title,
        f"""
        SELECT provider AS "Provider",
               asset_type || ' ' || observation_type AS "Data",
               series AS "Series",
               last_observed_at AS "Latest value at",
               last_ingested_at AS "Collected at",
               age_seconds AS "Age"
          FROM v_data_freshness
         WHERE {where}
         ORDER BY provider, asset_type, observation_type
        """,
        description=description,
        overrides=[age_override("Age", warn, crit)],
        height=6,
    )


def variable(
    name: str, label: str, query: str, *, multi: bool = True, include_all: bool = True
) -> dict[str, Any]:
    return {
        "name": name,
        "label": label,
        "type": "query",
        "datasource": DATASOURCE,
        "query": sql(query),
        "definition": sql(query),
        "refresh": 1,
        "sort": 0,
        "multi": multi,
        "includeAll": include_all and multi,
        "current": {},
        "options": [],
    }


def layout(panels: Iterable[Panel]) -> list[Panel]:
    """Place panels left to right, wrapping at 24 columns, and number them."""
    placed, x, y, row_height = [], 0, 0, 0
    for number, item in enumerate(panels, start=1):
        width, height = item["gridPos"]["w"], item["gridPos"]["h"]
        if x + width > 24:
            x, y, row_height = 0, y + row_height, 0
        item["gridPos"] = {"x": x, "y": y, "w": width, "h": height}
        item["id"] = number
        placed.append(item)
        x += width
        row_height = max(row_height, height)
    return placed


def dashboard(
    uid: str,
    title: str,
    description: str,
    panels: Iterable[Panel],
    *,
    variables: Iterable[dict[str, Any]] = (),
    time_from: str = "now-7d",
    refresh: str = "5m",
) -> dict[str, Any]:
    return {
        "uid": uid,
        "title": title,
        "description": description,
        "tags": [TAG],
        "timezone": "utc",
        "editable": False,
        "graphTooltip": 1,
        "schemaVersion": SCHEMA_VERSION,
        "version": 1,
        "time": {"from": time_from, "to": "now"},
        "refresh": refresh,
        "timepicker": {
            "refresh_intervals": ["1m", "5m", "15m", "1h", "6h", "1d"],
        },
        "links": [
            {
                "type": "dashboards",
                "tags": [TAG],
                "asDropdown": True,
                "title": "Market Data dashboards",
                "keepTime": True,
                "includeVars": False,
            }
        ],
        "annotations": {"list": []},
        "templating": {"list": list(variables)},
        "panels": layout(panels),
    }


# --- Shared panels ----------------------------------------------------------------------------


def latest_table(title: str, where: str, value_label: str = "Value", **kwargs: Any) -> Panel:
    """The newest value of each series, with its provider, kind, freshness and age."""
    return table(
        title,
        f"""
        SELECT symbol AS "Symbol",
               instrument_name AS "Name",
               provider AS "Provider",
               value AS "{value_label}",
               coalesce(currency, unit) AS "Unit",
               quote_kind AS "Kind",
               freshness AS "Freshness",
               observed_at AS "Observed",
               age_seconds AS "Age"
          FROM v_latest_observation
         WHERE {where}
         ORDER BY symbol, provider
        """,
        overrides=[override("Age", unit="s", decimals=0)],
        **kwargs,
    )


def series_chart(title: str, where: str, **kwargs: Any) -> Panel:
    """One line per instrument and provider; providers are never merged."""
    return timeseries(
        title,
        f"""
        SELECT observed_at AS "time",
               symbol || ' · ' || provider AS metric,
               value
          FROM v_observation
         WHERE $__timeFilter(observed_at)
           AND {where}
         ORDER BY 1
        """,
        **kwargs,
    )


def performance_chart(title: str, where: str, **kwargs: Any) -> Panel:
    """Change since the start of the time range, in percent, so instruments compare."""
    return timeseries(
        title,
        f"""
        SELECT observed_at AS "time",
               symbol || ' · ' || provider AS metric,
               (value / first_value(value) OVER (
                    PARTITION BY symbol, provider ORDER BY observed_at) - 1) * 100 AS value
          FROM v_observation
         WHERE $__timeFilter(observed_at)
           AND {where}
         ORDER BY 1
        """,
        unit="percent",
        decimals=2,
        description="Change since the first value in the selected time range.",
        **kwargs,
    )


# --- Dashboards --------------------------------------------------------------------------------


def global_overview() -> dict[str, Any]:
    return dashboard(
        "market-overview",
        "Global Market Overview",
        "Latest values across every asset class, relative performance and data freshness.",
        [
            attribution("twelve_data", "coingecko", "binance", "ecb", "nyfed", "us_treasury", "fred"),
            panel(
                "stat",
                "Key values",
                [
                    target(
                        f"""
                        SELECT observed_at AS "time", symbol || ' · ' || provider AS metric, value
                          FROM v_latest_observation
                         WHERE symbol IN ('BTC/USD', 'EUR/USD', 'XAU/USD', 'ESTR', 'SOFR', 'US10Y', 'SPY')
                           AND observation_type IN {PRICE_LIKE}
                         ORDER BY 1
                        """,
                        "time_series",
                    )
                ],
                height=4,
                description="Latest value of a few benchmarks, from each provider.",
                options={
                    "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "textMode": "value_and_name",
                    "colorMode": "none",
                    "graphMode": "none",
                },
            ),
            performance_chart(
                "Relative performance",
                f"symbol IN (${{instrument:sqlstring}}) AND observation_type IN {PRICE_LIKE}",
                height=10,
            ),
            latest_table(
                "Latest values",
                f"observation_type IN {PRICE_LIKE}",
                height=12,
                description="Values from different providers are listed separately.",
            ),
            freshness(
                "Data freshness",
                "true",
                warn=86400,
                crit=4 * 86400,
                description=(
                    "Age of the newest value per provider and kind of data. Daily and weekend "
                    "data is up to a few days old by design."
                ),
            ),
        ],
        variables=[
            variable(
                "instrument",
                "Instruments",
                f"""
                SELECT DISTINCT symbol FROM v_latest_observation
                 WHERE observation_type IN {PRICE_LIKE} ORDER BY 1
                """,
            )
        ],
    )


def listed_market(uid: str, title: str, asset_type: str) -> dict[str, Any]:
    where = f"asset_type = '{asset_type}'"
    return dashboard(
        uid,
        title,
        f"Prices and daily candles of the tracked {asset_type} instruments.",
        [
            attribution("twelve_data"),
            latest_table(
                "Latest prices",
                f"{where} AND observation_type = 'price' AND symbol IN (${{symbol:sqlstring}})",
                "Price",
                width=14,
                height=9,
            ),
            table(
                "Daily change",
                f"""
                SELECT symbol AS "Symbol",
                       timestamp AS "Day",
                       close AS "Close",
                       (close / previous_close - 1) * 100 AS "Change"
                  FROM (
                        SELECT symbol, provider, timestamp, close,
                               lag(close) OVER w AS previous_close,
                               row_number() OVER (PARTITION BY symbol, provider
                                                  ORDER BY timestamp DESC) AS newest
                          FROM v_candle
                         WHERE {where} AND interval = '1day'
                           AND timestamp > now() - interval '15 days'
                        WINDOW w AS (PARTITION BY symbol, provider ORDER BY timestamp)
                       ) c
                 WHERE newest = 1 AND symbol IN (${{symbol:sqlstring}})
                 ORDER BY "Change" DESC NULLS LAST
                """,
                width=10,
                height=9,
                description="Last daily close against the one before.",
                overrides=[
                    override(
                        "Change",
                        unit="percent",
                        decimals=2,
                        custom__cellOptions={"type": "color-text"},
                        thresholds=thresholds(("red", None), ("green", 0)),
                    )
                ],
            ),
            series_chart(
                "Price",
                f"{where} AND observation_type = 'price' AND symbol IN (${{symbol:sqlstring}})",
                height=9,
                width=12,
            ),
            performance_chart(
                "Relative performance",
                f"{where} AND observation_type = 'price' AND symbol IN (${{symbol:sqlstring}})",
                height=9,
                width=12,
            ),
            panel(
                "candlestick",
                "Daily candles: $candle_symbol",
                [
                    target(
                        f"""
                        SELECT timestamp AS "time", open, high, low, close, volume
                          FROM v_candle
                         WHERE {where} AND interval = '1day'
                           AND symbol = ${{candle_symbol:sqlstring}}
                           AND $__timeFilter(timestamp)
                         ORDER BY 1
                        """
                    )
                ],
                height=10,
                description="Closed daily candles, unadjusted, dated by the trading day.",
                options={"mode": "candles+volume", "candleStyle": "candles", "colorStrategy": "open-close"},
            ),
            freshness(
                "Data freshness",
                where,
                warn=2 * 86400,
                crit=4 * 86400,
                description="Prices only change while the market is open, so weekends age the data.",
            ),
        ],
        variables=[
            variable(
                "symbol",
                "Symbols",
                f"SELECT DISTINCT symbol FROM v_latest_observation WHERE {where} ORDER BY 1",
            ),
            variable(
                "candle_symbol",
                "Candles for",
                f"SELECT DISTINCT symbol FROM v_latest_observation WHERE {where} ORDER BY 1",
                multi=False,
            ),
        ],
        time_from="now-30d",
    )


def crypto() -> dict[str, Any]:
    where = "asset_type = 'crypto' AND symbol IN (${coin:sqlstring})"
    return dashboard(
        "crypto-market",
        "Cryptocurrency Market",
        "Prices, market capitalization, volume, supply and dominance of the tracked coins.",
        [
            attribution("coingecko", "binance"),
            table(
                "Market overview",
                f"""
                SELECT symbol AS "Coin",
                       instrument_name AS "Name",
                       provider AS "Provider",
                       max(value) FILTER (WHERE observation_type = 'price') AS "Price",
                       max(currency) FILTER (WHERE observation_type = 'price') AS "Currency",
                       max(value) FILTER (WHERE observation_type = 'market_cap') AS "Market cap",
                       max(value) FILTER (WHERE observation_type = 'volume') AS "Volume",
                       max(value) FILTER (WHERE observation_type = 'circulating_supply')
                           AS "Circulating supply",
                       max(value) FILTER (WHERE observation_type = 'dominance') AS "Dominance",
                       max(observed_at) FILTER (WHERE observation_type = 'price') AS "Price at"
                  FROM v_latest_observation
                 WHERE {where}
                 GROUP BY symbol, instrument_name, provider
                 ORDER BY "Market cap" DESC NULLS LAST
                """,
                height=10,
                description=(
                    "CoinGecko prices are averages across exchanges, served with a delay of 1 to 5 "
                    "minutes, in US dollars. Binance prices are the last trade on Binance, in Tether "
                    "(USDT), and are separate instruments."
                ),
                overrides=[
                    override("Market cap", unit="short", decimals=2),
                    override("Volume", unit="short", decimals=2),
                    override("Circulating supply", unit="short", decimals=2),
                    override("Dominance", unit="percent", decimals=2),
                ],
            ),
            series_chart(
                "Price",
                f"{where} AND observation_type = 'price'",
                width=12,
                height=9,
                custom={"lineWidth": 1, "showPoints": "never", "scaleDistribution": {"type": "log", "log": 10}},
                description="Logarithmic scale, so coins of very different prices fit one chart.",
            ),
            performance_chart(
                "Relative performance", f"{where} AND observation_type = 'price'", width=12, height=9
            ),
            panel(
                "piechart",
                "Market dominance",
                [
                    target(
                        f"""
                        SELECT symbol AS metric, value, observed_at AS "time"
                          FROM v_latest_observation
                         WHERE {where} AND observation_type = 'dominance'
                         ORDER BY 1
                        """,
                        "time_series",
                    )
                ],
                width=8,
                height=9,
                unit="percent",
                description="Share of total crypto market capitalization. Only the largest coins have a figure.",
                options={
                    "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "pieType": "donut",
                    "legend": {"displayMode": "table", "placement": "right", "values": ["value"]},
                },
            ),
            panel(
                "bargauge",
                "Market capitalization",
                [
                    target(
                        f"""
                        SELECT observed_at AS "time", symbol AS metric, value
                          FROM v_latest_observation
                         WHERE {where} AND observation_type = 'market_cap'
                         ORDER BY 1
                        """,
                        "time_series",
                    )
                ],
                width=16,
                height=9,
                unit="short",
                options={
                    "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "orientation": "horizontal",
                    "displayMode": "basic",
                },
            ),
            series_chart(
                "Trading volume",
                f"{where} AND observation_type = 'volume'",
                unit="short",
                height=8,
            ),
            freshness(
                "Data freshness",
                "asset_type = 'crypto'",
                warn=15 * 60,
                crit=60 * 60,
                description=(
                    "CoinGecko prices are collected every 5 minutes, dominance every 2 hours; "
                    "Binance prices every minute."
                ),
            ),
        ],
        variables=[
            variable(
                "coin",
                "Coins",
                "SELECT DISTINCT symbol FROM v_latest_observation WHERE asset_type = 'crypto' ORDER BY 1",
            )
        ],
    )


def forex() -> dict[str, Any]:
    where = "asset_type = 'forex' AND observation_type = 'rate' AND symbol IN (${pair:sqlstring})"
    return dashboard(
        "forex-monitor",
        "Forex Monitor",
        "Market and reference exchange rates. Reference rates are official daily fixings, "
        "indicative rates are market quotes; both are shown, never merged.",
        [
            attribution("twelve_data", "ecb"),
            latest_table("Latest rates", where, "Rate", height=10, decimals=5),
            series_chart(
                "Rates",
                where,
                height=10,
                decimals=5,
                description="One line per pair and provider. ECB reference rates are daily.",
            ),
            freshness(
                "Data freshness",
                "asset_type = 'forex'",
                warn=3 * 3600,
                crit=3 * 86400,
                description="Market rates are collected every 30 minutes; ECB rates once a day.",
            ),
        ],
        variables=[
            variable(
                "pair",
                "Pairs",
                "SELECT DISTINCT symbol FROM v_latest_observation WHERE asset_type = 'forex' ORDER BY 1",
            )
        ],
        time_from="now-30d",
    )


def bond_yields() -> dict[str, Any]:
    curve = (
        "asset_type = 'government_bond_yield' AND observation_type = 'yield'"
        " AND instrument_metadata->>'curve' = ${curve:sqlstring}"
    )
    where = f"{curve} AND provider = ${{source:sqlstring}}"
    maturity_months = (
        "CASE right(instrument_metadata->>'maturity', 1) "
        "WHEN 'M' THEN left(instrument_metadata->>'maturity', -1)::int "
        "ELSE left(instrument_metadata->>'maturity', -1)::int * 12 END"
    )
    return dashboard(
        "government-bond-yields",
        "Government Bond Yields",
        "Government benchmark yields, in percent: the US Treasury par yield curve and the "
        "ECB's euro area yield curves. Choose a curve and the provider to read it from.",
        [
            attribution("us_treasury", "ecb", "fred"),
            panel(
                "barchart",
                "Yield curve",
                [
                    target(
                        f"""
                        SELECT instrument_metadata->>'maturity' AS "Maturity",
                               value AS "Yield"
                          FROM v_latest_observation
                         WHERE {where}
                         ORDER BY {maturity_months}
                        """
                    )
                ],
                width=10,
                height=9,
                unit="percent",
                decimals=2,
                description="Latest yield by maturity, from the chosen provider.",
                options={"xField": "Maturity", "orientation": "vertical", "showValue": "always"},
            ),
            latest_table("Latest yields", curve, "Yield", width=14, height=9),
            series_chart("Yields", where, unit="percent", decimals=2, height=10),
            freshness(
                "Data freshness",
                "asset_type = 'government_bond_yield'",
                warn=3 * 86400,
                crit=5 * 86400,
                description="Yields are published each business day, after the close.",
            ),
        ],
        variables=[
            variable(
                "curve",
                "Curve",
                """
                SELECT DISTINCT metadata->>'curve' FROM instrument
                 WHERE asset_type = 'government_bond_yield' ORDER BY 1
                """,
                multi=False,
            ),
            variable(
                "source",
                "Provider",
                """
                SELECT DISTINCT provider FROM v_latest_observation
                 WHERE asset_type = 'government_bond_yield'
                   AND instrument_metadata->>'curve' = ${curve:sqlstring}
                 ORDER BY 1 DESC
                """,
                multi=False,
            ),
        ],
        time_from="now-1y",
        refresh="1h",
    )


def interest_rates() -> dict[str, Any]:
    where = "asset_type = 'interest_rate' AND observation_type = 'rate' AND symbol IN (${rate:sqlstring})"
    return dashboard(
        "interest-rates",
        "Interest Rates",
        "Money-market and central bank rates, in percent.",
        [
            attribution("ecb", "nyfed", "fred"),
            latest_table("Latest rates", where, "Rate", height=8, decimals=3),
            series_chart(
                "Rates",
                where,
                step=True,
                unit="percent",
                decimals=3,
                height=11,
                description=(
                    "Central bank rates are recorded on the dates they change and drawn as steps; "
                    "nothing is filled in between."
                ),
            ),
            freshness(
                "Data freshness",
                "asset_type = 'interest_rate'",
                warn=4 * 86400,
                crit=7 * 86400,
                description=(
                    "€STR, SOFR, EFFR and OBFR are published each business day. Policy rates only "
                    "change at central bank decisions, so their age is expected to be large."
                ),
            ),
        ],
        variables=[
            variable(
                "rate",
                "Rates",
                "SELECT DISTINCT symbol FROM v_latest_observation WHERE asset_type = 'interest_rate' ORDER BY 1",
            )
        ],
        time_from="now-2y",
        refresh="1h",
    )


def commodities() -> dict[str, Any]:
    where = "asset_type = 'commodity' AND symbol IN (${commodity:sqlstring})"
    return dashboard(
        "commodities",
        "Commodities",
        "Spot commodity prices and daily candles.",
        [
            attribution("twelve_data"),
            text(
                "Coverage",
                "Spot gold (XAU/USD) is the only commodity on Twelve Data's free plan. Silver, "
                "crude oil and natural gas need a paid plan; free government sources republish "
                "oil and gas prices under third-party licences, so they are not collected.",
                height=4,
            ),
            latest_table(
                "Latest prices",
                f"{where} AND observation_type = 'price'",
                "Price",
                height=5,
                description="Spot prices are indicative over-the-counter quotes, per troy ounce for metals.",
            ),
            series_chart("Price", f"{where} AND observation_type = 'price'", width=12, height=10),
            panel(
                "candlestick",
                "Daily candles: $candle_commodity",
                [
                    target(
                        """
                        SELECT timestamp AS "time", open, high, low, close
                          FROM v_candle
                         WHERE asset_type = 'commodity' AND interval = '1day'
                           AND symbol = ${candle_commodity:sqlstring}
                           AND $__timeFilter(timestamp)
                         ORDER BY 1
                        """
                    )
                ],
                width=12,
                height=10,
                description="Closed daily candles, dated by the day they open (UTC).",
                options={"mode": "candles", "candleStyle": "candles", "colorStrategy": "open-close"},
            ),
            freshness(
                "Data freshness",
                "asset_type = 'commodity'",
                warn=3 * 3600,
                crit=3 * 86400,
                description="Prices are collected every 30 minutes; spot markets close at weekends.",
            ),
        ],
        variables=[
            variable(
                "commodity",
                "Commodities",
                "SELECT DISTINCT symbol FROM v_latest_observation WHERE asset_type = 'commodity' ORDER BY 1",
            ),
            variable(
                "candle_commodity",
                "Candles for",
                "SELECT DISTINCT symbol FROM v_latest_observation WHERE asset_type = 'commodity' ORDER BY 1",
                multi=False,
            ),
        ],
        time_from="now-90d",
    )


def indicators() -> dict[str, Any]:
    return dashboard(
        "economic-indicators",
        "Economic Indicators",
        "Macroeconomic indicators. Each period shows its current value; revisions are kept.",
        [
            attribution("ecb", "fred"),
            table(
                "Latest values",
                """
                SELECT DISTINCT ON (code)
                       code AS "Code",
                       name AS "Indicator",
                       coalesce(country, 'EA') AS "Area",
                       observed_at AS "Period",
                       value AS "Value",
                       unit AS "Unit",
                       revisions AS "Revisions",
                       provider AS "Provider",
                       ingested_at AS "Stored"
                  FROM v_indicator_value
                 ORDER BY code, observed_at DESC
                """,
                height=9,
                description="Periods are dated by their first day: September 2026 is 2026-09-01.",
            ),
            timeseries(
                "$indicator",
                """
                SELECT observed_at AS "time", name AS metric, value
                  FROM v_indicator_value
                 WHERE code = ${indicator:sqlstring}
                   AND $__timeFilter(observed_at)
                 ORDER BY 1
                """,
                bars=True,
                height=10,
                width=16,
                custom={"drawStyle": "bars", "fillOpacity": 80, "lineWidth": 1},
            ),
            table(
                "Revisions of $indicator",
                """
                SELECT e.observed_at AS "Period", e.ingested_at AS "Stored", e.value AS "Value"
                  FROM economic_observation e
                  JOIN economic_indicator i ON i.id = e.indicator_id
                 WHERE i.code = ${indicator:sqlstring}
                   AND (e.indicator_id, e.observed_at) IN (
                        SELECT indicator_id, observed_at FROM economic_observation
                         GROUP BY 1, 2 HAVING count(*) > 1)
                 ORDER BY 1 DESC, 2
                """,
                width=8,
                height=10,
                description="Periods whose published value changed, with every stored value.",
            ),
            table(
                "Data freshness",
                """
                SELECT code AS "Indicator",
                       provider AS "Provider",
                       max(observed_at) AS "Latest period",
                       max(ingested_at) AS "Last stored",
                       EXTRACT(EPOCH FROM now() - max(ingested_at)) AS "Since stored"
                  FROM v_indicator_value
                 GROUP BY code, provider
                 ORDER BY code
                """,
                height=7,
                description="Collection runs daily; new periods appear weeks after their end.",
                overrides=[age_override("Since stored", 2 * 86400, 4 * 86400)],
            ),
        ],
        variables=[
            variable(
                "indicator",
                "Indicator",
                "SELECT name AS __text, code AS __value FROM economic_indicator ORDER BY name",
                multi=False,
            )
        ],
        time_from="now-10y",
        refresh="1h",
    )


def latency_table(**kwargs: Any) -> Panel:
    return table(
        "Collection latency",
        """
        SELECT provider AS "Provider",
               asset_type || ' ' || observation_type AS "Data",
               freshness AS "Freshness",
               observations AS "Values",
               median_seconds AS "Median",
               p95_seconds AS "95th percentile",
               max_seconds AS "Longest"
          FROM v_collection_latency
         ORDER BY provider, asset_type, observation_type
        """,
        description=(
            "Time from a value's own timestamp to its storage, over the last 24 hours. "
            "End-of-day data is published the next business day, so hours are normal there."
        ),
        overrides=[
            override(name, unit="s", decimals=0)
            for name in ("Median", "95th percentile", "Longest")
        ],
        **kwargs,
    )


def data_quality() -> dict[str, Any]:
    kinds = "kind IN (${kind:sqlstring})"
    resolution_colours = [
        {
            "type": "value",
            "options": {
                "open": {"color": "orange", "index": 0},
                "filled": {"color": "green", "index": 1},
                "cleared": {"color": "green", "index": 2},
                "no_data": {"color": "text", "index": 3},
                "recorded": {"color": "text", "index": 4},
                "unrecoverable": {"color": "red", "index": 5},
            },
        }
    ]
    coloured = {"custom__cellOptions": {"type": "color-text"}}
    return dashboard(
        "data-quality",
        "Data Quality",
        "Stale series, gaps and their backfill, conflicting re-collections, price spikes, "
        "provider discrepancies and collection latency. Stored values are never corrected.",
        [
            attribution(
                "twelve_data", "coingecko", "binance", "ecb", "nyfed", "us_treasury", "fred"
            ),
            panel(
                "stat",
                "Open issues",
                [
                    target(
                        """
                        SELECT count(*) FILTER (WHERE kind = 'stale') AS "Stale",
                               count(*) FILTER (WHERE kind = 'gap') AS "Gaps",
                               count(*) FILTER (WHERE kind = 'discrepancy') AS "Discrepancies"
                          FROM v_quality_issue
                         WHERE status = 'open'
                        """
                    )
                ],
                height=4,
                description="Problems that still hold. Gaps stay open until backfill has run.",
                options={
                    "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "textMode": "value_and_name",
                    "colorMode": "value",
                    "graphMode": "none",
                },
                custom=None,
                overrides=[],
            ),
            table(
                "Open issues",
                f"""
                SELECT kind AS "Kind",
                       symbol AS "Symbol",
                       provider AS "Provider",
                       other_provider AS "Compared with",
                       message AS "Problem",
                       detected_at AS "Detected",
                       last_seen_at AS "Last seen"
                  FROM v_quality_issue
                 WHERE status = 'open' AND {kinds}
                 ORDER BY detected_at DESC
                 LIMIT 500
                """,
                height=9,
            ),
            timeseries(
                "Issues found per day",
                f"""
                SELECT time_bucket('1 day', detected_at) AS "time", kind AS metric,
                       count(*) AS value
                  FROM v_quality_issue
                 WHERE $__timeFilter(detected_at) AND {kinds}
                 GROUP BY 1, 2
                 ORDER BY 1
                """,
                bars=True,
                width=12,
                height=8,
            ),
            table(
                "Gaps and backfill",
                """
                SELECT symbol AS "Symbol",
                       provider AS "Provider",
                       coalesce(interval, observation_type) AS "Data",
                       range_start AS "Missing from",
                       range_end AS "Missing to",
                       coalesce(resolution, status) AS "Outcome",
                       details->>'values_found' AS "Values filled",
                       details->>'reason' AS "Reason"
                  FROM v_quality_issue
                 WHERE kind = 'gap' AND $__timeFilter(last_seen_at)
                 ORDER BY range_start DESC
                 LIMIT 500
                """,
                width=12,
                height=8,
                description=(
                    "Backfill re-fetches gaps once a day. no_data: the provider had nothing "
                    "(a holiday, for example). unrecoverable: it cannot re-fetch that data."
                ),
                mappings=resolution_colours,
                overrides=[override("Outcome", **coloured)],
            ),
            table(
                "Conflicts and spikes",
                f"""
                SELECT detected_at AS "When",
                       kind AS "Kind",
                       symbol AS "Symbol",
                       provider AS "Provider",
                       coalesce(interval, observation_type) AS "Data",
                       range_end AS "Value at",
                       message AS "What happened",
                       details::text AS "Values"
                  FROM v_quality_issue
                 WHERE kind IN ('conflict', 'spike') AND {kinds}
                   AND $__timeFilter(detected_at)
                 ORDER BY detected_at DESC
                 LIMIT 500
                """,
                height=8,
                description=(
                    "Conflict: the provider re-sent a stored value differently; the stored value "
                    "was kept and marked suspect. Spike: an implausible move, marked suspect."
                ),
            ),
            table(
                "Provider comparison",
                """
                SELECT symbol AS "Symbol",
                       observation_type AS "Data",
                       provider AS "Provider",
                       value AS "Value",
                       other_provider AS "Other provider",
                       other_value AS "Other value",
                       difference AS "Difference",
                       difference_percent AS "Difference %",
                       observed_at AS "Observed",
                       other_observed_at AS "Other observed"
                  FROM v_provider_discrepancy
                 ORDER BY abs(difference_percent) DESC NULLS LAST
                """,
                height=8,
                description=(
                    "Latest values of instruments collected from two providers, observed within "
                    "a day of each other. Reference and market rates differ a little by nature."
                ),
                overrides=[
                    override(
                        "Difference %",
                        unit="percent",
                        decimals=3,
                        custom__cellOptions={"type": "color-text"},
                        thresholds=thresholds(("green", None), ("orange", 1), ("red", 2)),
                    )
                ],
            ),
            table(
                "Suspect values",
                """
                SELECT provider AS "Provider",
                       symbol AS "Symbol",
                       observation_type AS "Data",
                       count(*) AS "Suspect values",
                       max(observed_at) AS "Latest"
                  FROM v_observation
                 WHERE quality_status = 'suspect' AND $__timeFilter(observed_at)
                 GROUP BY provider, symbol, observation_type
                 ORDER BY 4 DESC
                """,
                width=10,
                height=7,
            ),
            latency_table(width=14, height=7),
            freshness(
                "Data freshness",
                "true",
                warn=86400,
                crit=4 * 86400,
                description="Age of the newest value per provider and kind of data.",
            ),
        ],
        variables=[
            variable(
                "kind",
                "Issue kinds",
                """
                SELECT unnest(ARRAY['stale', 'gap', 'conflict', 'spike', 'discrepancy'])
                """,
            )
        ],
        time_from="now-7d",
    )


def collector_monitoring() -> dict[str, Any]:
    status_colours = [
        {
            "type": "value",
            "options": {
                "healthy": {"color": "green", "index": 0},
                "success": {"color": "green", "index": 1},
                "degraded": {"color": "orange", "index": 2},
                "running": {"color": "blue", "index": 3},
                "down": {"color": "red", "index": 4},
                "failed": {"color": "red", "index": 5},
                "unknown": {"color": "text", "index": 6},
            },
        }
    ]
    coloured = {"custom__cellOptions": {"type": "color-text"}}
    return dashboard(
        "collector-monitoring",
        "Collector Monitoring",
        "Provider health, collection jobs, their runs and failures, and data freshness.",
        [
            table(
                "Provider health",
                """
                SELECT provider AS "Provider", enabled AS "Enabled", health AS "Health",
                       consecutive_failures AS "Failures in a row",
                       last_success_at AS "Last success", last_error_at AS "Last error at",
                       last_error AS "Last error"
                  FROM v_provider_status
                 ORDER BY provider
                """,
                height=6,
                mappings=status_colours,
                overrides=[override("Health", **coloured)],
            ),
            table(
                "Collection jobs",
                """
                SELECT job AS "Job", provider AS "Provider", kind AS "Kind",
                       enabled AS "Enabled", schedule AS "Schedule",
                       last_status AS "Last status", last_started_at AS "Last run",
                       next_run_at AS "Next run", last_error AS "Last error"
                  FROM v_job_status
                 ORDER BY job
                """,
                height=9,
                mappings=status_colours,
                overrides=[override("Last status", **coloured)],
            ),
            timeseries(
                "Runs per hour",
                """
                SELECT time_bucket('1 hour', started_at) AS "time", status AS metric,
                       count(*) AS value
                  FROM v_execution
                 WHERE $__timeFilter(started_at)
                 GROUP BY 1, 2
                 ORDER BY 1
                """,
                bars=True,
                width=12,
                height=8,
                overrides=[
                    override("success", color={"mode": "fixed", "fixedColor": "green"}),
                    override("failed", color={"mode": "fixed", "fixedColor": "red"}),
                ],
            ),
            timeseries(
                "Records written per hour",
                """
                SELECT time_bucket('1 hour', started_at) AS "time", job AS metric,
                       sum(records_written) AS value
                  FROM v_execution
                 WHERE $__timeFilter(started_at)
                 GROUP BY 1, 2
                 ORDER BY 1
                """,
                bars=True,
                width=12,
                height=8,
            ),
            timeseries(
                "Run duration",
                """
                SELECT started_at AS "time", job AS metric, duration_seconds AS value
                  FROM v_execution
                 WHERE $__timeFilter(started_at) AND finished_at IS NOT NULL
                 ORDER BY 1
                """,
                width=12,
                height=8,
                unit="s",
                custom={"lineWidth": 1, "fillOpacity": 0, "showPoints": "always", "pointSize": 3,
                        "drawStyle": "points"},
            ),
            latency_table(width=12, height=8),
            table(
                "Failed runs",
                """
                SELECT started_at AS "Started", job AS "Job", trigger AS "Trigger",
                       duration_seconds AS "Duration", error AS "Error"
                  FROM v_execution
                 WHERE status = 'failed' AND $__timeFilter(started_at)
                 ORDER BY started_at DESC
                 LIMIT 100
                """,
                height=8,
                overrides=[override("Duration", unit="s", decimals=0)],
            ),
            freshness(
                "Data freshness",
                "true",
                warn=86400,
                crit=4 * 86400,
                description="Age of the newest value per provider and kind of data.",
            ),
        ],
        time_from="now-24h",
        refresh="1m",
    )


def all_dashboards() -> list[dict[str, Any]]:
    return [
        global_overview(),
        listed_market("stock-market", "Stock Market", "stock"),
        listed_market("etf-monitor", "ETF Monitor", "etf"),
        crypto(),
        forex(),
        bond_yields(),
        interest_rates(),
        commodities(),
        data_quality(),
        indicators(),
        collector_monitoring(),
    ]


def render(board: dict[str, Any]) -> str:
    return json.dumps(board, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="fail if the files are out of date")
    args = parser.parse_args()

    expected = {f"{board['uid']}.json": render(board) for board in all_dashboards()}
    if args.check:
        actual = {path.name: path.read_text() for path in OUTPUT.glob("*.json")}
        if actual != expected:
            stale = sorted(set(actual) ^ set(expected) | {
                name for name in expected if actual.get(name) != expected[name]
            })
            print(f"Dashboards out of date: {', '.join(stale)}. Run scripts/build_dashboards.py.")
            return 1
        print(f"{len(expected)} dashboards up to date.")
        return 0

    OUTPUT.mkdir(parents=True, exist_ok=True)
    for path in OUTPUT.glob("*.json"):
        if path.name not in expected:
            path.unlink()
    for name, content in expected.items():
        (OUTPUT / name).write_text(content)
    print(f"Wrote {len(expected)} dashboards to {OUTPUT}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
