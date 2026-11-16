"""CoinGecko adapter: cryptocurrency prices, market data, supply and dominance.

Provider symbols have the form `<coin id>/<vs currency>`, for example
`bitcoin/usd`: CoinGecko's coin id, and the currency values are quoted in.
API facts this adapter relies on are recorded in docs/providers/coingecko.md.
"""

import logging
from collections import defaultdict
from collections.abc import Callable, Collection, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import batched

from pydantic import BaseModel, RootModel, field_validator

from app.models.enums import Freshness, ObservationType, QuoteKind
from app.providers.base import Capability, Provider
from app.providers.errors import (
    ProviderAuthError,
    ProviderConfigurationError,
    ProviderResponseError,
)
from app.providers.factory import register_adapter
from app.providers.http import ProviderHttpClient
from app.providers.parsing import (
    parse_decimal,
    parse_epoch_milliseconds,
    parse_epoch_seconds,
    parse_utc_datetime,
)
from app.schemas.normalized import Quote, SymbolMatch

logger = logging.getLogger(__name__)

# /coins/markets accepts at most this many ids per request.
_MAX_IDS_PER_REQUEST = 250

# CoinGecko revises historical values for up to 2 days after the date. Stored
# observations are immutable, so history is only taken once it has settled.
SETTLEMENT_PERIOD = timedelta(days=2)

# The free plans serve history for the past 365 days only. A request reaching
# further back is refused as a whole, so the range is shortened instead; the
# hour of slack keeps a request made a little later inside the limit.
HISTORY_LIMIT = timedelta(days=365) - timedelta(hours=1)

# CoinGecko's error code for a request beyond the plan's history range.
_ERROR_BEYOND_HISTORY = 10012

# Values on the free plans are served from a cache of 1 to 5 minutes.
_FRESHNESS = Freshness.DELAYED

_MARKET_TYPES = frozenset(
    {
        ObservationType.PRICE,
        ObservationType.MARKET_CAP,
        ObservationType.VOLUME,
        ObservationType.CIRCULATING_SUPPLY,
        ObservationType.TOTAL_SUPPLY,
    }
)
_ALL_TYPES = _MARKET_TYPES | {ObservationType.DOMINANCE}

# Where a value is missing, CoinGecko sends null. A listed coin cannot have a
# market capitalization or circulating supply of zero, so zero is treated as
# missing too rather than stored.
_ZERO_MEANS_MISSING = frozenset({ObservationType.MARKET_CAP, ObservationType.CIRCULATING_SUPPLY})


def _optional_decimal(value: object) -> Decimal | None:
    return None if value is None else parse_decimal(value)


class _Market(BaseModel):
    id: str
    symbol: str
    current_price: Decimal | None = None
    market_cap: Decimal | None = None
    total_volume: Decimal | None = None
    circulating_supply: Decimal | None = None
    total_supply: Decimal | None = None
    last_updated: datetime

    _numbers = field_validator(
        "current_price",
        "market_cap",
        "total_volume",
        "circulating_supply",
        "total_supply",
        mode="before",
    )(_optional_decimal)
    _last_updated = field_validator("last_updated", mode="before")(parse_utc_datetime)


class _Markets(RootModel[list[_Market]]):
    pass


