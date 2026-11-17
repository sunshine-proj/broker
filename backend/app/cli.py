"""Administrative commands.

Usage:
    python -m app.cli seed [--file PATH]
    python -m app.cli portfolio [--file PATH]
    python -m app.cli search PROVIDER QUERY
    python -m app.cli collect quotes PROVIDER [--types price,market_cap,...]
    python -m app.cli collect candles PROVIDER [--interval 1day] [--days 30]
    python -m app.cli collect history PROVIDER [--days 30]
    python -m app.cli collect indicators PROVIDER [--days 730]
    python -m app.cli jobs
    python -m app.cli run JOB
    python -m app.cli reader-role
    python -m app.cli app-role
    python -m app.cli storage-policies
    python -m app.cli user add NAME [--admin] [--password-stdin]
    python -m app.cli user password NAME [--password-stdin]
    python -m app.cli user role NAME viewer|admin
    python -m app.cli user enable|disable NAME
    python -m app.cli user list
    python -m app.cli token create NAME TOKEN_NAME [--days N]
    python -m app.cli token revoke NAME TOKEN_NAME
    python -m app.cli token list
    python -m app.cli prune-sessions
"""

import argparse
import asyncio
import getpass
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

import app.providers  # noqa: F401  (registers the adapters)
from app.collectors.runner import (
    JobAlreadyRunningError,
    JobDisabledError,
    JobNotFoundError,
    JobRunner,
    RunResult,
)
from app.config.logging import configure_logging
from app.config.settings import Settings, get_settings
from app.database import create_engine, create_session_factory
from app.models import Provider as ProviderRow
from app.models.enums import (
    CandleInterval,
    CollectorStatus,
    ExecutionTrigger,
    ObservationType,
    UserRole,
)
from app.providers.errors import ProviderError
from app.providers.factory import ProviderFactory
from app.repositories.auth import SqlAuthRepository
from app.repositories.collection_jobs import SqlCollectionJobRepository
from app.repositories.economic_data import SqlEconomicDataRepository
from app.repositories.instruments import SqlInstrumentRepository
from app.repositories.lists import SqlListRepository
from app.repositories.market_data import SqlMarketDataRepository
from app.repositories.portfolios import SqlPortfolioRepository
from app.repositories.provider_health import SqlProviderHealthRepository
from app.repositories.providers import SqlProviderRepository
from app.repositories.quality import SqlQualityRepository
from app.services.auth import AuthError, AuthService
from app.services.collection import CollectionOutcome, CollectionService
from app.services.ingestion import IngestionService
from app.services.portfolio import PortfolioError, apply_portfolios, load_portfolio_file
from app.services.provider_health import ProviderHealthService
from app.services.reader_role import ReaderRoleError, ensure_app_role, ensure_reader_role
from app.services.seed import SeedError, apply_seed, load_seed_file
from app.services.storage_policies import StoragePolicies, apply_storage_policies

logger = logging.getLogger(__name__)


class CommandError(Exception):
    """A command could not run; the message is shown to the operator."""


@dataclass(frozen=True)
class CollectRequest:
    # quotes, candles, history or indicators
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
            jobs=SqlCollectionJobRepository(session),
            lists=SqlListRepository(session),
        )
        await session.commit()


async def portfolio(settings: Settings, path: Path) -> None:
    """Replace the stored portfolios with the portfolio file's (PRD FR-016)."""
    portfolios = load_portfolio_file(path)
    if not path.exists():
        logger.info("No portfolio file; stored portfolios removed", extra={"path": str(path)})
    async with _session(settings) as session:
        await apply_portfolios(SqlPortfolioRepository(session), portfolios)
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
            economic_data = SqlEconomicDataRepository(session)
            service = CollectionService(
                factory=factory,
                instruments=instruments,
                economic_data=economic_data,
                ingestion=IngestionService(
                    instruments=instruments,
                    market_data=SqlMarketDataRepository(session),
                    economic_data=economic_data,
                    quality=SqlQualityRepository(session),
                ),
                health=ProviderHealthService(SqlProviderHealthRepository(session)),
            )
            end = datetime.now(UTC)
            start = end - timedelta(days=request.days)
            if request.kind == "quotes":
                outcome = await service.collect_quotes(provider, request.types)
            elif request.kind == "candles":
                outcome = await service.collect_candles(provider, request.interval, start, end)
            elif request.kind == "history":
                outcome = await service.collect_quote_history(provider, start, end)
            else:
                outcome = await service.collect_indicators(provider, start, end)
            # Committed on failure too: the provider's health record must be kept.
            await session.commit()
            return outcome
    finally:
        await factory.aclose()


