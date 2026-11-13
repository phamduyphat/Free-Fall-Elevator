from __future__ import annotations

from fastapi import APIRouter

from api.analytics.export import router as export_router
from api.analytics.metrics import router as metrics_router

router = APIRouter(prefix="/analytics")
router.include_router(metrics_router)
router.include_router(export_router)

__all__ = ["router"]

