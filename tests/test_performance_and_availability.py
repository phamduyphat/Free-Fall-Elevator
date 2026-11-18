"""Database-backed tests for AVL-1, PERF-3, PERF-5, and PERF-8..PERF-10 (VT-23)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from acs.logs import Logging
from analytics.fleet_metrics import FleetMetrics
from analytics.traffic_metrics import TrafficMetrics
from api import create_app, lifespan
from db import DbConnectionPool
from simulation.operational import (
    DEFAULT_BACKLOG_LIMIT,
    classify_traffic_profile,
    operational_simulator,
)
from simulation.traffic_profiles import (
    OFF_PEAK_PROFILE,
    PEAK_PROFILE,
    run_traffic_profile,
)
from tests.conftest import api_test_session, bearer, login_dashboard


def test_avl1_lifespan_emits_startup_and_shutdown_audit_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify lifespan(app) emits SYSTEM_STARTUP and SYSTEM_SHUTDOWN to audit_log_chain."""

    async def _run() -> None:
        async with api_test_session() as client:
            monkeypatch.delenv("SREMS_ANNOUNCED_MAINTENANCE", raising=False)
            app = create_app()
            async with lifespan(app):
                startup_logs = await Logging.search_audit_logs(
                    action_type="SYSTEM_STARTUP",
                    db_pool=DbConnectionPool,
                )
                assert len(startup_logs) >= 1
                assert startup_logs[0]["action_type"] == "SYSTEM_STARTUP"

            # lifespan() closes DbConnectionPool on exit; re-open before querying or tearing down.
            await DbConnectionPool.init_pool()
            shutdown_logs = await Logging.search_audit_logs(
                action_type="SYSTEM_SHUTDOWN",
                db_pool=DbConnectionPool,
            )
            assert len(shutdown_logs) >= 1
            assert shutdown_logs[0]["event_payload"].get("is_announced_maintenance") is False

            # Re-open and close with SREMS_ANNOUNCED_MAINTENANCE=true
            monkeypatch.setenv("SREMS_ANNOUNCED_MAINTENANCE", "true")
            async with lifespan(app):
                pass

            await DbConnectionPool.init_pool()
            announced_logs = await Logging.search_audit_logs(
                action_type="SYSTEM_SHUTDOWN",
                db_pool=DbConnectionPool,
            )
            assert any(
                log["event_payload"].get("is_announced_maintenance") is True
                and log["event_payload"].get("reason") == "ANNOUNCED_MAINTENANCE"
                for log in announced_logs
            )
            integrity = await Logging.verify_audit_chain(db_pool=DbConnectionPool)
            assert integrity.passed is True
            assert client is not None

    asyncio.run(_run())


