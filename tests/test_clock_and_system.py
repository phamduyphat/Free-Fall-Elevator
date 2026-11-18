from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import threading
import time

from acs.enums import (
    AuditActionType,
    CredentialAction,
    CredentialStatus,
    SuspensionReason,
    UserRole,
)
from api import app
from db import DbConnectionPool
from simulation.clock import (
    DEFAULT_BASE_TIME,
    DEFAULT_SIM_SECONDS_PER_TICK,
    DEFAULT_TICK_INTERVAL_SECONDS,
    EVENING_PEAK_BASE_TIME,
    MORNING_PEAK_BASE_TIME,
    OFF_PEAK_BASE_TIME,
    SimulationClock,
)
from tests.conftest import (
    AsgiClient,
    advance_clock,
    api_test_session,
    bearer,
    login_dashboard,
    seed_user_row,
)


def test_simulation_clock_constants_and_sequential_thread_loop() -> None:
    assert DEFAULT_BASE_TIME == datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    assert MORNING_PEAK_BASE_TIME == datetime(2026, 1, 1, 7, 0, 0, tzinfo=timezone.utc)
    assert EVENING_PEAK_BASE_TIME == datetime(2026, 1, 1, 17, 0, 0, tzinfo=timezone.utc)
    assert OFF_PEAK_BASE_TIME == datetime(2026, 1, 1, 22, 0, 0, tzinfo=timezone.utc)
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


def test_bulk_visitor_pass_expiry_in_clock_advance_and_loop() -> None:
    async def scenario() -> None:
        async with api_test_session(
            base_time=MORNING_PEAK_BASE_TIME,
            sim_seconds_per_tick=1800.0,
            tick_interval_seconds=0.0,
            start_loop=False,
        ) as client:
            admin_token = await login_dashboard(client, "ADMIN-CARD-0001")
            vis_uid = await seed_user_row(full_name="Bulk Visitor", role=UserRole.VISITOR)

            for cid in ("VIS-BULK-1", "VIS-BULK-2"):
                assert (
                    await client.post(
                        "/api/v1/acs/credentials/manage",
                        headers=bearer(admin_token),
                        json_body={
                            "action": CredentialAction.ISSUE_VISITOR_PASS.value,
                            "target_user_id": vis_uid,
                            "target_credential_id": cid,
                            "payload_details": {"duration_seconds": 3600},
                        },
                    )
                ).status_code == 200

            hook_saw_conn: list[bool] = []

            def check_hook(_next_time: datetime, _next_tick: int) -> None:
                hook_saw_conn.append(SimulationClock._loop_db_conn is not None)

            SimulationClock.set_tick_hook(check_hook)
            try:
                # Tick 1 (+1800s = 30m): midway, visitor passes still active
                await advance_clock(client, admin_token, 1)
                assert hook_saw_conn == [True]
                assert SimulationClock._loop_db_conn is None

                async with DbConnectionPool.acquire() as conn:
                    rows = await conn.fetch(
                        "SELECT credential_id, status FROM credentials WHERE credential_id IN ('VIS-BULK-1', 'VIS-BULK-2')"
                    )
                    assert {r["credential_id"]: r["status"] for r in rows} == {
                        "VIS-BULK-1": CredentialStatus.ACTIVE,
                        "VIS-BULK-2": CredentialStatus.ACTIVE,
                    }

                # Tick 2 (+1800s = 1h total): both visitor passes expire via bulk SQL IN transition
                admin_token = await login_dashboard(client, "ADMIN-CARD-0001")
                await advance_clock(client, admin_token, 1)
                assert hook_saw_conn == [True, True]
                assert SimulationClock._loop_db_conn is None

                admin_token = await login_dashboard(client, "ADMIN-CARD-0001")
                async with DbConnectionPool.acquire() as conn:
                    rows = await conn.fetch(
                        "SELECT credential_id, status FROM credentials WHERE credential_id IN ('VIS-BULK-1', 'VIS-BULK-2')"
                    )
                    assert {r["credential_id"]: r["status"] for r in rows} == {
                        "VIS-BULK-1": CredentialStatus.EXPIRED,
                        "VIS-BULK-2": CredentialStatus.EXPIRED,
                    }

                for cid in ("VIS-BULK-1", "VIS-BULK-2"):
                    audit_resp = await client.get(
                        "/api/v1/acs/logs/audit",
                        headers=bearer(admin_token),
                        params={
                            "target_id": cid,
                            "action_type": AuditActionType.VISITOR_PASS_EXPIRED.value,
                        },
                    )
                    assert audit_resp.status_code == 200
                    assert audit_resp.json()["count"] >= 1

                verify_resp = await client.post(
                    "/api/v1/acs/logs/audit/verify",
                    headers=bearer(admin_token),
                )
                assert verify_resp.status_code == 200
                assert verify_resp.json()["details"]["passed"] is True
            finally:
                SimulationClock.set_tick_hook(None)

    asyncio.run(scenario())


