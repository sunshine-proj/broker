"""Binance adapter: exchange-specific cryptocurrency prices from Binance's spot market.

Provider symbols are pairs written BASE/QUOTE in Binance's asset codes, for
example `BTC/USDT`; the adapter sends them to Binance as `BTCUSDT`. Writing the
pair out keeps the quote asset known, which Binance's responses do not state.

Only public market-data endpoints are used; no key is needed. API facts this
adapter relies on are recorded in docs/providers/binance.md.
"""

import json
import logging
import math
from collections.abc import Callable, Collection, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import batched
from typing import Any

from pydantic import BaseModel, field_validator

from app.models.enums import CandleInterval, Freshness, JobKind, ObservationType, QuoteKind
from app.providers.base import Capability, Provider
from app.providers.errors import (
    ProviderAuthError,
    ProviderConfigurationError,
    ProviderError,
    ProviderResponseError,
)
from app.providers.factory import register_adapter
from app.providers.http import ProviderHttpClient
from app.providers.parsing import parse_decimal, parse_epoch_milliseconds
from app.schemas.normalized import Candle, Quote, SymbolMatch

logger = logging.getLogger(__name__)

# /ticker/24hr costs 2 request weight for 1 to 20 symbols, more above that, so
# symbols are sent 20 at a time.
_TICKER_BATCH = 20
_TICKER_WEIGHT = 2
_KLINES_WEIGHT = 2
_EXCHANGE_INFO_WEIGHT = 20
# Most candles /klines returns per request.
_KLINES_LIMIT = 1000
_MAX_PAGES = 50

# Binance's error code for a symbol it does not list.
_INVALID_SYMBOL = -1121

_INTERVALS = {
    CandleInterval.MIN_1: "1m",
    CandleInterval.MIN_5: "5m",
    CandleInterval.MIN_15: "15m",
    CandleInterval.MIN_30: "30m",
    CandleInterval.HOUR_1: "1h",
    CandleInterval.HOUR_4: "4h",
    CandleInterval.DAY_1: "1d",
    CandleInterval.WEEK_1: "1w",
    CandleInterval.MONTH_1: "1M",
}

_QUOTE_TYPES = frozenset({ObservationType.PRICE, ObservationType.VOLUME})


class _Ticker(BaseModel):
    symbol: str
    lastPrice: Decimal  # noqa: N815 (the API's field names)
    quoteVolume: Decimal  # noqa: N815
    # End of the rolling 24-hour window: when the statistics were taken.
    closeTime: datetime  # noqa: N815

    _decimals = field_validator("lastPrice", "quoteVolume", mode="before")(parse_decimal)
    _time = field_validator("closeTime", mode="before")(parse_epoch_milliseconds)


class _Kline(BaseModel):
    """One /klines row: [open time, open, high, low, close, volume, close time, ...]."""

    opened: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    # In the base asset.
    volume: Decimal
    closed: datetime

    @classmethod
    def from_row(cls, row: object) -> "_Kline":
        if not isinstance(row, list) or len(row) < 7:
            raise ValueError("kline row is not a list of at least 7 fields")
        return cls(
            opened=parse_epoch_milliseconds(row[0]),
            open=parse_decimal(row[1]),
            high=parse_decimal(row[2]),
            low=parse_decimal(row[3]),
            close=parse_decimal(row[4]),
            volume=parse_decimal(row[5]),
            closed=parse_epoch_milliseconds(row[6]),
        )


class _SymbolInfo(BaseModel):
    symbol: str
    status: str
    baseAsset: str  # noqa: N815
    quoteAsset: str  # noqa: N815


class _ExchangeInfo(BaseModel):
    symbols: list[_SymbolInfo]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _milliseconds(at: datetime) -> int:
    return int(at.timestamp() * 1000)


