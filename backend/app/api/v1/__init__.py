"""Version 1 of the REST API. Every v1 router is included here.

Everything but the health check needs a signed-in user (PRD FR-017).
"""

from fastapi import APIRouter, Depends

from app.api.auth import me_router, require_user
from app.api.v1 import analytics, collectors, health, market, quality

router = APIRouter(prefix="/api/v1")
router.include_router(health.router)

_protected = [Depends(require_user)]
router.include_router(me_router)
for _module in (market, collectors, quality, analytics):
    router.include_router(_module.router, dependencies=_protected)
