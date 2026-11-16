"""Providers and collection: status, execution history and the manual trigger."""

import secrets
import uuid
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request
from pydantic import BaseModel

from app.api.errors import AppError
from app.api.v1.params import ERRORS, Paging, ProviderFilter, Repository
from app.collectors.runner import JobAlreadyRunningError, JobDisabledError, JobNotFoundError
from app.models.enums import CollectorStatus, ExecutionTrigger
from app.schemas.api import ExecutionOut, JobStatusOut, Page, ProviderOut
from app.schemas.errors import ErrorResponse
from app.services import read_api

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


@router.get(
    "/providers",
    response_model=Page[ProviderOut],
    responses=ERRORS,
    tags=["providers"],
    summary="Configured providers",
)
async def list_providers(repo: Repository, page: Paging) -> Page[ProviderOut]:
    """Providers with their rate limits and current health. Credentials are never shown."""
    return await read_api.list_providers(repo, limit=page.limit, offset=page.offset)


@router.get(
    "/collectors/status",
    response_model=Page[JobStatusOut],
    responses=ERRORS,
    summary="Collector status",
)
async def collector_status(
    repo: Repository,
    page: Paging,
    provider: ProviderFilter = None,
    enabled: bool | None = None,
) -> Page[JobStatusOut]:
    """Every collection job with its schedule, last run, outcome and next run (UTC)."""
    return await read_api.job_status(
        repo, provider=provider, enabled=enabled, limit=page.limit, offset=page.offset
    )


@router.get(
    "/collectors/executions",
    response_model=Page[ExecutionOut],
    responses=ERRORS,
    summary="Collector execution history",
)
async def collector_executions(
    repo: Repository,
    page: Paging,
    job: Annotated[str | None, Query(description="Job name")] = None,
    status: CollectorStatus | None = None,
) -> Page[ExecutionOut]:
    """Runs of the collection jobs, newest first."""
    return await read_api.executions(
        repo, job=job, status=status, limit=page.limit, offset=page.offset
    )
