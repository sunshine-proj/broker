"""FastAPI application factory.

Run with: uvicorn app.main:create_app --factory
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import v1
from app.api.errors import register_exception_handlers
from app.config.logging import configure_logging
from app.config.settings import Settings, get_settings
from app.database import create_engine, create_session_factory

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level, settings.log_format)
        engine = create_engine(settings)
        app.state.engine = engine
        app.state.session_factory = create_session_factory(engine)
        logger.info("Application started", extra={"version": settings.app_version})
        try:
            yield
        finally:
            await engine.dispose()
            logger.info("Application stopped")

    app = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=lifespan)
    register_exception_handlers(app)
    app.include_router(v1.router)
    return app
