"""GET /metrics: Prometheus metrics (PRD FR-013).

Outside /api/v1: it serves Prometheus, not API clients. It contains no
credentials. The process metrics come from this process; the platform state
is read from the database at each scrape, with a time limit, and the database
is reported down if that fails.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, Response
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.monitoring import database_metrics

logger = logging.getLogger(__name__)

router = APIRouter()

# Longest a scrape waits for the database.
DATABASE_TIMEOUT_SECONDS = 10


@router.get("/metrics", include_in_schema=False)
async def metrics(session: AsyncSession = Depends(get_session)) -> Response:
    snapshot = None
    try:
        snapshot = await asyncio.wait_for(
            database_metrics.read_snapshot(session), DATABASE_TIMEOUT_SECONDS
        )
    except (SQLAlchemyError, OSError, TimeoutError) as exc:
        logger.warning("Metrics could not read the database", extra={"error": str(exc)})
    body = generate_latest(REGISTRY) + database_metrics.render(snapshot)
    return Response(content=body, media_type=CONTENT_TYPE_LATEST)
