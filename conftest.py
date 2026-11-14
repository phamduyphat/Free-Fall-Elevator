from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
import json
from typing import Any, AsyncIterator, Optional
from urllib.parse import urlencode
import uuid

from acs.cab_accesscontrol import CabAccessControl
from acs.dashboard_accesscontrol import DashboardAccessControl
from api import app
from db import DbConnectionPool
from simulation.operational import operational_simulator
from simulation.clock import (
    DEFAULT_BASE_TIME,
    DEFAULT_SIM_SECONDS_PER_TICK,
    SimulationClock,
)


@dataclass
class AsgiResponse:
    """Minimal HTTP response returned by `AsgiClient`."""

    status_code: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")


class AsgiClient:
    """Zero-dependency in-process ASGI client for testing FastAPI routes."""

    def __init__(self, asgi_app: Any) -> None:
        self._app = asgi_app

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Any = None,
        headers: Optional[dict[str, str]] = None,
    ) -> AsgiResponse:
        raw_headers: list[tuple[bytes, bytes]] = []
        body_bytes = b""

        if json_body is not None:
            body_bytes = json.dumps(json_body, default=str).encode("utf-8")
            raw_headers.append((b"content-type", b"application/json"))
            raw_headers.append((b"content-length", str(len(body_bytes)).encode("ascii")))

        if headers:
            for k, v in headers.items():
                raw_headers.append((k.lower().encode("latin-1"), v.encode("latin-1")))

        query_bytes = b""
        if params:
            filtered = {k: v for k, v in params.items() if v is not None}
            if filtered:
                query_bytes = urlencode(filtered, doseq=True).encode("ascii")

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method.upper(),
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": query_bytes,
            "headers": raw_headers,
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }

        request_sent = False

        async def receive() -> dict[str, Any]:
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": body_bytes, "more_body": False}
            return {"type": "http.disconnect"}

        resp_status = 500
        resp_headers: dict[str, str] = {}
        chunks: list[bytes] = []

        async def send(message: dict[str, Any]) -> None:
            nonlocal resp_status, resp_headers
            if message["type"] == "http.response.start":
                resp_status = int(message["status"])
                resp_headers = {
                    k.decode("latin-1").lower(): v.decode("latin-1")
                    for k, v in message.get("headers", [])
                }
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))

        await self._app(scope, receive, send)
        return AsgiResponse(
            status_code=resp_status,
            headers=resp_headers,
            body=b"".join(chunks),
        )

    async def get(self, path: str, **kwargs: Any) -> AsgiResponse:
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs: Any) -> AsgiResponse:
        return await self.request("POST", path, **kwargs)

    async def patch(self, path: str, **kwargs: Any) -> AsgiResponse:
        return await self.request("PATCH", path, **kwargs)


@asynccontextmanager
async def api_test_session(
    *,
    base_time: datetime = DEFAULT_BASE_TIME,
    sim_seconds_per_tick: float = DEFAULT_SIM_SECONDS_PER_TICK,
    tick_interval_seconds: Optional[float] = 0.0,
    start_loop: bool = False,
) -> AsyncIterator[AsgiClient]:
    """Initialize DB pool, reset tables/ACS state/virtual clock, yield ASGI client, and tear down."""
    await DbConnectionPool.init_pool()
    await DbConnectionPool.reset_database_tables()
    CabAccessControl.reset_in_memory_state()
    DashboardAccessControl.reset_in_memory_state()
    operational_simulator.reset()
    SimulationClock.reset(
        base_time=base_time,
        sim_seconds_per_tick=sim_seconds_per_tick,
        tick_interval_seconds=tick_interval_seconds,
        start_loop=start_loop,
    )
    try:
        yield AsgiClient(app)
    finally:
        SimulationClock.stop()
        CabAccessControl.reset_in_memory_state()
        DashboardAccessControl.reset_in_memory_state()
        operational_simulator.reset()
        await DbConnectionPool.close_pool()


async def login_dashboard(client: AsgiClient, credential_id: str) -> str:
    """Log into the dashboard via HTTP and return the opaque Bearer session token."""
    resp = await client.post(
        "/api/v1/acs/auth/dashboard/login",
        json_body={"credential_id": credential_id},
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["session_token"]
    assert token
    return token


async def seed_user_row(
    *,
    full_name: str,
    role: str,
    apartment_unit: Optional[str] = None,
    assigned_floor: Optional[int] = None,
    user_id: Optional[str] = None,
) -> str:
    """Insert a test user into PostgreSQL `users` and return `user_id`."""
    uid = user_id or str(uuid.uuid4())
    now = SimulationClock.now()
    async with DbConnectionPool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (user_id, full_name, role, apartment_unit, assigned_floor, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $6)
            """,
            uid,
            full_name,
            role,
            apartment_unit,
            assigned_floor,
            now,
        )
    return uid


def bearer(token: str) -> dict[str, str]:
    """Build an Authorization Bearer header dictionary."""
    return {"Authorization": f"Bearer {token}"}