async def list_jobs(settings: Settings) -> None:
    """Print every collection job with its schedule and last outcome."""
    async with _session(settings) as session:
        jobs = await SqlCollectionJobRepository(session).list()

    def when(value: datetime | None) -> str:
        return value.strftime("%Y-%m-%d %H:%M") if value else "-"

    print(f"{'JOB':<30} {'ON':<3} {'SCHEDULE':<14} {'LAST RUN':<17} {'STATUS':<8} {'NEXT RUN':<17}")
    for job in jobs:
        schedule = (
            f"daily {job.daily_at.strftime('%H:%M')}"
            if job.daily_at
            else f"every {job.interval_minutes}m"
        )
        if job.market_hours_only:
            schedule += "*"
        print(
            f"{job.name:<30} {'yes' if job.enabled else 'no':<3} {schedule:<14} "
            f"{when(job.last_started_at):<17} {(job.last_status or '-'):<8} "
            f"{when(job.next_run_at):<17}"
        )
        if job.last_error:
            print(f"    last error: {job.last_error}")
    print("\nTimes in UTC. * = trading hours only. NEXT RUN is kept by the running scheduler.")


async def run_job(settings: Settings, job_name: str) -> RunResult:
    """Run a job now, as a manual run, and wait for it to finish."""
    engine = create_engine(settings)
    factory = ProviderFactory(settings)
    runner = JobRunner(
        engine=engine,
        session_factory=create_session_factory(engine),
        provider_factory=factory,
    )
    try:
        result = await runner.run(job_name, ExecutionTrigger.MANUAL)
    except JobNotFoundError as exc:
        raise CommandError(f"Unknown job: {job_name}") from exc
    except JobDisabledError as exc:
        raise CommandError(f"Job {job_name} is disabled") from exc
    except JobAlreadyRunningError as exc:
        raise CommandError(f"Job {job_name} is already running") from exc
    finally:
        await factory.aclose()
        await engine.dispose()
    assert result is not None  # manual runs are never skipped
    return result


async def reader_role(settings: Settings) -> None:
    """Create or update Grafana's read-only role and grant it read access."""
    if settings.grafana_db_password is None:
        raise CommandError("GRAFANA_DB_PASSWORD is not set")
    await _ensure_role(
        settings,
        ensure_reader_role,
        settings.grafana_db_user,
        settings.grafana_db_password.get_secret_value(),
    )


async def app_role(settings: Settings) -> None:
    """Create or update the backend's role: read, insert and update."""
    if settings.backend_db_password is None:
        raise CommandError("BACKEND_DB_PASSWORD is not set")
    await _ensure_role(
        settings,
        ensure_app_role,
        settings.backend_db_user,
        settings.backend_db_password.get_secret_value(),
    )


async def _ensure_role(settings: Settings, ensure: Any, name: str, password: str) -> None:
    engine = create_engine(settings)
    try:
        async with engine.begin() as connection:
            await ensure(connection, name, password)
    except ReaderRoleError as exc:
        raise CommandError(str(exc)) from exc
    finally:
        await engine.dispose()


async def storage_policies(settings: Settings) -> None:
    """Apply the compression and retention policies from the environment."""
    engine = create_engine(settings)
    try:
        async with engine.begin() as connection:
            await apply_storage_policies(
                connection,
                StoragePolicies(
                    compress_after_days=settings.compress_after_days,
                    observation_retention_days=settings.observation_retention_days,
                    candle_retention_days=settings.candle_retention_days,
                ),
            )
    finally:
        await engine.dispose()


def read_password(from_stdin: bool) -> str:
    """A new password: one line of standard input, or typed twice at a prompt."""
    if from_stdin:
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass("New password: ")
    if getpass.getpass("Repeat it: ") != first:
        raise CommandError("The passwords differ.")
    return first


async def users(settings: Settings, args: argparse.Namespace) -> None:
    """Accounts and API tokens (PRD FR-017). Secrets are never logged."""
    async with _session(settings) as session:
        repository = SqlAuthRepository(session)
        service = AuthService(repository)
        action = args.action
        if args.command == "user" and action == "add":
            role = UserRole.ADMIN if args.admin else UserRole.VIEWER
            await service.create_user(args.name, read_password(args.password_stdin), role)
        elif args.command == "user" and action == "password":
            await service.set_password(args.name, read_password(args.password_stdin))
        elif args.command == "user" and action == "role":
            await service.set_role(args.name, UserRole(args.role))
        elif args.command == "user" and action in ("enable", "disable"):
            await service.set_enabled(args.name, action == "enable")
        elif args.command == "user":
            print(f"{'USER':<24} {'ROLE':<7} {'ON':<3} {'LOCKED UNTIL':<17} LAST LOGIN")
            for user in await repository.list_users():
                enabled = "yes" if user.enabled else "no"
                print(
                    f"{user.username:<24} {user.role.value:<7} {enabled:<3} "
                    f"{_when(user.locked_until):<17} {_when(user.last_login_at)}"
                )
            return
        elif action == "create":
            token = await service.issue_token(args.name, args.token_name, args.days)
            # Shown once, on standard output only.
            print(token)
        elif action == "revoke":
            await service.revoke_token(args.name, args.token_name)
        else:
            print(f"{'USER':<24} {'TOKEN':<24} {'CREATED':<17} {'EXPIRES':<17} REVOKED")
            for token in await repository.list_tokens():
                print(
                    f"{token['username']:<24} {token['name']:<24} {_when(token['created_at']):<17} "
                    f"{_when(token['expires_at']):<17} {_when(token['revoked_at'])}"
                )
            return
        await session.commit()


