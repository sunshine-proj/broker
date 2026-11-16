"""FastAPI application factory.

Run with: uvicorn app.main:create_app --factory
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

import app.providers  # noqa: F401  (registers the adapters)
from app.api import v1
from app.api.errors import register_exception_handlers
from app.collectors.runner import JobRunner
from app.collectors.scheduler import CollectorScheduler
from app.config.logging import configure_logging
from app.config.settings import Settings, get_settings
from app.database import create_engine, create_session_factory
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
    return app
