"""The private portfolio file and its loading (PRD FR-016). No database."""

import uuid
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

import pytest

from app.models.enums import AssetType
from app.repositories.interfaces import HoldingTarget, PortfolioRecord
from app.services.portfolio import (
    PortfolioError,
    PortfolioFile,
    apply_portfolios,
    load_portfolio_file,
)

pytestmark = pytest.mark.anyio

EXAMPLE = Path(__file__).resolve().parents[2] / "portfolio" / "portfolio.example.toml"

FILE = """
[[portfolios]]
name = "main"
base_currency = "EUR"

[[portfolios.holdings]]
symbol = "AAPL"
provider = "alpha"
quantity = 10
cost_basis = 1500.10
cost_currency = "USD"
acquired_on = 2026-01-15

[[portfolios.holdings]]
symbol = "BTC/USD"
provider = "alpha"
quantity = 0.123456789
cost_basis = 4000
"""

AAPL, BTC, US10Y, ALPHA = (uuid.uuid4() for _ in range(4))
TARGETS = [
    HoldingTarget("AAPL", "alpha", AAPL, ALPHA, AssetType.STOCK),
    HoldingTarget("BTC/USD", "alpha", BTC, ALPHA, AssetType.CRYPTO),
    HoldingTarget("US10Y", "alpha", US10Y, ALPHA, AssetType.GOVERNMENT_BOND_YIELD),
]


class FakeRepository:
    def __init__(self) -> None:
        self.stored: list[PortfolioRecord] | None = None

    async def holding_targets(self) -> Sequence[HoldingTarget]:
        return TARGETS

    async def replace_all(self, portfolios: Sequence[PortfolioRecord]) -> None:
        self.stored = list(portfolios)


def write(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "portfolio.toml"
    path.write_text(content)
    return path


def test_file_is_parsed_with_exact_decimals(tmp_path: Path) -> None:
    portfolios = load_portfolio_file(write(tmp_path, FILE))

    (main,) = portfolios.portfolios
    assert main.base_currency == "EUR"
    assert main.holdings[0].cost_basis == Decimal("1500.10")
    assert main.holdings[1].quantity == Decimal("0.123456789")


def test_a_missing_file_means_no_portfolios(tmp_path: Path) -> None:
    assert load_portfolio_file(tmp_path / "absent.toml").portfolios == []


def test_the_shipped_example_is_valid() -> None:
    assert load_portfolio_file(EXAMPLE).portfolios[0].name == "example"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (("quantity = 10", "quantity = 0"), "quantity"),
        (("cost_basis = 4000", "cost_basis = -1"), "cost_basis"),
        (('base_currency = "EUR"', 'base_currency = "euro"'), "base_currency"),
        (('provider = "alpha"\nquantity = 10', 'provider = "alpha"\nqty = 10'), "qty"),
    ],
)
def test_invalid_files_are_rejected_without_their_values(
    tmp_path: Path, change: tuple[str, str], message: str
) -> None:
    old, new = change
    assert old in FILE

    with pytest.raises(PortfolioError, match=message) as raised:
        load_portfolio_file(write(tmp_path, FILE.replace(old, new, 1)))
    # Holdings are private: errors name the field, not what was in it.
    assert "1500.10" not in str(raised.value)


def test_duplicate_portfolios_and_bad_toml_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(PortfolioError, match="unique"):
        load_portfolio_file(
            write(tmp_path, FILE + '\n[[portfolios]]\nname = "main"\nbase_currency = "USD"\n')
        )
    with pytest.raises(PortfolioError, match="not valid TOML"):
        load_portfolio_file(write(tmp_path, "[[portfolios]\n"))


async def test_holdings_are_resolved_and_stored(tmp_path: Path) -> None:
    repository = FakeRepository()

    count = await apply_portfolios(repository, load_portfolio_file(write(tmp_path, FILE)))

    assert count == 2
    assert repository.stored is not None
    (main,) = repository.stored
    aapl, btc = main.holdings
    assert (aapl.instrument_id, aapl.provider_id, aapl.cost_currency) == (AAPL, ALPHA, "USD")
    # The cost is in the base currency unless stated.
    assert (btc.instrument_id, btc.cost_currency, btc.acquired_on) == (BTC, "EUR", None)


@pytest.mark.parametrize(
    ("symbol", "provider", "message"),
    [
        ("MSFT", "alpha", "holding 1: unknown instrument MSFT"),
        ("AAPL", "beta", "holding 1: AAPL has no provider beta"),
        ("US10Y", "alpha", "holding 1: US10Y cannot be held"),
    ],
)
async def test_unresolvable_holdings_store_nothing(
    symbol: str, provider: str, message: str
) -> None:
    repository = FakeRepository()
    portfolios = PortfolioFile.model_validate(
        {
            "portfolios": [
                {
                    "name": "main",
                    "base_currency": "EUR",
                    "holdings": [
                        {"symbol": symbol, "provider": provider, "quantity": 1, "cost_basis": 1}
                    ],
                }
            ]
        }
    )

    with pytest.raises(PortfolioError, match=message):
        await apply_portfolios(repository, portfolios)
    assert repository.stored is None


async def test_an_empty_file_removes_every_portfolio() -> None:
    repository = FakeRepository()

    assert await apply_portfolios(repository, PortfolioFile()) == 0
    assert repository.stored == []
