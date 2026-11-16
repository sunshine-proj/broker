"""Shared helpers for provider tests: a fake clock and a small example adapter."""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
from pydantic import BaseModel, SecretStr, field_validator

from app.models.enums import Freshness, ObservationType, QuoteKind
from app.providers.base import Capability, Provider
from app.providers.http import ProviderHttpClient
from app.providers.parsing import parse_decimal, parse_epoch_seconds
from app.providers.rate_limit import RateLimiter
from app.schemas.normalized import Quote
from app.schemas.provider_config import ProviderSettings


class FakeClock:
    """A clock that only moves when something sleeps on it."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def wall(self) -> datetime:
        """Wall-clock time, moving with the clock: 2026-11-16 09:30:15 UTC at the start."""
        return datetime(2026, 11, 16, 9, 30, 15, tzinfo=UTC) + timedelta(seconds=self.now - 1000)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


Handler = Callable[[httpx.Request], httpx.Response]


def make_client(
    handler: Handler,
    *,
    clock: FakeClock | None = None,
    credential: str | None = None,
    **settings: object,
) -> ProviderHttpClient:
    """A ProviderHttpClient whose requests are answered by `handler`; no network."""
    clock = clock or FakeClock()
    config = ProviderSettings.model_validate(settings)
    return ProviderHttpClient(
        provider="example",
        base_url="https://api.example.test/v1",
        settings=config,
        credential=SecretStr(credential) if credential else None,
        limiter=RateLimiter(
            "example",
            config.rate_limits,
            max_wait_seconds=config.rate_limit_max_wait_seconds,
            clock=clock,
            sleep=clock.sleep,
        ),
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        sleep=clock.sleep,
        jitter=lambda: 1.0,
        wall_clock=clock.wall,
    )


class _ExampleQuote(BaseModel):
    """The example provider's own response shape."""

    symbol: str
    price: Decimal
    ts: datetime

    _price = field_validator("price", mode="before")(parse_decimal)
    _ts = field_validator("ts", mode="before")(parse_epoch_seconds)


class _ExampleQuotes(BaseModel):
    data: list[_ExampleQuote]


class ExampleProvider(Provider):
    """A minimal adapter, written the way a real one is."""

    name = "example"
    capabilities = frozenset({Capability.QUOTES})

    async def fetch_quotes(
        self, symbols: Sequence[str], observation_types: object = None
    ) -> list[Quote]:
        raw = await self._client.get_json(
            "/quotes", {"symbols": ",".join(symbols)}, cost=len(symbols)
        )
        parsed = self._validate(_ExampleQuotes, raw)
        return [
            Quote(
                provider_symbol=item.symbol,
                observation_type=ObservationType.PRICE,
                observed_at=item.ts,
                value=item.price,
                currency="USD",
                unit="currency",
                freshness=Freshness.DELAYED,
                quote_kind=QuoteKind.TRADED,
            )
            for item in parsed.data
        ]