def test_bulk_lost_card_auto_revoke_in_clock_advance_and_loop() -> None:
    async def scenario() -> None:
        async with api_test_session(
            base_time=MORNING_PEAK_BASE_TIME,
            sim_seconds_per_tick=36 * 3600.0,
            tick_interval_seconds=0.0,
            start_loop=False,
        ) as client:
            admin_token = await login_dashboard(client, "ADMIN-CARD-0001")
            res_uid = await seed_user_row(
                full_name="Bulk Resident", role=UserRole.RESIDENT, assigned_floor=4
            )

            for cid in ("RES-LOST-1", "RES-LOST-2"):
                assert (
                    await client.post(
                        "/api/v1/acs/credentials/manage",
                        headers=bearer(admin_token),
                        json_body={
                            "action": CredentialAction.ISSUE.value,
                            "target_user_id": res_uid,
                            "target_credential_id": cid,
                        },
                    )
                ).status_code == 200
                assert (
                    await client.post(
                        "/api/v1/acs/credentials/manage",
                        headers=bearer(admin_token),
                        json_body={
                            "action": CredentialAction.SUSPEND.value,
                            "target_credential_id": cid,
                            "payload_details": {"suspension_reason": SuspensionReason.LOST.value},
                        },
                    )
                ).status_code == 200

            hook_saw_conn: list[bool] = []

            def check_hook(_next_time: datetime, _next_tick: int) -> None:
                hook_saw_conn.append(SimulationClock._loop_db_conn is not None)

            SimulationClock.set_tick_hook(check_hook)
            try:
                # Tick 1 (+36h): midway through 72h wait period, cards remain SUSPENDED
                await advance_clock(client, admin_token, 1)
                assert hook_saw_conn == [True]
                assert SimulationClock._loop_db_conn is None

                async with DbConnectionPool.acquire() as conn:
                    rows = await conn.fetch(
                        "SELECT credential_id, status FROM credentials WHERE credential_id IN ('RES-LOST-1', 'RES-LOST-2')"
                    )
                    assert {r["credential_id"]: r["status"] for r in rows} == {
                        "RES-LOST-1": CredentialStatus.SUSPENDED,
                        "RES-LOST-2": CredentialStatus.SUSPENDED,
                    }

                # Tick 2 (+36h = 72h total): both LOST cards auto-revoke via bulk SQL IN transition
                admin_token = await login_dashboard(client, "ADMIN-CARD-0001")
                await advance_clock(client, admin_token, 1)
                assert hook_saw_conn == [True, True]
                assert SimulationClock._loop_db_conn is None

                admin_token = await login_dashboard(client, "ADMIN-CARD-0001")
                async with DbConnectionPool.acquire() as conn:
                    rows = await conn.fetch(
                        "SELECT credential_id, status, revocation_reason FROM credentials WHERE credential_id IN ('RES-LOST-1', 'RES-LOST-2')"
                    )
                    assert {
                        r["credential_id"]: (r["status"], r["revocation_reason"]) for r in rows
                    } == {
                        "RES-LOST-1": (CredentialStatus.REVOKED, "WINDOW_EXPIRED"),
                        "RES-LOST-2": (CredentialStatus.REVOKED, "WINDOW_EXPIRED"),
                    }

                for cid in ("RES-LOST-1", "RES-LOST-2"):
                    audit_resp = await client.get(
                        "/api/v1/acs/logs/audit",
                        headers=bearer(admin_token),
                        params={
                            "target_id": cid,
                            "action_type": AuditActionType.CREDENTIAL_REVOKED.value,
                        },
                    )
                    assert audit_resp.status_code == 200
                    assert audit_resp.json()["count"] >= 1

                verify_resp = await client.post(
                    "/api/v1/acs/logs/audit/verify",
                    headers=bearer(admin_token),
                )
                assert verify_resp.status_code == 200
                assert verify_resp.json()["details"]["passed"] is True
            finally:
                SimulationClock.set_tick_hook(None)

    asyncio.run(scenario())


def test_operational_simulator_loads_synced_state_from_db_on_init() -> None:
    from simulation.operational import OperationalSimulator

    async def scenario() -> None:
        async with api_test_session(base_time=MORNING_PEAK_BASE_TIME) as client:
            admin_token = await login_dashboard(client, "ADMIN-CARD-0001")
            hc_resp = await client.post(
                "/api/v1/operational/hall-calls",
                json_body={"origin_floor": 6, "direction": "UP", "call_type": "ORDINARY"},
            )
            assert hc_resp.status_code == 201
            call_id = hc_resp.json()["request"]["request_id"]

            await advance_clock(client, admin_token, 10)
            maint_resp = await client.post(
                "/api/v1/acs/fleet/maintenance",
                headers=bearer(admin_token),
                json_body={"elevator_id": "E4", "operation_type": "ENTER_MAINTENANCE"},
            )
            assert maint_resp.status_code == 200
            await advance_clock(client, admin_token, 10)

            # With DbConnectionPool active, creating a new OperationalSimulator auto-loads DB state
            # while initializing base_time to current UTC wall time (sim_tick = 0).
            from datetime import timezone
            before_init = datetime.now(timezone.utc)
            reloaded = OperationalSimulator(auto_advance=False)
            after_init = datetime.now(timezone.utc)
            assert call_id in reloaded.hall_calls
            assert reloaded.hall_calls[call_id].status == "COMPLETED"
            assert reloaded.cars["E1"].current_floor == 6
            assert reloaded.cars["E4"].availability == "MAINTENANCE"
            assert reloaded.sim_tick == 0
            assert before_init <= reloaded.base_time <= after_init
            assert len(reloaded.trips) >= 1
            assert len(reloaded.downtime_intervals) == 1

    asyncio.run(scenario())

