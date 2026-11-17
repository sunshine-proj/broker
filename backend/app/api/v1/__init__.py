"""Version 1 of the REST API. Every v1 router is included here."""

from fastapi import APIRouter

from app.api.v1 import analytics, collectors, health, market, quality

router = APIRouter(prefix="/api/v1")
router.include_router(health.router)
router.include_router(market.router)
router.include_router(collectors.router)
router.include_router(quality.router)
router.include_router(analytics.router)
