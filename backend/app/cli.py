"""Administrative commands.

Usage:
    python -m app.cli seed [--file PATH]
    python -m app.cli search PROVIDER QUERY
    python -m app.cli collect quotes PROVIDER [--types price,market_cap,...]
    python -m app.cli collect candles PROVIDER [--interval 1day] [--days 30]
    python -m app.cli collect history PROVIDER [--days 30]
"""

import argparse
import asyncio
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

import app.providers  # noqa: F401  (registers the adapters)
from app.config.logging import configure_logging
from app.config.settings import Settings, get_settings
from app.database import create_engine, create_session_factory
from app.models import Provider as ProviderRow
from app.models.enums import CandleInterval, ObservationType
from app.providers.errors import ProviderError
from app.providers.factory import ProviderFactory
from app.repositories.economic_data import SqlEconomicDataRepository
from app.repositories.instruments import SqlInstrumentRepository
from app.repositories.market_data import SqlMarketDataRepository
from app.repositories.provider_health import SqlProviderHealthRepository
from app.repositories.providers import SqlProviderRepository
from app.services.collection import CollectionOutcome, CollectionService
from app.services.ingestion import IngestionService
from app.services.provider_health import ProviderHealthService
from app.services.seed import SeedError, apply_seed, load_seed_file

logger = logging.getLogger(__name__)


class CommandError(Exception):
    """A command could not run; the message is shown to the operator."""


@dataclass(frozen=True)
class CollectRequest:
    # quotes, candles or history
    kind: str
    provider: str
    interval: CandleInterval = CandleInterval.DAY_1
    days: int = 30
    # Observation types to fetch with `quotes`; None means all.
    types: frozenset[ObservationType] | None = None


@asynccontextmanager
async def _session(settings: Settings) -> AsyncIterator[AsyncSession]:
    engine = create_engine(settings)
    try:
        async with create_session_factory(engine)() as session:
            yield session
    finally:
        await engine.dispose()


async def _provider(session: AsyncSession, name: str) -> ProviderRow:
    provider = await SqlProviderRepository(session).get_by_name(name)
    if provider is None:
        raise CommandError(f"Unknown provider: {name}")
    return provider


async def seed(settings: Settings, path: Path) -> None:
    seed_file = load_seed_file(path)
    async with _session(settings) as session:
        await apply_seed(
            seed_file,
            providers=SqlProviderRepository(session),
            instruments=SqlInstrumentRepository(session),
            economic_data=SqlEconomicDataRepository(session),
        )
        await session.commit()


async def search(settings: Settings, provider_name: str, query: str) -> None:
    """Print the provider's symbols matching `query`, to help fill in the seed file."""
    factory = ProviderFactory(settings)
    try:
        async with _session(settings) as session:
            provider = await _provider(session, provider_name)
        try:
            matches = await factory.build(provider).search_symbols(query)
        except ProviderError as exc:
            raise CommandError(str(exc)) from exc
    finally:
        await factory.aclose()

    print(f"{'SYMBOL':<12} {'EXCHANGE':<8} {'CURRENCY':<8} {'TYPE':<22} NAME")
    for match in matches:
        print(
            f"{match.provider_symbol:<12} {match.exchange or '-':<8} {match.currency or '-':<8} "
            f"{match.instrument_type or '-':<22} {match.name}"
        )


async def collect(settings: Settings, request: CollectRequest) -> CollectionOutcome:
    """Run one collection for a provider and store the result."""
    factory = ProviderFactory(settings)
    try:
        async with _session(settings) as session:
            provider = await _provider(session, request.provider)
            instruments = SqlInstrumentRepository(session)
            service = CollectionService(
                factory=factory,
                instruments=instruments,
                ingestion=IngestionService(
                    instruments=instruments,
                    market_data=SqlMarketDataRepository(session),
                    economic_data=SqlEconomicDataRepository(session),
                ),
                health=ProviderHealthService(SqlProviderHealthRepository(session)),
            )
            end = datetime.now(UTC)
            start = end - timedelta(days=request.days)
            if request.kind == "quotes":
                outcome = await service.collect_quotes(provider, request.types)
            elif request.kind == "candles":
                outcome = await service.collect_candles(provider, request.interval, start, end)
            else:
                outcome = await service.collect_quote_history(provider, start, end)
            # Committed on failure too: the provider's health record must be kept.
            await session.commit()
            return outcome
    finally:
        await factory.aclose()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    commands = parser.add_subparsers(dest="command", required=True)

    seed_parser = commands.add_parser("seed", help="apply the seed file to the database")
    seed_parser.add_argument("--file", type=Path, help="seed file (default: SEED_FILE setting)")

    search_parser = commands.add_parser("search", help="search a provider's symbols")
    search_parser.add_argument("provider")
    search_parser.add_argument("query")

    collect_parser = commands.add_parser("collect", help="collect data from a provider once")
    kinds = collect_parser.add_subparsers(dest="kind", required=True)
    quotes_parser = kinds.add_parser("quotes", help="latest values of the mapped instruments")
    quotes_parser.add_argument("provider")
    quotes_parser.add_argument(
        "--types",
        type=_observation_types,
        help="comma-separated observation types to fetch (default: all the provider offers)",
    )
    candles_parser = kinds.add_parser("candles", help="closed candles of the mapped instruments")
    candles_parser.add_argument("provider")
    candles_parser.add_argument(
        "--interval",
        type=CandleInterval,
        choices=list(CandleInterval),
        default=CandleInterval.DAY_1,
        help="candle interval (default: 1day)",
    )
    candles_parser.add_argument(
        "--days", type=int, default=30, help="how many days back to fetch (default: 30)"
    )
    history_parser = kinds.add_parser("history", help="past values of the mapped instruments")
    history_parser.add_argument("provider")
    history_parser.add_argument(
        "--days", type=int, default=30, help="how many days back to fetch (default: 30)"
    )
    return parser


def _observation_types(value: str) -> frozenset[ObservationType]:
    try:
        return frozenset(ObservationType(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        allowed = ", ".join(t.value for t in ObservationType)
        raise argparse.ArgumentTypeError(f"unknown observation type; use: {allowed}") from exc


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    try:
        if args.command == "seed":
            asyncio.run(seed(settings, args.file or settings.seed_file))
        elif args.command == "search":
            asyncio.run(search(settings, args.provider, args.query))
        else:
            request = CollectRequest(
                kind=args.kind,
                provider=args.provider,
                interval=getattr(args, "interval", CandleInterval.DAY_1),
                days=getattr(args, "days", 30),
                types=getattr(args, "types", None),
            )
            outcome = asyncio.run(collect(settings, request))
            # A provider failure is already logged and recorded; report it in the exit code.
            return 0 if outcome.succeeded else 2
    except (SeedError, CommandError) as exc:
        logger.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
