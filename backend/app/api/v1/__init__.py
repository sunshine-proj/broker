"""Version 1 of the REST API. Every v1 router is included here."""

from fastapi import APIRouter

from app.api.v1 import collectors, health, market

router = APIRouter(prefix="/api/v1")
router.include_router(health.router)
router.include_router(market.router)
router.include_router(collectors.router)
