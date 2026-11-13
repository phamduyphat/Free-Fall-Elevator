from __future__ import annotations

from fastapi import APIRouter

from api.acs.auth import router as auth_router
from api.acs.cab import router as cab_router
from api.acs.credentials import router as credentials_router
from api.acs.fleet import router as fleet_router
from api.acs.logs import router as logs_router

router = APIRouter(prefix="/acs")
router.include_router(auth_router)
router.include_router(cab_router)
router.include_router(credentials_router)
router.include_router(fleet_router)
router.include_router(logs_router)

__all__ = ["router"]

