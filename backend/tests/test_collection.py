"""Collection service and its CLI, with in-memory collaborators."""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app import cli
from app.config.settings import Settings
from app.models.enums import CandleInterval, Freshness, ObservationType, QuoteKind
from app.providers.errors import ProviderConfigurationError, ProviderUnavailableError
from app.schemas.normalized import Candle, IndicatorValue, Quote
from app.services.collection import CollectionOutcome, CollectionService
from app.services.ingestion import IngestionResult
from app.services.provider_health import ProviderHealthService

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
PROVIDER = SimpleNamespace(id=uuid.uuid4(), name="example")


def quote(symbol: str) -> Quote:
    return Quote(
        provider_symbol=symbol,
        observation_type=ObservationType.PRICE,
        observed_at=NOW,
        value=Decimal("1"),
        currency="USD",
        unit="currency",
        freshness=Freshness.DELAYED,
        quote_kind=QuoteKind.TRADED,
    )


def candle(symbol: str) -> Candle:
    return Candle(
        provider_symbol=symbol,
        interval=CandleInterval.DAY_1,
        timestamp=NOW,
        open=Decimal("1"),
        high=Decimal("1"),
        low=Decimal("1"),
        close=Decimal("1"),
    )


class StubProvider:
    def __init__(self, failing: set[str]) -> None:
        self.failing = failing
        self.quote_requests: list[list[str]] = []
        self.candle_requests: list[str] = []
        self.history_requests: list[str] = []
        self.requested_types: Any = None

    async def fetch_quotes(
        self, symbols: Sequence[str], observation_types: Any = None
    ) -> list[Quote]:
        self.quote_requests.append(list(symbols))
        self.requested_types = observation_types
        if self.failing:
            raise ProviderUnavailableError("example", "request timed out")
        return [quote(symbol) for symbol in symbols]

    async def fetch_quote_history(self, symbol: str, *args: Any) -> list[Quote]:
        self.history_requests.append(symbol)
        if symbol in self.failing:
            raise ProviderUnavailableError("example", "request timed out")
        return [quote(symbol)]

    async def fetch_indicator(self, series_code: str, *args: Any) -> list[IndicatorValue]:
        if series_code.startswith("BROKEN"):
            raise ProviderUnavailableError("example", "request timed out")
        return [IndicatorValue(series_code=series_code, observed_at=NOW, value=Decimal("2.1"))]

    async def fetch_candles(self, symbol: str, *args: Any) -> list[Candle]:
        self.candle_requests.append(symbol)
        if symbol in self.failing:
            raise ProviderUnavailableError("example", "request timed out")
        return [candle(symbol)]


class StubFactory:
    def __init__(self, provider: StubProvider, error: Exception | None = None) -> None:
        self.provider = provider
        self.error = error

    def build(self, row: Any) -> StubProvider:
        if self.error is not None:
            raise self.error
        return self.provider


class StubInstruments:
    def __init__(self, symbols: list[str]) -> None:
        self.symbols = symbols

    async def list_mappings(self, provider_id: uuid.UUID) -> list[Any]:
        return [SimpleNamespace(provider_symbol=symbol) for symbol in self.symbols]


class StubEconomicData:
    async def list_indicators(self, **filters: Any) -> list[Any]:
        assert filters["enabled"] is True
        return [
            SimpleNamespace(provider_series_code=code)
            for code in ("ICP/M.U2.N.000000.4.ANR", "BROKEN/X", "MNA/Q.Y.I9")
        ]


