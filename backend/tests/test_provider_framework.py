"""Provider framework without network or database: configuration, parsing,
the adapter base class, the factory, validation, ingestion and health."""

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from app.config.settings import Settings
from app.models.enums import CandleInterval, Freshness, ObservationType, QuoteKind
from app.providers.base import Capability, Provider
from app.providers.errors import (
    ProviderConfigurationError,
    ProviderResponseError,
    ProviderUnavailableError,
    UnsupportedOperationError,
)
from app.providers.factory import ProviderFactory
from app.providers.parsing import (
    assume_utc,
    parse_decimal,
    parse_epoch_milliseconds,
    parse_epoch_seconds,
    parse_utc_date,
    parse_utc_datetime,
)
from app.repositories.interfaces import RevisionWriteResult, WriteResult
from app.schemas.market_data import EconomicObservationIn, MarketObservationIn, OHLCVIn
from app.schemas.normalized import Candle, IndicatorValue, Quote
from app.schemas.provider_config import ProviderSettings
from app.services.ingestion import IngestionService
from app.services.provider_health import DOWN_AFTER_FAILURES, ProviderHealthService
from app.services.validation import validate_candle, validate_indicator_value, validate_quote
from tests.providers_support import ExampleProvider, make_client

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
PROVIDER_ID = uuid.uuid4()


def quote(**overrides: Any) -> Quote:
    fields: dict[str, Any] = {
        "provider_symbol": "AAPL",
        "observation_type": ObservationType.PRICE,
        "observed_at": NOW - timedelta(minutes=15),
        "value": Decimal("189.95"),
        "currency": "USD",
        "unit": "currency",
        "freshness": Freshness.DELAYED,
        "quote_kind": QuoteKind.TRADED,
    }
    return Quote(**{**fields, **overrides})


def candle(**overrides: Any) -> Candle:
    fields: dict[str, Any] = {
        "provider_symbol": "AAPL",
        "interval": CandleInterval.DAY_1,
        "timestamp": NOW - timedelta(days=1),
        "open": Decimal("188"),
        "high": Decimal("191"),
        "low": Decimal("187"),
        "close": Decimal("190"),
        "volume": Decimal("1000"),
        "currency": "USD",
    }
    return Candle(**{**fields, **overrides})


# --- Provider configuration ----------------------------------------------------


def test_configuration_defaults() -> None:
    config = ProviderSettings()

    assert (config.timeout_seconds, config.max_retries) == (10.0, 3)
    assert config.rate_limits == ()
    assert config.auth.type == "none"


def test_configuration_round_trips_through_json() -> None:
    config = ProviderSettings.model_validate(
        {
            "rate_limits": [{"limit": 8, "period": "minute"}, {"limit": 800, "period": "day"}],
            "auth": {"type": "query", "name": "apikey", "env": "TWELVE_DATA_API_KEY"},
        }
    )

    assert ProviderSettings.model_validate(config.model_dump(mode="json")) == config
    assert [limit.period.seconds for limit in config.rate_limits] == [60, 86400]


