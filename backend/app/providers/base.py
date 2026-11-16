"""The common provider interface.

Every adapter subclasses `Provider`, declares what it can fetch, and returns
only the normalized schemas from `app.schemas.normalized`. No provider-specific
response shape leaves an adapter.
"""

from collections.abc import Collection, Sequence
from datetime import datetime
from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel, ValidationError

from app.models.enums import CandleInterval, ObservationType
from app.providers.errors import ProviderResponseError, UnsupportedOperationError
from app.providers.http import ProviderHttpClient
from app.schemas.normalized import Candle, IndicatorValue, Quote, SymbolMatch


class Capability(StrEnum):
    QUOTES = "quotes"
    CANDLES = "candles"
    INDICATORS = "indicators"
    SYMBOL_SEARCH = "symbol_search"
    QUOTE_HISTORY = "quote_history"


class Provider:
    # Must equal the provider's `name` in the seed file.
    name: ClassVar[str]
    capabilities: ClassVar[frozenset[Capability]] = frozenset()

    def __init__(self, client: ProviderHttpClient) -> None:
        self._client = client

    async def fetch_quotes(
        self,
        symbols: Sequence[str],
        observation_types: Collection[ObservationType] | None = None,
    ) -> list[Quote]:
        """Latest values for the given provider symbols.

        `observation_types` limits what is fetched; None means everything the
        adapter offers. An adapter may save requests by skipping a type.
        """
        raise UnsupportedOperationError(self.name, "quotes are not supported")

    async def fetch_quote_history(self, symbol: str, start: datetime, end: datetime) -> list[Quote]:
        """Past values of one symbol with start <= time < end."""
        raise UnsupportedOperationError(self.name, "quote history is not supported")

    async def fetch_candles(
        self, symbol: str, interval: CandleInterval, start: datetime, end: datetime
    ) -> list[Candle]:
        """Closed candles with start <= open time < end."""
        raise UnsupportedOperationError(self.name, "candles are not supported")

    async def fetch_indicator(
        self, series_code: str, start: datetime, end: datetime
    ) -> list[IndicatorValue]:
        """Values of an economic series for periods with start <= period < end."""
        raise UnsupportedOperationError(self.name, "economic indicators are not supported")

    async def search_symbols(self, query: str) -> list[SymbolMatch]:
        """Instruments the provider knows that match a name or ticker."""
        raise UnsupportedOperationError(self.name, "symbol search is not supported")

    def _validate[T: BaseModel](self, model: type[T], data: object) -> T:
        """Validate a raw provider response against the adapter's own model of it."""
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            # Field locations and error types only: the payload itself is not echoed.
            problems = "; ".join(
                f"{'.'.join(str(part) for part in error['loc'])}: {error['type']}"
                for error in exc.errors()[:5]
            )
            raise ProviderResponseError(
                self.name, f"response did not match {model.__name__} ({problems})"
            ) from exc
