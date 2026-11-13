from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from db import DbConnectionPool
from api.routes import router as api_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await DbConnectionPool.init_pool()
    try:
        yield
    finally:
        await DbConnectionPool.close_pool()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(api_router, prefix="/api/v1")

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
