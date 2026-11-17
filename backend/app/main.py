"""FastAPI application factory.

Run with: uvicorn app.main:create_app --factory
"""

import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

import app.providers  # noqa: F401  (registers the adapters)
from app.api import metrics, v1
from app.api.errors import register_exception_handlers
from app.collectors.runner import JobRunner
from app.collectors.scheduler import CollectorScheduler
from app.config.logging import configure_logging
from app.config.settings import Settings, get_settings
from app.database import create_engine, create_session_factory
from app.monitoring.metrics import HTTP_DURATION, HTTP_REQUESTS
from app.providers.factory import ProviderFactory

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level, settings.log_format)
        engine = create_engine(settings)
        session_factory = create_session_factory(engine)
        # One provider factory per process: it owns the providers' rate limiters.
        providers = ProviderFactory(settings)
        scheduler = CollectorScheduler(
            runner=JobRunner(
                engine=engine, session_factory=session_factory, provider_factory=providers
            ),
            session_factory=session_factory,
        )
        app.state.engine = engine
        app.state.session_factory = session_factory
        app.state.scheduler = scheduler
        if settings.scheduler_enabled:
            await scheduler.start()
        logger.info(
            "Application started",
            extra={"version": settings.app_version, "scheduler": settings.scheduler_enabled},
        )
        try:
            yield
        finally:
            await scheduler.shutdown()
            await providers.aclose()
            await engine.dispose()
            logger.info("Application stopped")

    app = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=lifespan)
    app.state.settings = settings
    register_exception_handlers(app)
    app.include_router(v1.router)
    app.include_router(metrics.router)
    app.middleware("http")(_record_request)
    return app


_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})


async def _record_request(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Count and time every request by its route template, never its raw path."""
    started = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        route = _route_template(request)
        method = request.method if request.method in _METHODS else "other"
        HTTP_REQUESTS.labels(method, route, str(status)).inc()
        HTTP_DURATION.labels(method, route).observe(time.perf_counter() - started)


def _route_template(request: Request) -> str:
    """The template of the route that served the request, such as /api/v1/indicators/{code}.

    The matched route's own template is relative to the router that holds it,
    so the request path's leading segments supply the router prefixes, which
    have no parameters. Requests that matched no route share one label.
    """
    route = request.scope.get("route")
    template = getattr(route, "path_format", None) or getattr(route, "path", None)
    if not template:
        return "unmatched"
    parts = request.scope["path"].rstrip("/").split("/")
    depth = len(template.rstrip("/").split("/")) - 1
    return "/".join(parts[: max(len(parts) - depth, 1)]) + template