@pytest.mark.parametrize(
    ("configuration", "message"),
    [
        ({"timeout": 5}, "timeout"),
        ({"timeout_seconds": 0}, "greater than 0"),
        ({"max_retries": 50}, "less than or equal to 10"),
        ({"rate_limits": [{"limit": 0, "period": "minute"}]}, "greater than 0"),
        ({"rate_limits": [{"limit": 5, "period": "fortnight"}]}, "period"),
        ({"auth": {"type": "query"}}, "needs both"),
        ({"auth": {"type": "header", "name": "X-Key", "env": "POSTGRES_PASSWORD"}}, "_API_KEY"),
        ({"auth": {"type": "query", "name": "k", "env": "A_KEY", "api_key": "x"}}, "api_key"),
    ],
)
def test_invalid_configuration_is_rejected(configuration: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        ProviderSettings.model_validate(configuration)


# --- Parsing helpers -------------------------------------------------------------


def test_parse_decimal_is_exact() -> None:
    assert parse_decimal("0.1") + parse_decimal("0.2") == Decimal("0.3")
    assert parse_decimal(" 189.9500 ") == Decimal("189.9500")
    assert parse_decimal(42) == Decimal(42)
    assert parse_decimal(Decimal("1e-18")) == Decimal("0.000000000000000001")


@pytest.mark.parametrize("value", [0.1, True, None, "", "n/a", "NaN", "Infinity", [1]])
def test_parse_decimal_refuses_what_is_not_an_exact_number(value: object) -> None:
    with pytest.raises(ValueError):
        parse_decimal(value)


def test_timestamps_are_converted_to_utc() -> None:
    expected = datetime(2026, 11, 16, 8, 0, tzinfo=UTC)

    assert parse_utc_datetime("2026-11-16T10:00:00+02:00") == expected
    assert parse_utc_datetime("2026-11-16T08:00:00Z") == expected
    assert assume_utc("2026-11-16 08:00:00") == expected
    assert parse_epoch_seconds(1794816000) == expected
    assert parse_epoch_milliseconds("1794816000000") == expected
    assert parse_utc_date("2026-11-16") == datetime(2026, 11, 16, tzinfo=UTC)


def test_timestamp_without_timezone_is_refused() -> None:
    with pytest.raises(ValueError, match="no timezone"):
        parse_utc_datetime("2026-11-16T10:00:00")


# --- Adapter base class ------------------------------------------------------------

pytest_anyio = pytest.mark.anyio


@pytest_anyio
async def test_adapter_returns_normalized_quotes() -> None:
    body = b'{"data": [{"symbol": "AAPL", "price": 189.9500, "ts": 1794816000}]}'
    provider = ExampleProvider(make_client(lambda request: httpx.Response(200, content=body)))

    (result,) = await provider.fetch_quotes(["AAPL"])

    assert result.provider_symbol == "AAPL"
    assert result.value == Decimal("189.9500")
    assert result.observed_at == datetime(2026, 11, 16, 8, 0, tzinfo=UTC)
    assert (result.freshness, result.quote_kind) == (Freshness.DELAYED, QuoteKind.TRADED)


@pytest_anyio
@pytest.mark.parametrize(
    "body",
    [
        b'{"data": [{"symbol": "AAPL", "price": "n/a", "ts": 1794816000}]}',
        b'{"data": [{"symbol": "AAPL", "ts": 1794816000}]}',
        b'{"status": "error", "message": "symbol not found"}',
        b"[]",
    ],
)
async def test_malformed_response_is_a_response_error(body: bytes) -> None:
    provider = ExampleProvider(make_client(lambda request: httpx.Response(200, content=body)))

    with pytest.raises(ProviderResponseError, match="did not match"):
        await provider.fetch_quotes(["AAPL"])


@pytest_anyio
async def test_validation_error_does_not_echo_the_payload() -> None:
    body = b'{"data": [{"symbol": "AAPL", "price": "confidential-text", "ts": 1794816000}]}'
    provider = ExampleProvider(make_client(lambda request: httpx.Response(200, content=body)))

    with pytest.raises(ProviderResponseError) as raised:
        await provider.fetch_quotes(["AAPL"])

    assert "confidential-text" not in str(raised.value)
    assert "data.0.price" in str(raised.value)


@pytest_anyio
async def test_unsupported_operations_say_so() -> None:
    provider = ExampleProvider(make_client(lambda request: httpx.Response(200, json={})))

    assert provider.capabilities == {Capability.QUOTES}
    with pytest.raises(UnsupportedOperationError, match="candles are not supported"):
        await provider.fetch_candles("AAPL", CandleInterval.DAY_1, NOW, NOW)
    with pytest.raises(UnsupportedOperationError):
        await provider.fetch_indicator("CPI", NOW, NOW)


# --- Factory -------------------------------------------------------------------------


def provider_row(**overrides: Any) -> Any:
    fields = {
        "name": "example",
        "base_url": "https://api.example.test",
        "enabled": True,
        "configuration": {"rate_limits": [{"limit": 8, "period": "minute"}]},
    }
    return SimpleNamespace(**{**fields, **overrides})


def factory(
    settings: Settings, adapters: dict[str, type[Provider]] | None = None
) -> ProviderFactory:
    return ProviderFactory(
        settings,
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
        adapters={"example": ExampleProvider} if adapters is None else adapters,
    )


def test_factory_builds_the_registered_adapter(settings: Settings) -> None:
    assert isinstance(factory(settings).build(provider_row()), ExampleProvider)


def test_factory_shares_one_rate_limiter_per_provider(settings: Settings) -> None:
    provider_factory = factory(settings)

    first = provider_factory.build(provider_row())
    second = provider_factory.build(provider_row())

    assert first._client._limiter is second._client._limiter


def test_factory_reads_the_credential_from_the_environment(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXAMPLE_API_KEY", "from-environment")
    auth = {"type": "query", "name": "apikey", "env": "EXAMPLE_API_KEY"}

    provider = factory(settings).build(provider_row(configuration={"auth": auth}))

    assert provider._client._credential.get_secret_value() == "from-environment"


@pytest.mark.parametrize(
    ("row", "message"),
    [
        ({"name": "unknown"}, "no adapter is implemented"),
        ({"enabled": False}, "disabled"),
        ({"base_url": "http://api.example.test"}, "https"),
        (
            {"configuration": {"auth": {"type": "query", "name": "k", "env": "ABSENT_API_KEY"}}},
            "ABSENT_API_KEY is not set",
        ),
    ],
)
def test_factory_refuses_unusable_providers(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, row: dict[str, Any], message: str
) -> None:
    monkeypatch.delenv("ABSENT_API_KEY", raising=False)

    with pytest.raises(ProviderConfigurationError, match=message):
        factory(settings).build(provider_row(**row))


# --- Ingestion validation ----------------------------------------------------------------


def test_valid_quote_passes() -> None:
    assert validate_quote(quote(), "USD", NOW) is None


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"observed_at": NOW + timedelta(hours=1)}, "in the future"),
        ({"currency": "EUR"}, "does not match the instrument's USD"),
        ({"currency": None}, "price has no currency"),
    ],
)
def test_invalid_quote_is_rejected(overrides: dict[str, Any], reason: str) -> None:
    problem = validate_quote(quote(**overrides), "USD", NOW)

    assert problem is not None and reason in problem


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_numbers_cannot_become_normalized_values(value: str) -> None:
    with pytest.raises(ValidationError, match="finite"):
        quote(value=Decimal(value))
    with pytest.raises(ValidationError, match="finite"):
        candle(close=Decimal(value))


