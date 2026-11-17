"""Data-quality issues (PRD FR-012). Read-only."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.v1.params import ERRORS, Paging, ProviderFilter, Repository, time_range
from app.models.enums import IssueKind, IssueStatus
from app.schemas.api import Page, QualityIssueOut
from app.services import read_api

router = APIRouter(tags=["data quality"])


@router.get(
    "/quality/issues",
    response_model=Page[QualityIssueOut],
    responses=ERRORS,
    summary="Data-quality issues",
)
async def quality_issues(
    repo: Repository,
    page: Paging,
    kind: IssueKind | None = None,
    status: IssueStatus | None = None,
    provider: ProviderFilter = None,
    symbol: Annotated[
        str | None, Query(description="Instrument symbol, for example `AAPL`")
    ] = None,
    start: Annotated[datetime | None, Query(description="Last seen at or after (UTC)")] = None,
    end: Annotated[datetime | None, Query(description="Last seen before (UTC)")] = None,
) -> Page[QualityIssueOut]:
    """Stale series, gaps, conflicts, spikes and provider discrepancies, most recently seen first.

    The time range applies to when an issue was last seen and defaults to the
    last 7 days. A provider filter matches either provider of a discrepancy.
    """
    period = time_range(start, end, default_days=7)
    return await read_api.quality_issues(
        repo,
        kind=kind,
        status=status,
        provider=provider,
        symbol=symbol,
        start=period.start,
        end=period.end,
        limit=page.limit,
        offset=page.offset,
    )
