"""Query parameters shared by the read endpoints."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import Annotated

from fastapi import Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import AppError
from app.database import get_session
from app.repositories.analytics_queries import SqlAnalyticsRepository
from app.repositories.read_queries import SqlReadRepository
from app.schemas.errors import ErrorResponse

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000


@dataclass(frozen=True)
class Pagination:
    limit: int
    offset: int


def pagination(
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT, description="Items per page")] = DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0, description="Items to skip")] = 0,
) -> Pagination:
    return Pagination(limit=limit, offset=offset)


@dataclass(frozen=True)
class TimeRange:
    start: datetime
    end: datetime


def time_range(start: datetime | None, end: datetime | None, default_days: int) -> TimeRange:
    """A validated range, start inclusive and end exclusive, in UTC.

    Missing bounds default to now and to `default_days` before the end. A
    timestamp without a timezone is taken as UTC.
    """
    end = _utc(end) if end is not None else datetime.now(UTC)
    start = _utc(start) if start is not None else end - timedelta(days=default_days)
    if start >= end:
        raise AppError(
            "invalid_time_range",
            "start must be before end.",
            HTTPStatus.UNPROCESSABLE_ENTITY,
            {"start": start.isoformat(), "end": end.isoformat()},
        )
    return TimeRange(start, end)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


START = Query(
    default=None,
    description="Earliest time, inclusive (ISO 8601; UTC if no offset is given)",
)
END = Query(
    default=None,
    description="Latest time, exclusive (ISO 8601; UTC if no offset is given). Default: now",
)

ERRORS: dict[int | str, dict[str, object]] = {
    404: {"model": ErrorResponse, "description": "Not found"},
    422: {"model": ErrorResponse, "description": "Invalid parameters"},
}


def repository(session: AsyncSession = Depends(get_session)) -> SqlReadRepository:
    return SqlReadRepository(session)


Repository = Annotated[SqlReadRepository, Depends(repository)]
Paging = Annotated[Pagination, Depends(pagination)]
ProviderFilter = Annotated[str | None, Query(description="Provider name, for example `ecb`")]


def analytics_repository(session: AsyncSession = Depends(get_session)) -> SqlAnalyticsRepository:
    return SqlAnalyticsRepository(session)


Analytics = Annotated[SqlAnalyticsRepository, Depends(analytics_repository)]