def test_small_clock_difference_is_tolerated() -> None:
    assert validate_quote(quote(observed_at=NOW + timedelta(minutes=2)), "USD", NOW) is None


def test_values_without_currency_are_accepted_where_it_does_not_apply() -> None:
    treasury_yield = quote(
        observation_type=ObservationType.YIELD,
        currency=None,
        unit="percent",
        value=Decimal("-0.25"),
    )

    assert validate_quote(treasury_yield, None, NOW) is None


def test_valid_candle_passes() -> None:
    assert validate_candle(candle(), "USD", NOW) is None
    assert validate_candle(candle(volume=None), "USD", NOW) is None


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"high": Decimal("189")}, "high is below"),
        ({"low": Decimal("189")}, "low is above"),
        ({"volume": Decimal("-1")}, "volume"),
        ({"timestamp": NOW + timedelta(days=1)}, "in the future"),
        ({"currency": "GBP"}, "does not match"),
    ],
)
def test_invalid_candle_is_rejected(overrides: dict[str, Any], reason: str) -> None:
    problem = validate_candle(candle(**overrides), "USD", NOW)

    assert problem is not None and reason in problem


def test_indicator_value_validation() -> None:
    value = IndicatorValue(series_code="CPI", observed_at=NOW, value=Decimal("310.3"))
    future = value.model_copy(update={"observed_at": NOW + timedelta(days=40)})

    assert validate_indicator_value(value, NOW) is None
    assert "in the future" in (validate_indicator_value(future, NOW) or "")


# --- Ingestion service (in-memory repositories) ---------------------------------------------


class FakeInstruments:
    def __init__(self) -> None:
        self.by_symbol = {
            "AAPL": SimpleNamespace(id=uuid.uuid4(), currency="USD"),
            "US10Y": SimpleNamespace(id=uuid.uuid4(), currency=None),
        }

    async def get_instruments_by_provider_symbol(self, provider_id: uuid.UUID) -> dict[str, Any]:
        return self.by_symbol


class FakeMarketData:
    def __init__(self) -> None:
        self.observations: list[MarketObservationIn] = []
        self.candles: list[OHLCVIn] = []

    async def add_observations(self, observations: Sequence[MarketObservationIn]) -> WriteResult:
        self.observations += observations
        return WriteResult(inserted=len(observations))

    async def add_candles(self, candles: Sequence[OHLCVIn]) -> WriteResult:
        self.candles += candles
        return WriteResult(inserted=len(candles))


class FakeEconomicData:
    def __init__(self) -> None:
        self.indicator = SimpleNamespace(id=uuid.uuid4(), provider_series_code="CPIAUCSL")
        self.observations: list[EconomicObservationIn] = []
        self.ingested_at: datetime | None = None

    async def list_indicators(self, **filters: Any) -> list[Any]:
        return [self.indicator]

    async def add_observations(
        self, observations: Sequence[EconomicObservationIn], *, ingested_at: datetime | None = None
    ) -> RevisionWriteResult:
        self.observations += observations
        self.ingested_at = ingested_at
        return RevisionWriteResult(inserted=len(observations))


@pytest.fixture
def ingestion() -> SimpleNamespace:
    instruments, market_data, economic_data = (
        FakeInstruments(),
        FakeMarketData(),
        FakeEconomicData(),
    )
    service = IngestionService(
        instruments=instruments,
        market_data=market_data,
        economic_data=economic_data,
        clock=lambda: NOW,
    )
    return SimpleNamespace(
        service=service, instruments=instruments, market=market_data, economic=economic_data
    )


