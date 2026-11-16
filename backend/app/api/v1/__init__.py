"""Version 1 of the REST API. Every v1 router is included here."""

from fastapi import APIRouter

from app.api.v1 import health

router = APIRouter(prefix="/api/v1")
router.include_router(health.router)
