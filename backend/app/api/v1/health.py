import logging
from http import HTTPStatus

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.schemas.health import HealthResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


async def _database_is_available(session: AsyncSession) -> bool:
    try:
        await session.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError, TimeoutError) as exc:
        logger.warning("Database health check failed", extra={"error": str(exc)})
        return False
    return True


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={HTTPStatus.SERVICE_UNAVAILABLE: {"model": HealthResponse}},
    summary="Service health",
)
async def health(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> HealthResponse:
    """Report whether the service and its database are usable.

    Returns 503 with the same body when the database is unreachable.
    """
    database_ok = await _database_is_available(session)
    if not database_ok:
        response.status_code = HTTPStatus.SERVICE_UNAVAILABLE
    return HealthResponse(
        status="ok" if database_ok else "degraded",
        version=request.app.version,
        database="ok" if database_ok else "unavailable",
    )
