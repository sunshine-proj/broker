from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config.settings import Settings
from app.models.enums import AssetType, IndicatorFrequency
from app.schemas.market_data import MarketObservationIn
from app.services.seed import SeedError, load_seed_file

VALID_SEED = """
[[exchanges]]
code = "XNAS"
name = "Nasdaq Stock Market"
country = "US"
timezone = "America/New_York"

[[providers]]
name = "example"
base_url = "https://api.example.test"
configuration = { max_retries = 2, rate_limits = [{ limit = 8, period = "minute" }] }

[[instruments]]
symbol = "AAPL"
name = "Apple Inc."
asset_type = "stock"
exchange = "XNAS"
currency = "USD"
providers = { example = "AAPL.US" }

[[instruments]]
symbol = "EUR/USD"
name = "Euro / US Dollar"
asset_type = "forex"
currency = "USD"

[[indicators]]
code = "US_CPI"
name = "US Consumer Price Index"
country = "US"
unit = "index"
frequency = "monthly"
provider = "example"
provider_series_code = "CPIAUCSL"
"""


def write(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "seed.toml"
    path.write_text(content)
    return path


def test_valid_seed_file_is_parsed(tmp_path: Path) -> None:
    seed = load_seed_file(write(tmp_path, VALID_SEED))

    assert [e.code for e in seed.exchanges] == ["XNAS"]
    configuration = seed.providers[0].configuration
    assert configuration.max_retries == 2
    assert configuration.rate_limits[0].limit == 8
    apple, eur_usd = seed.instruments
    assert apple.asset_type is AssetType.STOCK
    assert apple.providers == {"example": "AAPL.US"}
    assert eur_usd.exchange is None
    assert eur_usd.enabled is True
    assert seed.indicators[0].frequency is IndicatorFrequency.MONTHLY


def test_empty_seed_file_is_valid(tmp_path: Path) -> None:
    seed = load_seed_file(write(tmp_path, "# nothing tracked yet\n"))

    assert seed.instruments == []


def test_shipped_seed_file_is_valid(settings: Settings) -> None:
    load_seed_file(settings.seed_file)


def test_missing_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(SeedError, match="not found"):
        load_seed_file(tmp_path / "absent.toml")


def test_malformed_toml_is_reported(tmp_path: Path) -> None:
    with pytest.raises(SeedError, match="not valid TOML"):
        load_seed_file(write(tmp_path, "[[instruments]\n"))


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (('exchange = "XNAS"', 'exchange = "XLON"'), "unknown exchange XLON"),
        (
            ('providers = { example = "AAPL.US" }', 'providers = { other = "A" }'),
            "unknown provider",
        ),
        (('provider = "example"', 'provider = "other"'), "unknown provider other"),
        (('asset_type = "stock"', 'asset_type = "share"'), "asset_type"),
        (('currency = "USD"\nproviders', 'curency = "USD"\nproviders'), "curency"),
        (('symbol = "EUR/USD"', 'symbol = "EUR/USD"\napi_key = "x"'), "api_key"),
    ],
)
def test_invalid_content_is_rejected(tmp_path: Path, change: tuple[str, str], message: str) -> None:
    old, new = change
    assert old in VALID_SEED

    with pytest.raises(SeedError, match=message):
        load_seed_file(write(tmp_path, VALID_SEED.replace(old, new)))


def test_duplicate_instrument_is_rejected(tmp_path: Path) -> None:
    duplicate = """
[[instruments]]
symbol = "EUR/USD"
name = "Euro / US Dollar again"
asset_type = "forex"
"""
    with pytest.raises(SeedError, match="duplicate instrument"):
        load_seed_file(write(tmp_path, VALID_SEED + duplicate))


OBSERVATION = {
    "instrument_id": "00000000-0000-0000-0000-000000000001",
    "provider_id": "00000000-0000-0000-0000-000000000002",
    "observation_type": "price",
    "value": "101.25",
    "currency": "USD",
    "unit": "currency",
    "freshness": "delayed",
    "quote_kind": "traded",
}


def test_observation_rejects_naive_timestamp() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        MarketObservationIn(**OBSERVATION, observed_at="2026-11-16T10:00:00")


def test_observation_timestamp_is_converted_to_utc() -> None:
    observation = MarketObservationIn(**OBSERVATION, observed_at="2026-11-16T10:00:00+02:00")

    assert observation.observed_at.isoformat() == "2026-11-16T08:00:00+00:00"


def test_observation_value_keeps_decimal_precision() -> None:
    observation = MarketObservationIn(
        **{**OBSERVATION, "value": "0.000000012345678901"}, observed_at="2026-11-16T10:00:00Z"
    )

    assert observation.value == Decimal("0.000000012345678901")