def test_avl1_idle_clock_fast_forward_keeps_availability_target(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify idle step_from_clock(120) emits 10s heartbeats so FleetMetrics reports >=99.5%."""
    base_time = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)

    async def _run() -> None:
        async with api_test_session(base_time=base_time) as _client:
            app = create_app()
            async with lifespan(app):
                operational_simulator.step_from_clock(120)
                async with DbConnectionPool.acquire() as conn:
                    await operational_simulator.sync_to_db(conn)

                start_time = base_time - timedelta(seconds=5)
                end_time = base_time + timedelta(seconds=125)
                report = await FleetMetrics.get_downtime_summary(
                    start_time=start_time,
                    end_time=end_time,
                    report_time=end_time,
                    db_pool=DbConnectionPool,
                )
                with capsys.disabled():
                    print(
                        f"\n[AVL-1] availability={report.system_availability_pct:.2f}% "
                        f"unannounced_downtime={report.unannounced_downtime_seconds:.1f}s "
                        f"announced_maintenance={report.announced_maintenance_seconds:.1f}s "
                        f"target_met={report.meets_availability_target}"
                    )
                assert report.unannounced_downtime_seconds == 0.0
                assert report.system_availability_pct >= 99.5
                assert report.meets_availability_target is True

            # lifespan() closes DbConnectionPool on exit; re-open for api_test_session teardown.
            await DbConnectionPool.init_pool()

    asyncio.run(_run())


def test_perf5_backlog_exposed_without_discarding_requests(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify >100 active requests are all persisted in DB and exposed in API responses."""

    async def _run() -> None:
        async with api_test_session() as client:
            admin_token = await login_dashboard(client, "ADMIN-CARD-0001")

            # Submit 90 distinct hall calls (60 ORDINARY + 30 FREIGHT with direction=None)
            ordinary_combos = [(-1, "UP"), (30, "DOWN")] + [
                (floor, direction)
                for floor in range(1, 30)
                for direction in ("UP", "DOWN")
            ]
            hall_ids: list[str] = []
            for floor, direction in ordinary_combos:
                resp = await client.post(
                    "/api/v1/operational/hall-calls",
                    json_body={
                        "origin_floor": floor,
                        "direction": direction,
                        "call_type": "ORDINARY",
                    },
                )
                assert resp.status_code == 201
                payload = resp.json()
                assert payload["merged"] is False
                hall_ids.append(payload["request"]["request_id"])

            for floor in range(1, 31):
                resp = await client.post(
                    "/api/v1/operational/hall-calls",
                    json_body={
                        "origin_floor": floor,
                        "direction": None,
                        "call_type": "FREIGHT",
                    },
                )
                assert resp.status_code == 201
                payload = resp.json()
                assert payload["merged"] is False
                hall_ids.append(payload["request"]["request_id"])

            # Submit 15 distinct car calls via ACS cab swipe + destination (total = 105 active)
            car_ids: list[str] = []
            for dest_floor in range(2, 17):
                swipe_resp = await client.post(
                    "/api/v1/acs/auth/cab/swipe",
                    json_body={
                        "elevator_id": "E1",
                        "credential_id": "ADMIN-CARD-0001",
                        "origin_floor": 1,
                    },
                )
                assert swipe_resp.status_code == 200
                dest_resp = await client.post(
                    "/api/v1/acs/cab/destination",
                    json_body={
                        "elevator_id": "E1",
                        "origin_floor": 1,
                        "requested_floor": dest_floor,
                    },
                )
                assert dest_resp.status_code == 200
                
                car_ids.append(dest_resp.json()["details"]["operational_request_id"])

            assert len(hall_ids) == 90
            assert len(car_ids) == 15

            async with DbConnectionPool.acquire() as conn:
                await operational_simulator.sync_to_db(conn)
                hall_db_count = await conn.fetchval("SELECT COUNT(*) FROM hall_calls")
                car_db_count = await conn.fetchval("SELECT COUNT(*) FROM car_calls")
                assert hall_db_count == 90
                assert car_db_count == 15

            summary_resp = await client.get(
                "/api/v1/operational/summary",
                headers=bearer(admin_token),
            )
            assert summary_resp.status_code == 200
            summary = summary_resp.json()
            assert summary["active_requests"] == 105
            assert summary["backlog_limit"] == DEFAULT_BACKLOG_LIMIT
            assert summary["backlog_exceeded"] is True
            assert summary["backlog_count"] == 5

            fleet_resp = await client.get(
                "/api/v1/fleet/status",
                headers=bearer(admin_token),
            )
            assert fleet_resp.status_code == 200
            fleet = fleet_resp.json()
            with capsys.disabled():
                print(
                    f"\n[PERF-5] hall_calls_db={hall_db_count} car_calls_db={car_db_count} "
                    f"active_requests={summary['active_requests']} backlog_limit={summary['backlog_limit']} "
                    f"backlog_exceeded={summary['backlog_exceeded']} backlog_count={summary['backlog_count']}"
                )
            assert fleet["active_requests"] == 105
            assert fleet["backlog_limit"] == DEFAULT_BACKLOG_LIMIT
            assert fleet["backlog_exceeded"] is True
            assert fleet["backlog_count"] == 5

    asyncio.run(_run())


def test_perf8_to_perf10_traffic_profile_classification_and_vt23_targets(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify automatic profile classification, dispatch parity, and VT-23 wait KPIs in DB."""
    # Classification boundaries
    assert classify_traffic_profile(datetime(2026, 6, 1, 7, 30, tzinfo=timezone.utc)) == "PEAK"
    assert classify_traffic_profile(datetime(2026, 6, 1, 18, 0, tzinfo=timezone.utc)) == "PEAK"
    assert classify_traffic_profile(datetime(2026, 6, 1, 23, 30, tzinfo=timezone.utc)) == "OFF_PEAK"
    assert classify_traffic_profile(datetime(2026, 6, 1, 3, 0, tzinfo=timezone.utc)) == "OFF_PEAK"
    assert classify_traffic_profile(datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)) == "NORMAL"

    # Dispatch parity: identical elevator selection for 07:30 (PEAK) vs 23:30 (OFF_PEAK)
    peak_ts = datetime(2026, 6, 1, 7, 30, tzinfo=timezone.utc)
    off_peak_ts = datetime(2026, 6, 1, 23, 30, tzinfo=timezone.utc)

    operational_simulator.reset(base_time=peak_ts)
    peak_call, _ = operational_simulator.submit_hall_call(origin_floor=5, direction="UP")
    operational_simulator.reset(base_time=off_peak_ts)
    off_peak_call, _ = operational_simulator.submit_hall_call(origin_floor=5, direction="UP")
    assert peak_call["assigned_car"] is not None
    assert peak_call["assigned_car"] == off_peak_call["assigned_car"]
    assert peak_call["traffic_profile"] == "PEAK"
    assert off_peak_call["traffic_profile"] == "OFF_PEAK"

    async def _run() -> None:
        # VT-23 Peak profile against PostgreSQL
        async with api_test_session(base_time=PEAK_PROFILE.base_time) as _client:
            peak_ids = run_traffic_profile(operational_simulator, PEAK_PROFILE, reset_sim=True)
            assert len(peak_ids) > 0
            async with DbConnectionPool.acquire() as conn:
                await operational_simulator.sync_to_db(conn)

            peak_report = await TrafficMetrics.get_wait_time_kpis(
                start_time=PEAK_PROFILE.base_time - timedelta(seconds=1),
                end_time=PEAK_PROFILE.base_time
                + timedelta(seconds=PEAK_PROFILE.duration_seconds + 1),
                traffic_profile="PEAK",
                db_pool=DbConnectionPool,
            )
            with capsys.disabled():
                print(
                    f"\n[VT-23 PEAK] total={peak_report.total_calls} completed={peak_report.completed_calls} "
                    f"mean_wait={peak_report.mean_wait_seconds:.2f}s p95_wait={peak_report.p95_wait_seconds:.2f}s "
                    f"target_met={peak_report.target_met}"
                )
            assert peak_report.total_calls == len(peak_ids)
            assert peak_report.completed_calls == len(peak_ids)
            assert peak_report.mean_wait_seconds <= 60.0
            assert peak_report.p95_wait_seconds <= 120.0
            assert peak_report.target_met is True

        # VT-23 Off-Peak profile against PostgreSQL
        async with api_test_session(base_time=OFF_PEAK_PROFILE.base_time) as _client:
            off_peak_ids = run_traffic_profile(
                operational_simulator, OFF_PEAK_PROFILE, reset_sim=True
            )
            assert len(off_peak_ids) > 0
            async with DbConnectionPool.acquire() as conn:
                await operational_simulator.sync_to_db(conn)

            off_peak_report = await TrafficMetrics.get_wait_time_kpis(
                start_time=OFF_PEAK_PROFILE.base_time - timedelta(seconds=1),
                end_time=OFF_PEAK_PROFILE.base_time
                + timedelta(seconds=OFF_PEAK_PROFILE.duration_seconds + 1),
                traffic_profile="OFF_PEAK",
                db_pool=DbConnectionPool,
            )
            with capsys.disabled():
                print(
                    f"\n[VT-23 OFF_PEAK] total={off_peak_report.total_calls} completed={off_peak_report.completed_calls} "
                    f"mean_wait={off_peak_report.mean_wait_seconds:.2f}s p95_wait={off_peak_report.p95_wait_seconds:.2f}s "
                    f"target_met={off_peak_report.target_met}"
                )
            assert off_peak_report.total_calls == len(off_peak_ids)
            assert off_peak_report.completed_calls == len(off_peak_ids)
            assert off_peak_report.mean_wait_seconds <= 90.0
            assert off_peak_report.target_met is True

    asyncio.run(_run())
