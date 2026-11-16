"""Schema of the seed file (see backend/seed.toml)."""

from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import AssetType, IndicatorFrequency
from app.schemas.provider_config import ProviderSettings


class _SeedModel(BaseModel):
    # Unknown keys are rejected so that a typo cannot be silently ignored.
    model_config = ConfigDict(extra="forbid")


class SeedExchange(_SeedModel):
    code: str
    name: str
    country: str | None = Field(default=None, min_length=2, max_length=2)
    timezone: str


class SeedProvider(_SeedModel):
    name: str
    base_url: str = Field(pattern=r"^https://")
    enabled: bool = True
    # Timeouts, retries, rate limits and authentication. Never a credential.
    configuration: ProviderSettings = Field(default_factory=ProviderSettings)


class SeedInstrument(_SeedModel):
    symbol: str
    name: str
    asset_type: AssetType
    exchange: str | None = None
    currency: str | None = None
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Provider name -> the symbol that provider uses for this instrument.
    providers: dict[str, str] = Field(default_factory=dict)


class SeedIndicator(_SeedModel):
    code: str
    name: str
    country: str | None = Field(default=None, min_length=2, max_length=2)
    unit: str
    frequency: IndicatorFrequency
    provider: str
    provider_series_code: str
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


def _duplicates(values: list[Any]) -> list[Any]:
    seen: set[Any] = set()
    repeated = []
    for value in values:
        if value in seen:
            repeated.append(value)
        seen.add(value)
    return repeated


class SeedFile(_SeedModel):
    exchanges: list[SeedExchange] = Field(default_factory=list)
    providers: list[SeedProvider] = Field(default_factory=list)
    instruments: list[SeedInstrument] = Field(default_factory=list)
    indicators: list[SeedIndicator] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_references_and_duplicates(self) -> Self:
        problems: list[str] = []

        exchange_codes = [e.code for e in self.exchanges]
        provider_names = [p.name for p in self.providers]
        instrument_keys = [(i.asset_type.value, i.symbol, i.exchange) for i in self.instruments]
        indicator_codes = [i.code for i in self.indicators]
        for label, values in (
            ("exchange", exchange_codes),
            ("provider", provider_names),
            ("instrument", instrument_keys),
            ("indicator", indicator_codes),
        ):
            problems += [f"duplicate {label}: {value}" for value in _duplicates(values)]

        for instrument in self.instruments:
            if instrument.exchange is not None and instrument.exchange not in exchange_codes:
                problems.append(
                    f"instrument {instrument.symbol}: unknown exchange {instrument.exchange}"
                )
            for provider in instrument.providers:
                if provider not in provider_names:
                    problems.append(f"instrument {instrument.symbol}: unknown provider {provider}")
        for indicator in self.indicators:
            if indicator.provider not in provider_names:
                problems.append(
                    f"indicator {indicator.code}: unknown provider {indicator.provider}"
                )

        if problems:
            raise ValueError("; ".join(problems))
        return self
