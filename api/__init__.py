from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes import router as api_router
from db import DbConnectionPool
from simulation import SimulationClock
from simulation.operational import operational_simulator

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage application-level lifecycle: database connection pool initialization and cleanup."""
    now_utc = datetime.now(timezone.utc)
    with SimulationClock._lock:
        SimulationClock._base_time = now_utc
        SimulationClock._tick = 0
        SimulationClock.current_time = now_utc
    try:
        await DbConnectionPool.init_pool()
        async with DbConnectionPool.acquire() as conn:
            await operational_simulator.load_from_db(conn)
        SimulationClock.start()
    except OSError as exc:
        import logging
        logging.getLogger("srems").warning(
            "Could not connect to PostgreSQL — starting without a database: %s", exc
        )
    try:
        yield
    finally:
        SimulationClock.stop()
        await DbConnectionPool.close_pool()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application instance."""
    app_instance = FastAPI(lifespan=lifespan)
    app_instance.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app_instance.include_router(api_router, prefix="/api/v1")
    return app_instance


app = create_app()

__all__ = ["app", "create_app", "lifespan"]