class StubIngestion:
    def __init__(self) -> None:
        self.quotes: list[Quote] = []
        self.candles: list[Candle] = []
        self.indicator_values: list[IndicatorValue] = []

    async def ingest_quotes(
        self, provider_id: uuid.UUID, quotes: Sequence[Quote]
    ) -> IngestionResult:
        self.quotes += quotes
        return IngestionResult(received=len(quotes), written=len(quotes))

    async def ingest_indicator_values(
        self, provider_id: uuid.UUID, values: Sequence[IndicatorValue]
    ) -> IngestionResult:
        self.indicator_values += values
        return IngestionResult(received=len(values), written=len(values))

    async def ingest_candles(
        self, provider_id: uuid.UUID, candles: Sequence[Candle]
    ) -> IngestionResult:
        self.candles += candles
        return IngestionResult(received=len(candles), written=len(candles))


class StubHealthRepository:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def record_success(self, *args: Any) -> None:
        self.events.append("success")

    async def record_failure(self, provider_id: Any, at: Any, error: str, **kwargs: Any) -> None:
        self.events.append(f"failure: {error}")


def build(
    symbols: list[str], *, failing: set[str] | None = None, build_error: Exception | None = None
) -> SimpleNamespace:
    provider = StubProvider(failing or set())
    ingestion, health = StubIngestion(), StubHealthRepository()
    service = CollectionService(
        factory=StubFactory(provider, build_error),  # type: ignore[arg-type]
        instruments=StubInstruments(symbols),  # type: ignore[arg-type]
        economic_data=StubEconomicData(),  # type: ignore[arg-type]
        ingestion=ingestion,  # type: ignore[arg-type]
        health=ProviderHealthService(health),  # type: ignore[arg-type]
    )
    return SimpleNamespace(service=service, provider=provider, ingestion=ingestion, health=health)


async def test_quotes_of_all_mapped_symbols_are_fetched_in_one_call_and_stored() -> None:
    world = build(["AAPL", "EUR/USD"])

    outcome = await world.service.collect_quotes(PROVIDER)

    assert world.provider.quote_requests == [["AAPL", "EUR/USD"]]
    assert [q.provider_symbol for q in world.ingestion.quotes] == ["AAPL", "EUR/USD"]
    assert (outcome.requested, outcome.succeeded, outcome.result.written) == (2, True, 2)
    assert world.health.events == ["success"]


async def test_provider_failure_is_contained_and_recorded() -> None:
    world = build(["AAPL"], failing={"AAPL"})

    outcome = await world.service.collect_quotes(PROVIDER)

    assert (outcome.succeeded, outcome.result.written) == (False, 0)
    assert world.ingestion.quotes == []
    assert world.health.events == ["failure: ProviderUnavailableError: request timed out"]


async def test_missing_credential_is_recorded_as_a_provider_failure() -> None:
    error = ProviderConfigurationError("example", "environment variable EXAMPLE_API_KEY is not set")
    world = build(["AAPL"], build_error=error)

    outcome = await world.service.collect_quotes(PROVIDER)

    assert outcome.succeeded is False
    assert "EXAMPLE_API_KEY is not set" in world.health.events[0]


async def test_provider_without_instruments_is_not_called() -> None:
    world = build([])

    outcome = await world.service.collect_quotes(PROVIDER)

    assert (outcome.requested, outcome.succeeded) == (0, True)
    assert world.provider.quote_requests == []
    assert world.health.events == []


async def test_candles_continue_past_a_failing_symbol() -> None:
    world = build(["AAPL", "BROKEN", "MSFT"], failing={"BROKEN"})

    outcome = await world.service.collect_candles(PROVIDER, CandleInterval.DAY_1, NOW, NOW)

    assert world.provider.candle_requests == ["AAPL", "BROKEN", "MSFT"]
    assert [c.provider_symbol for c in world.ingestion.candles] == ["AAPL", "MSFT"]
    assert (outcome.requested, outcome.succeeded, outcome.result.written) == (3, False, 2)
    assert world.health.events == [
        "success",
        "failure: ProviderUnavailableError: request timed out",
        "success",
    ]


async def test_requested_observation_types_reach_the_provider() -> None:
    world = build(["AAPL"])

    await world.service.collect_quotes(PROVIDER, {ObservationType.PRICE})

    assert world.provider.requested_types == {ObservationType.PRICE}


