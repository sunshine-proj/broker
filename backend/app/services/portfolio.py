"""Portfolio tracking: holdings from the private portfolio file (PRD FR-016).

The file is the source of truth: loading it replaces every stored portfolio,
and a portfolio removed from the file, or a missing file, removes it. Holdings
are logged as counts only; their contents are private.

Format (see portfolio/portfolio.example.toml):

    [[portfolios]]
    name = "main"
    base_currency = "EUR"

    [[portfolios.holdings]]
    symbol = "AAPL"
    provider = "twelve_data"
    quantity = 10
    cost_basis = 1500.00          # total cost
    cost_currency = "USD"         # default: the base currency
    acquired_on = 2026-01-15      # optional; valued from this day on
    note = "..."                  # optional
"""

import logging
import tomllib
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.models.enums import AssetType
from app.repositories.interfaces import HoldingRecord, PortfolioRecord, PortfolioRepository

logger = logging.getLogger(__name__)

# What a holding can be: values in a currency that can be owned.
HOLDABLE = (AssetType.STOCK, AssetType.ETF, AssetType.CRYPTO, AssetType.COMMODITY)


class PortfolioError(Exception):
    pass


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HoldingEntry(_Model):
    symbol: str
    provider: str
    quantity: Decimal = Field(gt=0)
    cost_basis: Decimal = Field(ge=0)
    cost_currency: str | None = Field(default=None, pattern=r"^[A-Z0-9]{3,10}$")
    acquired_on: date | None = None
    note: str | None = None


class PortfolioEntry(_Model):
    name: str = Field(pattern=r"^[a-z0-9_]+$", max_length=128)
    base_currency: str = Field(pattern=r"^[A-Z]{3}$")
    holdings: list[HoldingEntry] = Field(default_factory=list)


class PortfolioFile(_Model):
    portfolios: list[PortfolioEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> Self:
        names = [p.name for p in self.portfolios]
        if len(names) != len(set(names)):
            raise ValueError("portfolio names must be unique")
        return self


def load_portfolio_file(path: Path) -> PortfolioFile:
    """The parsed file; an absent file means no portfolios."""
    if not path.exists():
        return PortfolioFile()
    try:
        data = tomllib.loads(path.read_text(), parse_float=Decimal)
        return PortfolioFile.model_validate(data)
    except tomllib.TOMLDecodeError as exc:
        raise PortfolioError(f"{path}: not valid TOML: {exc}") from exc
    except ValidationError as exc:
        # Locations and messages only: the input values are private.
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        raise PortfolioError(f"{path}: {problems}") from exc


async def apply_portfolios(repository: PortfolioRepository, portfolios: PortfolioFile) -> int:
    """Replace the stored portfolios with the file's; returns the number of holdings.

    Raises PortfolioError, before writing anything, if a holding names an
    unknown instrument, a provider that does not price it, or an instrument
    that cannot be held. The caller commits.
    """
    targets = await repository.holding_targets()
    by_key = {(t.symbol, t.provider): t for t in targets}
    symbols = {t.symbol for t in targets}

    problems = []
    records = []
    for portfolio in portfolios.portfolios:
        holdings = []
        for number, holding in enumerate(portfolio.holdings, start=1):
            where = f"portfolio {portfolio.name}, holding {number}"
            target = by_key.get((holding.symbol, holding.provider))
            if holding.symbol not in symbols:
                problems.append(f"{where}: unknown instrument {holding.symbol}")
            elif target is None:
                problems.append(f"{where}: {holding.symbol} has no provider {holding.provider}")
            elif target.asset_type not in HOLDABLE:
                problems.append(f"{where}: {holding.symbol} cannot be held ({target.asset_type})")
            else:
                holdings.append(
                    HoldingRecord(
                        instrument_id=target.instrument_id,
                        provider_id=target.provider_id,
                        quantity=holding.quantity,
                        cost_basis=holding.cost_basis,
                        cost_currency=holding.cost_currency or portfolio.base_currency,
                        acquired_on=holding.acquired_on,
                        note=holding.note,
                    )
                )
        records.append(PortfolioRecord(portfolio.name, portfolio.base_currency, holdings))
    if problems:
        raise PortfolioError("; ".join(problems))

    await repository.replace_all(records)
    count = sum(len(record.holdings) for record in records)
    logger.info("Portfolios loaded", extra={"portfolios": len(records), "holdings": count})
    return count
