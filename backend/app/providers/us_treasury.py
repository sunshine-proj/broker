"""US Department of the Treasury adapter: the Daily Treasury Par Yield Curve Rates.

Provider symbols are the field names of the Treasury's XML feed, one per
maturity, for example `BC_10YEAR` (10-year) or `BC_3MONTH` (3-month). The feed
is public, needs no key, and the data are US government works in the public
domain. API facts this adapter relies on are recorded in docs/providers/us_treasury.md.

The feed is XML (an Atom/OData document), read with the standard library's
parser. It does not resolve external entities, and the Python 3.12 expat
build refuses entity-expansion attacks.
"""

import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.models.enums import Freshness, JobKind, ObservationType, QuoteKind
from app.providers.base import Capability, Provider
from app.providers.errors import ProviderConfigurationError, ProviderResponseError
from app.providers.factory import register_adapter
from app.providers.http import ProviderHttpClient
from app.providers.parsing import parse_decimal
from app.schemas.normalized import Quote

logger = logging.getLogger(__name__)

_FEED_PATH = "/pages/xml"
_DATASET = "daily_treasury_yield_curve"
_SYMBOL = re.compile(r"^BC_\w+$")

# Business days of current values returned per collection. More than one, so
# that a missed collection day is filled in on the next run.
_LATEST_OBSERVATIONS = 5

_ATOM = "{http://www.w3.org/2005/Atom}"
_DATA = "{http://schemas.microsoft.com/ado/2007/08/dataservices}"
_META = "{http://schemas.microsoft.com/ado/2007/08/dataservices/metadata}"


@dataclass(frozen=True)
class _Day:
    day: datetime
    # Field name -> yield in percent. A maturity not quoted that day is absent.
    values: dict[str, Decimal]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _previous_month(day: date) -> date:
    return date(day.year - 1, 12, 1) if day.month == 1 else date(day.year, day.month - 1, 1)


@register_adapter
class UsTreasuryProvider(Provider):
    name = "us_treasury"
    capabilities = frozenset({Capability.QUOTES, Capability.QUOTE_HISTORY})

    def __init__(
        self, client: ProviderHttpClient, *, clock: Callable[[], datetime] = _utc_now
    ) -> None:
        super().__init__(client)
        self._clock = clock

    @classmethod
    def planned_credits(
        cls,
        kind: JobKind,
        symbols: Sequence[str],
        observation_types: Collection[ObservationType] | None = None,
    ) -> int:
        # The whole curve comes in one document: this month's and last month's.
        return 2

    async def fetch_quotes(
        self,
        symbols: Sequence[str],
        observation_types: Collection[ObservationType] | None = None,
    ) -> list[Quote]:
        """The last few published curves, from this month's and last month's feeds."""
        if observation_types is not None and ObservationType.YIELD not in observation_types:
            return []
        self._check(symbols)
        today = self._clock().date()
        days: list[_Day] = []
        for month in (_previous_month(today), today.replace(day=1)):
            days += await self._feed({"field_tdr_date_value_month": f"{month:%Y%m}"})
        days.sort(key=lambda d: d.day)
        return self._to_quotes(symbols, days[-_LATEST_OBSERVATIONS:])

    async def fetch_quote_history(self, symbol: str, start: datetime, end: datetime) -> list[Quote]:
        """Past values, one request per calendar year."""
        self._check([symbol])
        days: list[_Day] = []
        last = (end - timedelta(microseconds=1)).astimezone(UTC)
        for year in range(start.astimezone(UTC).year, last.year + 1):
            days += await self._feed({"field_tdr_date_value": str(year)})
        in_range = sorted((d for d in days if start <= d.day < end), key=lambda d: d.day)
        return self._to_quotes([symbol], in_range)

    def _to_quotes(self, symbols: Sequence[str], days: Sequence[_Day]) -> list[Quote]:
        return [
            Quote(
                provider_symbol=symbol,
                observation_type=ObservationType.YIELD,
                observed_at=day.day,
                value=day.values[symbol],
                unit="percent",
                freshness=Freshness.END_OF_DAY,
                # Par yields read from the Treasury's fitted curve, not trades.
                quote_kind=QuoteKind.REFERENCE,
            )
            for day in days
            for symbol in symbols
            if symbol in day.values
        ]

    def _check(self, symbols: Sequence[str]) -> None:
        for symbol in symbols:
            if not _SYMBOL.match(symbol):
                raise ProviderConfigurationError(
                    self.name, f"symbol {symbol!r} must be a feed field such as BC_10YEAR"
                )

    async def _feed(self, params: dict[str, str]) -> list[_Day]:
        text = await self._client.get_text(_FEED_PATH, {"data": _DATASET, **params})
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            raise ProviderResponseError(self.name, "response is not valid XML") from exc
        if root.tag != f"{_ATOM}feed":
            raise ProviderResponseError(self.name, "response is not a yield curve feed")
        try:
            return [self._day(entry) for entry in root.iter(f"{_ATOM}entry")]
        except ValueError as exc:
            raise ProviderResponseError(
                self.name, f"malformed entry in response ({type(exc).__name__})"
            ) from exc

    @staticmethod
    def _day(entry: ET.Element) -> _Day:
        properties = entry.find(f"{_ATOM}content/{_META}properties")
        if properties is None:
            raise ValueError("entry has no properties")
        when: datetime | None = None
        values: dict[str, Decimal] = {}
        for field in properties:
            name = field.tag.removeprefix(_DATA)
            text = (field.text or "").strip()
            if name == "NEW_DATE":
                day = date.fromisoformat(text[:10])
                when = datetime(day.year, day.month, day.day, tzinfo=UTC)
            elif name.startswith("BC_") and text and field.get(f"{_META}null") != "true":
                values[name] = parse_decimal(text)
        if when is None:
            raise ValueError("entry has no date")
        return _Day(when, values)