class _GlobalData(BaseModel):
    # Share of total market capitalization, in percent, keyed by lower-case
    # coin symbol. Only the largest coins are listed.
    market_cap_percentage: dict[str, Decimal]
    updated_at: datetime

    @field_validator("market_cap_percentage", mode="before")
    @classmethod
    def _percentages(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        return {key: parse_decimal(share) for key, share in value.items()}

    _updated_at = field_validator("updated_at", mode="before")(parse_epoch_seconds)


class _Global(BaseModel):
    data: _GlobalData


class _Point(BaseModel):
    at: datetime
    value: Decimal | None


def _points(value: object) -> list[_Point]:
    """[[milliseconds, value], ...] as points."""
    if not isinstance(value, list):
        raise ValueError("expected a list of [timestamp, value] pairs")
    points = []
    for pair in value:
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError("expected [timestamp, value]")
        points.append(
            _Point(at=parse_epoch_milliseconds(pair[0]), value=_optional_decimal(pair[1]))
        )
    return points


class _MarketChart(BaseModel):
    prices: list[_Point]
    market_caps: list[_Point]
    total_volumes: list[_Point]

    _series = field_validator("prices", "market_caps", "total_volumes", mode="before")(_points)


class _SearchCoin(BaseModel):
    id: str
    name: str
    symbol: str


class _Search(BaseModel):
    coins: list[_SearchCoin]


def _split_symbol(provider: str, symbol: str) -> tuple[str, str]:
    coin, separator, currency = symbol.partition("/")
    if not separator or not coin or not currency:
        raise ProviderConfigurationError(
            provider, f"symbol {symbol!r} must have the form <coin id>/<vs currency>"
        )
    return coin, currency.lower()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _error_code(body: object) -> object:
    """CoinGecko's numeric error code, from either of its error body shapes."""
    if not isinstance(body, dict):
        return None
    error = body.get("error", body)
    status = error.get("status") if isinstance(error, dict) else None
    return status.get("error_code") if isinstance(status, dict) else None


@register_adapter
class CoinGeckoProvider(Provider):
    name = "coingecko"
    capabilities = frozenset(
        {Capability.QUOTES, Capability.QUOTE_HISTORY, Capability.SYMBOL_SEARCH}
    )

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
        """Price, market data and supply of each coin, plus dominance if requested.

        One request per vs currency (and per 250 coins) for market data, and one
        more for dominance. A coin CoinGecko does not know is left out.
        """
        wanted = _ALL_TYPES if observation_types is None else _ALL_TYPES & set(observation_types)
        if not symbols or not wanted:
            return []

        by_currency: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for symbol in symbols:
            coin, currency = _split_symbol(self.name, symbol)
            by_currency[currency].append((coin, symbol))

        batch_size = min(
            _MAX_IDS_PER_REQUEST, self._client.settings.max_batch_size or _MAX_IDS_PER_REQUEST
        )
        quotes: list[Quote] = []
        # Coin ticker -> provider symbols, for matching dominance figures.
        tickers: dict[str, list[str]] = defaultdict(list)
        for currency, coins in by_currency.items():
            for batch in batched(coins, batch_size):
                markets = await self._markets(currency, [coin for coin, _ in batch])
                symbol_of = dict(batch)
                for market in markets:
                    symbol = symbol_of.get(market.id)
                    if symbol is None:
                        continue
                    tickers[market.symbol.lower()].append(symbol)
                    quotes += self._market_quotes(symbol, currency, market, wanted)
                missing = set(symbol_of) - {market.id for market in markets}
                for coin in sorted(missing):
                    logger.warning(
                        "Coin not returned by the provider",
                        extra={"provider": self.name, "symbol": symbol_of[coin]},
                    )

        if ObservationType.DOMINANCE in wanted:
            quotes += await self._dominance(tickers)
        return quotes

    async def _markets(self, currency: str, coins: list[str]) -> list[_Market]:
        raw = await self._client.get_json(
            "/coins/markets",
            {
                "vs_currency": currency,
                "ids": ",".join(coins),
                "per_page": _MAX_IDS_PER_REQUEST,
                "precision": "full",
            },
        )
        return self._validate(_Markets, raw).root

    def _market_quotes(
        self,
        symbol: str,
        currency: str,
        market: _Market,
        wanted: Collection[ObservationType],
    ) -> list[Quote]:
        money = currency.upper()
        values = {
            ObservationType.PRICE: (market.current_price, money, "currency"),
            ObservationType.MARKET_CAP: (market.market_cap, money, "currency"),
            ObservationType.VOLUME: (market.total_volume, money, "currency"),
            ObservationType.CIRCULATING_SUPPLY: (market.circulating_supply, None, "count"),
            ObservationType.TOTAL_SUPPLY: (market.total_supply, None, "count"),
        }
        quotes = []
        for observation_type, (value, quote_currency, unit) in values.items():
            if observation_type not in wanted or value is None:
                continue
            if value == 0 and observation_type in _ZERO_MEANS_MISSING:
                continue
            quotes.append(
                Quote(
                    provider_symbol=symbol,
                    observation_type=observation_type,
                    observed_at=market.last_updated,
                    value=value,
                    currency=quote_currency,
                    unit=unit,
                    freshness=_FRESHNESS,
                    # An average across exchanges, not a price anyone can trade at.
                    quote_kind=QuoteKind.INDICATIVE,
                    # CoinGecko's own name for the volume figure, whose period
                    # the API reference does not state.
                    metadata={"field": "total_volume"}
                    if observation_type is ObservationType.VOLUME
                    else None,
                )
            )
        return quotes

    async def _dominance(self, tickers: dict[str, list[str]]) -> list[Quote]:
        raw = await self._client.get_json("/global")
        data = self._validate(_Global, raw).data
        quotes = []
        for ticker, symbols in tickers.items():
            share = data.market_cap_percentage.get(ticker)
            if share is None:
                # Only the largest coins are listed; the rest have no figure.
                continue
            if len(symbols) > 1 and len({s.partition("/")[0] for s in symbols}) > 1:
                logger.warning(
                    "Ticker is shared by several tracked coins; dominance skipped",
                    extra={"provider": self.name, "ticker": ticker},
                )
                continue
            quotes += [
                Quote(
                    provider_symbol=symbol,
                    observation_type=ObservationType.DOMINANCE,
                    observed_at=data.updated_at,
                    value=share,
                    unit="percent",
                    freshness=_FRESHNESS,
                    quote_kind=QuoteKind.INDICATIVE,
                )
                for symbol in symbols
            ]
        return quotes

    async def fetch_quote_history(self, symbol: str, start: datetime, end: datetime) -> list[Quote]:
        """Historical price, market capitalization and volume, settled values only.

        CoinGecko picks the spacing: hourly for ranges up to 90 days, daily
        (at 00:00 UTC) beyond. Points from the last two days are left out
        because CoinGecko may still revise them.
        """
        coin, currency = _split_symbol(self.name, symbol)
        now = self._clock()
        end = min(end, now - SETTLEMENT_PERIOD)
        if start < now - HISTORY_LIMIT:
            logger.warning(
                "History before the plan's limit cannot be fetched; range shortened",
                extra={
                    "provider": self.name,
                    "symbol": symbol,
                    "requested_start": start.isoformat(),
                    "start": (now - HISTORY_LIMIT).isoformat(),
                },
            )
            start = now - HISTORY_LIMIT
        if end <= start:
            return []

        try:
            raw = await self._client.get_json(
                f"/coins/{coin}/market_chart/range",
                {
                    "vs_currency": currency,
                    "from": int(start.timestamp()),
                    "to": int(end.timestamp()),
                    "precision": "full",
                },
            )
        except ProviderAuthError as exc:
            if _error_code(exc.body) == _ERROR_BEYOND_HISTORY:
                raise ProviderResponseError(
                    self.name, "requested history is beyond what the plan allows"
                ) from exc
            raise
        chart = self._validate(_MarketChart, raw)
        money = currency.upper()
        series = (
            (ObservationType.PRICE, chart.prices),
            (ObservationType.MARKET_CAP, chart.market_caps),
            (ObservationType.VOLUME, chart.total_volumes),
        )
        quotes = []
        for observation_type, points in series:
            for point in points:
                if point.value is None or not (start <= point.at < end):
                    continue
                if point.value == 0 and observation_type in _ZERO_MEANS_MISSING:
                    continue
                quotes.append(
                    Quote(
                        provider_symbol=symbol,
                        observation_type=observation_type,
                        observed_at=point.at,
                        value=point.value,
                        currency=money,
                        unit="currency",
                        freshness=_FRESHNESS,
                        quote_kind=QuoteKind.INDICATIVE,
                        metadata={"field": "total_volumes"}
                        if observation_type is ObservationType.VOLUME
                        else None,
                    )
                )
        return quotes

    async def search_symbols(self, query: str) -> list[SymbolMatch]:
        """Coins matching `query`, largest first.

        The provider symbol shown is the coin id. Use it as `<id>/<currency>`
        in the seed file, for example `bitcoin/usd`.
        """
        raw = await self._client.get_json("/search", {"query": query})
        search = self._validate(_Search, raw)
        return [
            SymbolMatch(
                provider_symbol=coin.id,
                name=coin.name,
                instrument_type=f"crypto ({coin.symbol.upper()})",
            )
            for coin in search.coins
        ]