async def test_quote_history_continues_past_a_failing_symbol() -> None:
    world = build(["bitcoin/usd", "broken/usd", "ethereum/usd"], failing={"broken/usd"})

    outcome = await world.service.collect_quote_history(PROVIDER, NOW, NOW)

    assert world.provider.history_requests == ["bitcoin/usd", "broken/usd", "ethereum/usd"]
    assert [q.provider_symbol for q in world.ingestion.quotes] == ["bitcoin/usd", "ethereum/usd"]
    assert (outcome.requested, outcome.succeeded, outcome.result.written) == (3, False, 2)


async def test_indicators_of_the_provider_are_collected_past_a_failing_series() -> None:
    world = build([])

    outcome = await world.service.collect_indicators(PROVIDER, NOW, NOW)

    assert [v.series_code for v in world.ingestion.indicator_values] == [
        "ICP/M.U2.N.000000.4.ANR",
        "MNA/Q.Y.I9",
    ]
    assert (outcome.requested, outcome.succeeded, outcome.result.written) == (3, False, 2)


# --- Command line --------------------------------------------------------------------


@pytest.fixture
def quiet_cli(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "configure_logging", lambda *args: None)


@pytest.mark.parametrize(("succeeded", "exit_code"), [(True, 0), (False, 2)])
def test_collect_exit_code_reports_provider_failure(
    monkeypatch: pytest.MonkeyPatch, quiet_cli: None, succeeded: bool, exit_code: int
) -> None:
    calls = []

    async def fake_collect(settings: Settings, request: cli.CollectRequest) -> CollectionOutcome:
        calls.append(request)
        return CollectionOutcome("example", 1, succeeded, IngestionResult())

    monkeypatch.setattr(cli, "collect", fake_collect)

    assert (
        cli.main(["collect", "candles", "example", "--interval", "1h", "--days", "7"]) == exit_code
    )
    assert calls == [cli.CollectRequest("candles", "example", CandleInterval.HOUR_1, 7)]


def test_collect_quotes_uses_default_candle_arguments(
    monkeypatch: pytest.MonkeyPatch, quiet_cli: None
) -> None:
    calls = []

    async def fake_collect(settings: Settings, request: cli.CollectRequest) -> CollectionOutcome:
        calls.append(request)
        return CollectionOutcome("example", 1, True, IngestionResult())

    monkeypatch.setattr(cli, "collect", fake_collect)

    assert cli.main(["collect", "quotes", "example"]) == 0
    assert cli.main(["collect", "quotes", "example", "--types", "price, dominance"]) == 0
    assert cli.main(["collect", "history", "example", "--days", "90"]) == 0
    assert cli.main(["collect", "indicators", "example"]) == 0
    assert calls == [
        cli.CollectRequest("quotes", "example"),
        cli.CollectRequest(
            "quotes",
            "example",
            types=frozenset({ObservationType.PRICE, ObservationType.DOMINANCE}),
        ),
        cli.CollectRequest("history", "example", days=90),
        cli.CollectRequest("indicators", "example", days=730),
    ]


def test_unknown_provider_is_reported(monkeypatch: pytest.MonkeyPatch, quiet_cli: None) -> None:
    async def fake_collect(*args: Any) -> CollectionOutcome:
        raise cli.CommandError("Unknown provider: nope")

    monkeypatch.setattr(cli, "collect", fake_collect)

    assert cli.main(["collect", "quotes", "nope"]) == 1


def test_invalid_interval_is_refused_by_the_parser(quiet_cli: None) -> None:
    with pytest.raises(SystemExit):
        cli.main(["collect", "candles", "example", "--interval", "7min"])


def test_invalid_observation_type_is_refused_by_the_parser(quiet_cli: None) -> None:
    with pytest.raises(SystemExit):
        cli.main(["collect", "quotes", "example", "--types", "price,mood"])
