from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
import uvicorn

from db import DbConnectionPool


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await DbConnectionPool.init_pool()
    try:
        yield
    finally:
        await DbConnectionPool.close_pool()


app = FastAPI(lifespan=lifespan)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