@register_adapter
class BinanceProvider(Provider):
    name = "binance"
    capabilities = frozenset({Capability.QUOTES, Capability.CANDLES, Capability.SYMBOL_SEARCH})

    def __init__(
        self, client: ProviderHttpClient, *, clock: Callable[[], datetime] = _utc_now
    ) -> None:
        super().__init__(client)
        self._clock = clock

    @classmethod
    def planned_credits(
        cls,
        kind: JobKind,
        symbols: Sequence[str],
        observation_types: Collection[ObservationType] | None = None,
    ) -> int:
        """Request weight per run (Binance limits weight, not requests)."""
        if kind is JobKind.QUOTES:
            return _TICKER_WEIGHT * math.ceil(len(symbols) / _TICKER_BATCH)
        return _KLINES_WEIGHT * len(symbols)

    async def fetch_quotes(
        self,
        symbols: Sequence[str],
        observation_types: Collection[ObservationType] | None = None,
    ) -> list[Quote]:
        """Last trade price and 24-hour volume of each pair, 20 pairs per request.

        The volume is the rolling 24-hour turnover in the quote asset.
        """
        wanted = (
            _QUOTE_TYPES if observation_types is None else _QUOTE_TYPES & set(observation_types)
        )
        if not wanted or not symbols:
            return []
        pairs = {self._pair(symbol): symbol for symbol in symbols}
        quotes: list[Quote] = []
        for batch in batched(pairs, _TICKER_BATCH):
            raw = await self._request(
                "/ticker/24hr",
                # Binance expects a JSON array without spaces.
                {"symbols": json.dumps(list(batch), separators=(",", ":"))},
                _TICKER_WEIGHT,
            )
            if not isinstance(raw, list):
                raise ProviderResponseError(self.name, "ticker response is not a list")
            for item in raw:
                ticker = self._validate(_Ticker, item)
                symbol = pairs.get(ticker.symbol)
                if symbol is None:
                    continue
                quotes += self._to_quotes(symbol, ticker, wanted)
        return quotes

    def _to_quotes(
        self, symbol: str, ticker: _Ticker, wanted: Collection[ObservationType]
    ) -> list[Quote]:
        currency = symbol.partition("/")[2]
        values = {
            ObservationType.PRICE: (ticker.lastPrice, None),
            ObservationType.VOLUME: (ticker.quoteVolume, {"window": "24h", "field": "quoteVolume"}),
        }
        return [
            Quote(
                provider_symbol=symbol,
                observation_type=observation_type,
                observed_at=ticker.closeTime,
                value=value,
                currency=currency,
                unit="currency",
                freshness=Freshness.REAL_TIME,
                # The last trade on this exchange: an executed price.
                quote_kind=QuoteKind.TRADED,
                metadata=metadata,
            )
            for observation_type, (value, metadata) in values.items()
            if observation_type in wanted
        ]

    async def fetch_candles(
        self, symbol: str, interval: CandleInterval, start: datetime, end: datetime
    ) -> list[Candle]:
        """Closed candles with start <= open time < end, oldest first.

        Volume is in the base asset; prices are in the quote asset.
        """
        pair = self._pair(symbol)
        currency = symbol.partition("/")[2]
        now = self._clock()
        candles: list[Candle] = []
        page_start = start
        for _ in range(_MAX_PAGES):
            raw = await self._request(
                "/klines",
                {
                    "symbol": pair,
                    "interval": _INTERVALS[interval],
                    "startTime": _milliseconds(page_start),
                    "endTime": _milliseconds(end) - 1,
                    "limit": _KLINES_LIMIT,
                },
                _KLINES_WEIGHT,
            )
            if not isinstance(raw, list):
                raise ProviderResponseError(self.name, "klines response is not a list")
            try:
                rows = [_Kline.from_row(row) for row in raw]
            except ValueError as exc:
                raise ProviderResponseError(self.name, "malformed kline in response") from exc
            for row in rows:
                # The last candle may still be forming.
                if not (start <= row.opened < end) or row.closed >= now:
                    continue
                candles.append(
                    Candle(
                        provider_symbol=symbol,
                        interval=interval,
                        timestamp=row.opened,
                        open=row.open,
                        high=row.high,
                        low=row.low,
                        close=row.close,
                        volume=row.volume,
                        currency=currency,
                    )
                )
            if len(rows) < _KLINES_LIMIT:
                break
            page_start = rows[-1].opened + timedelta(milliseconds=1)
        unique = {candle.timestamp: candle for candle in candles}
        return [unique[timestamp] for timestamp in sorted(unique)]

    async def search_symbols(self, query: str) -> list[SymbolMatch]:
        """Trading spot pairs whose base or quote asset, or pair name, matches the query."""
        raw = await self._request(
            "/exchangeInfo",
            {"permissions": "SPOT", "symbolStatus": "TRADING", "showPermissionSets": "false"},
            _EXCHANGE_INFO_WEIGHT,
        )
        info = self._validate(_ExchangeInfo, raw)
        needle = query.strip().upper().replace("/", "")
        return [
            SymbolMatch(
                provider_symbol=f"{item.baseAsset}/{item.quoteAsset}",
                name=f"{item.baseAsset} / {item.quoteAsset}",
                instrument_type="spot pair",
                currency=item.quoteAsset,
            )
            for item in info.symbols
            if needle in {item.baseAsset, item.quoteAsset} or needle == item.symbol
        ][:50]

    def _pair(self, symbol: str) -> str:
        base, separator, quote = symbol.partition("/")
        if not (separator and base.isalnum() and quote.isalnum()):
            raise ProviderConfigurationError(
                self.name, f"symbol {symbol!r} must be a pair written BASE/QUOTE, like BTC/USDT"
            )
        return f"{base}{quote}".upper()

    async def _request(self, path: str, params: dict[str, Any], weight: int) -> Any:
        try:
            return await self._client.get_json(path, params, cost=weight)
        except ProviderError as exc:
            raise self._translate(exc) from exc

    def _translate(self, exc: ProviderError) -> ProviderError:
        """Binance-specific refusals, named so the health status explains them."""
        if isinstance(exc, ProviderAuthError):
            # No key is sent, so a 403 is Binance's firewall, which also uses it for
            # rate-limit violations.
            return ProviderResponseError(
                self.name, "refused by Binance's web application firewall (HTTP 403)"
            )
        if not isinstance(exc, ProviderResponseError):
            return exc
        if exc.status_code == 451:
            return ProviderResponseError(
                self.name,
                "Binance refuses requests from this location (HTTP 451)",
                status_code=451,
            )
        if exc.status_code == 418:
            return ProviderResponseError(
                self.name,
                "IP address banned for ignoring rate limits (HTTP 418)",
                status_code=418,
            )
        if isinstance(exc.body, dict) and exc.body.get("code") == _INVALID_SYMBOL:
            return ProviderResponseError(
                self.name, "Binance does not list a requested symbol", status_code=exc.status_code
            )
        return exc
