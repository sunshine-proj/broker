"""Twelve Data adapter: stocks, ETFs and forex.

API facts this adapter relies on are recorded in docs/providers/twelve_data.md.
"""

import logging
from collections.abc import Callable, Collection, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import batched
from typing import Any

from pydantic import BaseModel, field_validator

from app.models.enums import CandleInterval, Freshness, ObservationType, QuoteKind
from app.providers.base import Capability, Provider
from app.providers.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitedError,
    ProviderResponseError,
)
from app.providers.factory import register_adapter
from app.providers.http import ProviderHttpClient
from app.providers.parsing import (
    assume_utc,
    parse_decimal,
    parse_epoch_seconds,
    parse_utc_date,
)
from app.schemas.normalized import Candle, Quote, SymbolMatch

logger = logging.getLogger(__name__)

# The most bars /time_series returns in one response.
_PAGE_SIZE = 5000
# Upper bound on pages fetched for one request, as a guard against endless paging.
_MAX_PAGES = 20

# How long after its open time a candle is closed. Week and month use the
# longest possible length, so a bar is never taken as closed too early.
_INTERVAL_LENGTH = {
    CandleInterval.MIN_1: timedelta(minutes=1),
    CandleInterval.MIN_5: timedelta(minutes=5),
    CandleInterval.MIN_15: timedelta(minutes=15),
    CandleInterval.MIN_30: timedelta(minutes=30),
    CandleInterval.HOUR_1: timedelta(hours=1),
    CandleInterval.HOUR_4: timedelta(hours=4),
    CandleInterval.DAY_1: timedelta(days=1),
    CandleInterval.WEEK_1: timedelta(days=7),
    CandleInterval.MONTH_1: timedelta(days=31),
}
_DAILY_OR_LONGER = {CandleInterval.DAY_1, CandleInterval.WEEK_1, CandleInterval.MONTH_1}


class _Quote(BaseModel):
    symbol: str
    # The latest traded price; equals the closing price once the session has ended.
    close: Decimal
    # Time of the last one-minute candle: when the price was observed.
    last_quote_at: datetime
    # Present for stocks and ETFs, absent for forex pairs.
    currency: str | None = None

    _close = field_validator("close", mode="before")(parse_decimal)
    _last_quote_at = field_validator("last_quote_at", mode="before")(parse_epoch_seconds)


class _Bar(BaseModel):
    datetime: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None = None

    _prices = field_validator("open", "high", "low", "close", mode="before")(parse_decimal)

    @field_validator("volume", mode="before")
    @classmethod
    def _volume(cls, value: object) -> Decimal | None:
        return None if value is None else parse_decimal(value)


class _TimeSeriesMeta(BaseModel):
    # Present for stocks and ETFs. Forex gives currency names, not codes.
    currency: str | None = None


class _TimeSeries(BaseModel):
    meta: _TimeSeriesMeta
    values: list[_Bar]


class _SearchResult(BaseModel):
    symbol: str
    instrument_name: str
    instrument_type: str | None = None
    mic_code: str | None = None
    country: str | None = None
    currency: str | None = None


class _SearchResponse(BaseModel):
    data: list[_SearchResult]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _quote_currency(symbol: str) -> str | None:
    """The quote currency of a pair written BASE/QUOTE, or None for other symbols."""
    base, separator, quote = symbol.partition("/")
    return quote if separator and base and quote else None


# ISO 4217 codes of the precious metals: gold, silver, platinum, palladium.
_METALS = frozenset({"XAU", "XAG", "XPT", "XPD"})


def _is_metal(symbol: str) -> bool:
    return symbol.partition("/")[0] in _METALS


def _is_error(item: object) -> bool:
    return isinstance(item, dict) and item.get("status") == "error"


