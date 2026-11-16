"""Collection jobs: manual trigger."""

import secrets
import uuid
from http import HTTPStatus

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel

from app.api.errors import AppError
from app.collectors.runner import JobAlreadyRunningError, JobDisabledError, JobNotFoundError
from app.models.enums import CollectorStatus, ExecutionTrigger
from app.schemas.errors import ErrorResponse

router = APIRouter(tags=["collectors"])


class RunAccepted(BaseModel):
    execution_id: uuid.UUID
    job: str
    status: CollectorStatus
    trigger: ExecutionTrigger


def _check_admin_key(request: Request, api_key: str | None) -> None:
    configured = request.app.state.settings.admin_api_key
    if configured is None or not configured.get_secret_value():
        raise AppError(
            "manual_trigger_disabled",
            "Manual triggers are disabled: ADMIN_API_KEY is not set.",
            HTTPStatus.FORBIDDEN,
        )
    if not api_key or not secrets.compare_digest(
        api_key.encode(), configured.get_secret_value().encode()
    ):
        raise AppError(
            "invalid_api_key", "Missing or invalid X-API-Key header.", HTTPStatus.UNAUTHORIZED
        )


@router.post(
    "/collectors/{job}/run",
    status_code=HTTPStatus.ACCEPTED,
    response_model=RunAccepted,
    responses={
        HTTPStatus.UNAUTHORIZED: {"model": ErrorResponse},
        HTTPStatus.FORBIDDEN: {"model": ErrorResponse},
        HTTPStatus.NOT_FOUND: {"model": ErrorResponse},
        HTTPStatus.CONFLICT: {"model": ErrorResponse},
    },
    summary="Run a collection job now",
)
async def run_job(
    job: str,
    request: Request,
    x_api_key: str | None = Header(default=None),
) -> RunAccepted:
    """Start a run of the job in the background and return its execution id.

    Requires the `X-API-Key` header to match `ADMIN_API_KEY`. A manual run
    collects every instrument of the job, also outside trading hours, and
    uses the provider's rate budget like any other run.
    """
    _check_admin_key(request, x_api_key)
    try:
        handle = await request.app.state.scheduler.trigger_manual(job)
    except JobNotFoundError:
        raise AppError(
            "job_not_found", f"No collection job named {job!r}.", HTTPStatus.NOT_FOUND
        ) from None
    except JobDisabledError:
        raise AppError(
            "job_disabled", f"Collection job {job!r} is disabled.", HTTPStatus.CONFLICT
        ) from None
    except JobAlreadyRunningError:
        raise AppError(
            "job_running", f"Collection job {job!r} is already running.", HTTPStatus.CONFLICT
        ) from None
    return RunAccepted(
        execution_id=handle.execution_id,
        job=job,
        status=CollectorStatus.RUNNING,
        trigger=ExecutionTrigger.MANUAL,
    )
