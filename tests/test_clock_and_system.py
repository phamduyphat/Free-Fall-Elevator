from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import threading
import time

from acs.enums import DenialReason
from api import app
from simulation.clock import (
    DEFAULT_BASE_TIME,
    DEFAULT_SIM_SECONDS_PER_TICK,
    DEFAULT_TICK_INTERVAL_SECONDS,
    EVENING_PEAK_BASE_TIME,
    MORNING_PEAK_BASE_TIME,
    OFF_PEAK_BASE_TIME,
    SimulationClock,
)
from tests.conftest import AsgiClient, api_test_session, bearer, login_dashboard, seed_user_row


def test_simulation_clock_constants_and_sequential_thread_loop() -> None:
    assert DEFAULT_BASE_TIME == datetime(2026, 1, 1, 0, 0, 0)
    assert MORNING_PEAK_BASE_TIME == datetime(2026, 1, 1, 7, 0, 0)
    assert EVENING_PEAK_BASE_TIME == datetime(2026, 1, 1, 17, 0, 0)
    assert OFF_PEAK_BASE_TIME == datetime(2026, 1, 1, 22, 0, 0)
    assert DEFAULT_SIM_SECONDS_PER_TICK == 1.0
    assert DEFAULT_TICK_INTERVAL_SECONDS == 1.0

    hook_threads: list[str] = []
    tick_timestamps: list[tuple[int, float]] = []
    done_two_ticks = threading.Event()

    def slow_hook(next_time: datetime, next_tick: int) -> None:
        hook_threads.append(threading.current_thread().name)
        time.sleep(0.06)
        tick_timestamps.append((next_tick, time.monotonic()))
        if len(tick_timestamps) >= 2:
            done_two_ticks.set()

    SimulationClock.set_tick_hook(slow_hook)
    try:
        snap = SimulationClock.reset(
            base_time=MORNING_PEAK_BASE_TIME,
            sim_seconds_per_tick=2.0,
            tick_interval_seconds=0.05,
            start_loop=True,
        )
        assert snap.running is True
        assert snap.tick == 0
        assert SimulationClock.now() == MORNING_PEAK_BASE_TIME

        assert done_two_ticks.wait(timeout=2.0), "Background clock thread did not complete 2 ticks"
        SimulationClock.stop()

        # Verify tick ran in background daemon thread, never main thread
        assert all(t != threading.current_thread().name for t in hook_threads)

        # Sequential guarantee: sleep (0.05s) starts AFTER hook finishes (0.06s),
        # so delta between tick 1 finish and tick 2 finish >= 0.05 + 0.06 = 0.11s
        t1_num, t1_done = tick_timestamps[0]
        t2_num, t2_done = tick_timestamps[1]
        assert t1_num == 1 and t2_num == 2
        assert (t2_done - t1_done) >= 0.10

        after = SimulationClock.snapshot()
        assert after.running is False
        assert after.tick >= 2
        assert SimulationClock.current_time == MORNING_PEAK_BASE_TIME + timedelta(
            seconds=after.tick * 2.0
        )
    finally:
        SimulationClock.set_tick_hook(None)
        SimulationClock.stop()
        SimulationClock.reset(start_loop=False)


def test_health_db_readiness_uninitialized_and_connected() -> None:
    async def scenario() -> None:
        raw_client = AsgiClient(app)
        # Pool is not initialized -> 503 Service Unavailable
        uninit_resp = await raw_client.get("/api/v1/health/db")
        assert uninit_resp.status_code == 503
        assert uninit_resp.json()["database"] == "disconnected"

        async with api_test_session() as client:
            health_resp = await client.get("/api/v1/health")
            assert health_resp.status_code == 200
            assert health_resp.json()["status"] == "ok"

            db_resp = await client.get("/api/v1/health/db")
            assert db_resp.status_code == 200
            assert db_resp.json()["database"] == "connected"

            fleet_resp = await client.get("/api/v1/fleet/status")
            assert fleet_resp.status_code == 200
            assert len(fleet_resp.json()["elevators"]) == 5

    asyncio.run(scenario())


def test_system_reset_requires_admin_and_configures_clock() -> None:
    async def scenario() -> None:
        async with api_test_session() as client:
            # Unauthenticated reset -> 401 SESSION_INVALID with AuthorizationResult JSON
            no_auth_resp = await client.post("/api/v1/system/reset", json_body={})
            assert no_auth_resp.status_code == 401
            assert no_auth_resp.json()["allowed"] is False
            assert no_auth_resp.json()["reason_code"] == DenialReason.SESSION_INVALID

            # Seed technician & issue card via ADMIN
            admin_token = await login_dashboard(client, "ADMIN-CARD-0001")
            tech_uid = await seed_user_row(full_name="Tech Alice", role="TECHNICIAN")
            issue_resp = await client.post(
                "/api/v1/acs/credentials/manage",
                headers=bearer(admin_token),
                json_body={
                    "action": "ISSUE",
                    "target_user_id": tech_uid,
                    "target_credential_id": "TECH-CARD-01",
                },
            )
            assert issue_resp.status_code == 200

            # Technician cannot reset system -> 403 UNAUTHORIZED_ROLE
            tech_token = await login_dashboard(client, "TECH-CARD-01")
            tech_reset = await client.post(
                "/api/v1/system/reset",
                headers=bearer(tech_token),
                json_body={},
            )
            assert tech_reset.status_code == 403
            assert tech_reset.json()["reason_code"] == DenialReason.UNAUTHORIZED_ROLE

            # Admin resets system with Morning Peak T_0 and paused loop
            reset_resp = await client.post(
                "/api/v1/system/reset",
                headers=bearer(admin_token),
                json_body={
                    "base_time": MORNING_PEAK_BASE_TIME.isoformat(),
                    "sim_seconds_per_tick": 5.0,
                    "tick_interval_seconds": 0.0,
                    "start_loop": False,
                },
            )
            assert reset_resp.status_code == 200
            payload = reset_resp.json()
            assert payload["status"] == "reset"
            assert payload["clock"]["tick"] == 0
            assert payload["clock"]["sim_seconds_per_tick"] == 5.0
            assert payload["clock"]["current_sim_time"].startswith("2026-01-01T07:00:00")

            # Previous sessions were cleared on reset
            stale_tick = await client.post(
                "/api/v1/system/clock/tick",
                headers=bearer(admin_token),
                json_body={"steps": 1},
            )
            assert stale_tick.status_code == 401
            assert stale_tick.json()["reason_code"] == DenialReason.SESSION_INVALID

            # Log in again at T_0 and manually advance 3 ticks (15s)
            fresh_admin_token = await login_dashboard(client, "ADMIN-CARD-0001")
            tick_resp = await client.post(
                "/api/v1/system/clock/tick",
                headers=bearer(fresh_admin_token),
                json_body={"steps": 3},
            )
            assert tick_resp.status_code == 200
            assert tick_resp.json()["tick"] == 3
            assert tick_resp.json()["current_sim_time"].startswith("2026-01-01T07:00:15")

            clock_resp = await client.get("/api/v1/system/clock")
            assert clock_resp.status_code == 200
            assert clock_resp.json()["tick"] == 3

    asyncio.run(scenario())