@register_adapter
class TwelveDataProvider(Provider):
    name = "twelve_data"
    capabilities = frozenset({Capability.QUOTES, Capability.CANDLES, Capability.SYMBOL_SEARCH})

    def __init__(
        self, client: ProviderHttpClient, *, clock: Callable[[], datetime] = _utc_now
    ) -> None:
        super().__init__(client)
        self._clock = clock

    async def fetch_quotes(
        self,
        symbols: Sequence[str],
        observation_types: Collection[ObservationType] | None = None,
    ) -> list[Quote]:
        """Latest price of each symbol, one credit per symbol.

        A symbol the provider reports an error for is logged and left out; the
        other symbols of the batch are still returned.
        """
        batch_size = self._client.settings.max_batch_size or len(symbols) or 1
        quotes: list[Quote] = []
        for batch in batched(symbols, batch_size):
            raw = await self._client.get_json(
                "/quote", {"symbol": ",".join(batch)}, cost=len(batch)
            )
            self._raise_for_error_body(raw)
            # One symbol is answered with the quote itself, several with an
            # object keyed by symbol.
            items = {batch[0]: raw} if len(batch) == 1 else raw
            if not isinstance(items, dict):
                raise ProviderResponseError(self.name, "quote response is not an object")
            for symbol in batch:
                quote = self._to_quote(symbol, items.get(symbol))
                if quote is not None and (
                    observation_types is None or quote.observation_type in observation_types
                ):
                    quotes.append(quote)
        return quotes

    def _to_quote(self, symbol: str, item: object) -> Quote | None:
        if item is None:
            logger.warning("No quote returned", extra={"provider": self.name, "symbol": symbol})
            return None
        if isinstance(item, dict) and item.get("status") == "error":
            logger.warning(
                "Provider reported an error for a symbol",
                extra={"provider": self.name, "symbol": symbol, "code": item.get("code")},
            )
            return None
        parsed = self._validate(_Quote, item)

        if parsed.currency is not None:
            observation_type, currency, kind = (
                ObservationType.PRICE,
                parsed.currency,
                QuoteKind.TRADED,
            )
        elif (pair_currency := _quote_currency(symbol)) is not None and _is_metal(symbol):
            # Spot precious metals are priced like currency pairs (XAU/USD), but
            # the value is the price of the metal: indicative, not executable.
            observation_type, currency, kind = (
                ObservationType.PRICE,
                pair_currency,
                QuoteKind.INDICATIVE,
            )
        elif pair_currency is not None:
            # A forex rate is an aggregated quote, not an executable price.
            observation_type, currency, kind = (
                ObservationType.RATE,
                pair_currency,
                QuoteKind.INDICATIVE,
            )
        else:
            logger.warning(
                "Quote has no currency and is not a currency pair",
                extra={"provider": self.name, "symbol": symbol},
            )
            return None

        return Quote(
            provider_symbol=symbol,
            observation_type=observation_type,
            observed_at=parsed.last_quote_at,
            value=parsed.close,
            currency=currency,
            unit="currency",
            freshness=Freshness.REAL_TIME,
            quote_kind=kind,
        )

    async def fetch_candles(
        self, symbol: str, interval: CandleInterval, start: datetime, end: datetime
    ) -> list[Candle]:
        """Closed, unadjusted candles with start <= open time < end, oldest first."""
        now = self._clock()
        length = _INTERVAL_LENGTH[interval]
        candles: list[Candle] = []
        page_start = start

        for _ in range(_MAX_PAGES):
            page = await self._fetch_candle_page(symbol, interval, page_start, end)
            if page is None:
                break
            currency = page.meta.currency or _quote_currency(symbol)
            last_open = None
            for bar in page.values:
                opened = (
                    parse_utc_date(bar.datetime)
                    if interval in _DAILY_OR_LONGER
                    else assume_utc(bar.datetime)
                )
                last_open = opened
                # The provider includes the bar that is still forming; skip it.
                if not (start <= opened < end) or opened + length > now:
                    continue
                candles.append(
                    Candle(
                        provider_symbol=symbol,
                        interval=interval,
                        timestamp=opened,
                        open=bar.open,
                        high=bar.high,
                        low=bar.low,
                        close=bar.close,
                        volume=bar.volume,
                        currency=currency,
                    )
                )
            if len(page.values) < _PAGE_SIZE or last_open is None:
                break
            page_start = last_open + timedelta(seconds=1)

        # Paging can return the boundary bar twice.
        unique = {candle.timestamp: candle for candle in candles}
        return [unique[timestamp] for timestamp in sorted(unique)]

    async def _fetch_candle_page(
        self, symbol: str, interval: CandleInterval, start: datetime, end: datetime
    ) -> _TimeSeries | None:
        params = {
            "symbol": symbol,
            "interval": interval.value,
            "start_date": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
            "end_date": end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
            "timezone": "UTC",
            "order": "asc",
            "outputsize": _PAGE_SIZE,
            # Stored candles are immutable, so they must not change when a
            # later split or dividend rewrites adjusted history.
            "adjust": "none",
        }
        try:
            raw = await self._client.get_json("/time_series", params)
        except ProviderResponseError as exc:
            if _is_no_data(exc):
                return None
            raise
        self._raise_for_error_body(raw)
        return self._validate(_TimeSeries, raw)

    async def search_symbols(self, query: str) -> list[SymbolMatch]:
        raw = await self._client.get_json("/symbol_search", {"symbol": query, "outputsize": 30})
        self._raise_for_error_body(raw)
        parsed = self._validate(_SearchResponse, raw)
        return [
            SymbolMatch(
                provider_symbol=item.symbol,
                name=item.instrument_name,
                instrument_type=item.instrument_type,
                exchange=item.mic_code,
                country=item.country,
                currency=item.currency or None,
            )
            for item in parsed.data
        ]

    def _raise_for_error_body(self, raw: Any) -> None:
        """Twelve Data can report an error in the body of an HTTP 200 response."""
        if not _is_error(raw) or "code" not in raw:
            return
        code = raw["code"]
        error: ProviderError
        if code == 429:
            error = ProviderRateLimitedError(self.name, "provider rate limit reached (code 429)")
        elif code in (401, 403):
            error = ProviderAuthError(self.name, f"credential rejected (code {code})")
        else:
            error = ProviderResponseError(
                self.name, f"provider reported an error (code {code})", status_code=None, body=raw
            )
        raise error


def _is_no_data(error: ProviderResponseError) -> bool:
    """Twelve Data answers HTTP 400 when a valid request simply matches no bars."""
    body = error.body
    return (
        error.status_code == 400
        and isinstance(body, dict)
        and str(body.get("message", "")).startswith("No data is available")
    )