@pytest_anyio
async def test_quotes_are_resolved_validated_and_stored(
    ingestion: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    quotes = [
        quote(),
        quote(provider_symbol="UNTRACKED"),
        quote(currency="EUR"),
        quote(observed_at=NOW + timedelta(days=1)),
    ]

    with caplog.at_level(logging.WARNING):
        result = await ingestion.service.ingest_quotes(PROVIDER_ID, quotes)

    assert (result.received, result.written, result.rejected) == (4, 1, 3)
    (stored,) = ingestion.market.observations
    assert stored.instrument_id == ingestion.instruments.by_symbol["AAPL"].id
    assert stored.provider_id == PROVIDER_ID
    assert (stored.value, stored.currency, stored.unit) == (Decimal("189.95"), "USD", "currency")
    assert (stored.freshness, stored.quote_kind) == (Freshness.DELAYED, QuoteKind.TRADED)
    assert sorted(record.reason for record in caplog.records) == sorted(
        [
            "no enabled instrument is mapped to this symbol",
            "currency EUR does not match the instrument's USD",
            f"timestamp {(NOW + timedelta(days=1)).isoformat()} is in the future",
        ]
    )


@pytest_anyio
async def test_rejected_values_are_not_corrected(ingestion: SimpleNamespace) -> None:
    result = await ingestion.service.ingest_quotes(PROVIDER_ID, [quote(currency="EUR")])

    assert result.rejected == 1
    assert ingestion.market.observations == []


@pytest_anyio
async def test_candles_are_validated_and_stored(ingestion: SimpleNamespace) -> None:
    result = await ingestion.service.ingest_candles(
        PROVIDER_ID, [candle(), candle(high=Decimal("1")), candle(provider_symbol="UNTRACKED")]
    )

    assert (result.received, result.written, result.rejected) == (3, 1, 2)
    (stored,) = ingestion.market.candles
    assert stored.interval is CandleInterval.DAY_1
    assert stored.instrument_id == ingestion.instruments.by_symbol["AAPL"].id


@pytest_anyio
async def test_indicator_values_are_stored_with_the_ingestion_time(
    ingestion: SimpleNamespace,
) -> None:
    values = [
        IndicatorValue(series_code="CPIAUCSL", observed_at=NOW, value=Decimal("310.3")),
        IndicatorValue(series_code="UNKNOWN", observed_at=NOW, value=Decimal("1")),
    ]

    result = await ingestion.service.ingest_indicator_values(PROVIDER_ID, values)

    assert (result.received, result.written, result.rejected) == (2, 1, 1)
    assert ingestion.economic.observations[0].indicator_id == ingestion.economic.indicator.id
    assert ingestion.economic.ingested_at == NOW


@pytest_anyio
async def test_nothing_received_writes_nothing(ingestion: SimpleNamespace) -> None:
    result = await ingestion.service.ingest_quotes(PROVIDER_ID, [])

    assert result.received == 0


# --- Provider health service ---------------------------------------------------------------


class FakeHealthRepository:
    def __init__(self) -> None:
        self.events: list[tuple[Any, ...]] = []

    async def record_success(self, provider_id: uuid.UUID, at: datetime) -> None:
        self.events.append(("success", provider_id, at))

    async def record_failure(
        self, provider_id: uuid.UUID, at: datetime, error: str, *, down_after: int
    ) -> None:
        self.events.append(("failure", provider_id, at, error, down_after))


@pytest_anyio
async def test_guard_returns_the_result_and_records_success() -> None:
    repository = FakeHealthRepository()
    service = ProviderHealthService(repository, clock=lambda: NOW)

    async def operation() -> list[str]:
        return ["quote"]

    assert await service.guard(PROVIDER_ID, operation) == ["quote"]
    assert repository.events == [("success", PROVIDER_ID, NOW)]


@pytest_anyio
async def test_guard_contains_a_provider_failure(caplog: pytest.LogCaptureFixture) -> None:
    repository = FakeHealthRepository()
    service = ProviderHealthService(repository, clock=lambda: NOW)

    async def operation() -> None:
        raise ProviderUnavailableError("example", "request timed out")

    with caplog.at_level(logging.WARNING):
        result = await service.guard(PROVIDER_ID, operation)

    assert result is None
    assert repository.events == [
        (
            "failure",
            PROVIDER_ID,
            NOW,
            "ProviderUnavailableError: request timed out",
            DOWN_AFTER_FAILURES,
        )
    ]
    assert caplog.records[0].provider == "example"


@pytest_anyio
async def test_guard_does_not_hide_bugs() -> None:
    service = ProviderHealthService(FakeHealthRepository())

    async def operation() -> None:
        raise ZeroDivisionError

    with pytest.raises(ZeroDivisionError):
        await service.guard(PROVIDER_ID, operation)