async def prune_sessions(settings: Settings) -> None:
    """Delete expired and revoked sessions; needs the owner's rights."""
    async with _session(settings) as session:
        deleted = await SqlAuthRepository(session).prune_sessions(datetime.now(UTC))
        await session.commit()
    logger.info("Old sessions deleted", extra={"count": deleted})


def _when(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "-"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    commands = parser.add_subparsers(dest="command", required=True)

    seed_parser = commands.add_parser("seed", help="apply the seed file to the database")
    seed_parser.add_argument("--file", type=Path, help="seed file (default: SEED_FILE setting)")

    portfolio_parser = commands.add_parser(
        "portfolio", help="load the private portfolio file into the database"
    )
    portfolio_parser.add_argument(
        "--file", type=Path, help="portfolio file (default: PORTFOLIO_FILE setting)"
    )

    search_parser = commands.add_parser("search", help="search a provider's symbols")
    search_parser.add_argument("provider")
    search_parser.add_argument("query")

    commands.add_parser("jobs", help="list collection jobs and their status")
    commands.add_parser("reader-role", help="set up the read-only database role for Grafana")
    commands.add_parser("app-role", help="set up the backend's database role")
    commands.add_parser("storage-policies", help="apply the compression and retention policies")
    commands.add_parser("prune-sessions", help="delete expired and revoked sign-in sessions")

    user_parser = commands.add_parser("user", help="manage user accounts")
    user_actions = user_parser.add_subparsers(dest="action", required=True)
    for action, text in (("add", "create an account"), ("password", "set a new password")):
        action_parser = user_actions.add_parser(action, help=text)
        action_parser.add_argument("name")
        action_parser.add_argument(
            "--password-stdin", action="store_true", help="read the password from standard input"
        )
        if action == "add":
            action_parser.add_argument("--admin", action="store_true", help="role admin")
    role_parser = user_actions.add_parser("role", help="change an account's role")
    role_parser.add_argument("name")
    role_parser.add_argument("role", choices=[r.value for r in UserRole])
    for action in ("enable", "disable"):
        user_actions.add_parser(action, help=f"{action} an account").add_argument("name")
    user_actions.add_parser("list", help="list accounts")

    token_parser = commands.add_parser("token", help="manage personal API tokens")
    token_actions = token_parser.add_subparsers(dest="action", required=True)
    create_parser = token_actions.add_parser("create", help="issue a token and print it once")
    create_parser.add_argument("name", help="the account")
    create_parser.add_argument("token_name", help="what the token is for, e.g. laptop")
    create_parser.add_argument("--days", type=int, help="expire after this many days")
    revoke_parser = token_actions.add_parser("revoke", help="revoke a token")
    revoke_parser.add_argument("name")
    revoke_parser.add_argument("token_name")
    token_actions.add_parser("list", help="list tokens (never their values)")

    run_parser = commands.add_parser("run", help="run a collection job now")
    run_parser.add_argument("job")

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
    indicators_parser = kinds.add_parser("indicators", help="economic indicator values")
    indicators_parser.add_argument("provider")
    indicators_parser.add_argument(
        "--days",
        type=int,
        default=730,
        help="how many days back to fetch (default: 730, enough for quarterly and annual series)",
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
        elif args.command == "portfolio":
            asyncio.run(portfolio(settings, args.file or settings.portfolio_file))
        elif args.command == "search":
            asyncio.run(search(settings, args.provider, args.query))
        elif args.command == "jobs":
            asyncio.run(list_jobs(settings))
        elif args.command == "reader-role":
            asyncio.run(reader_role(settings))
        elif args.command == "app-role":
            asyncio.run(app_role(settings))
        elif args.command == "storage-policies":
            asyncio.run(storage_policies(settings))
        elif args.command in ("user", "token"):
            asyncio.run(users(settings, args))
        elif args.command == "prune-sessions":
            asyncio.run(prune_sessions(settings))
        elif args.command == "run":
            result = asyncio.run(run_job(settings, args.job))
            return 0 if result.status is CollectorStatus.SUCCESS else 2
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
    except (SeedError, PortfolioError, AuthError, CommandError) as exc:
        logger.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
