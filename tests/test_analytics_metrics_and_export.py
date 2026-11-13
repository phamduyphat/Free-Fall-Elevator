from __future__ import annotations

import asyncio
from datetime import timedelta
import json

from acs.enums import DenialReason
from db import DbConnectionPool
from simulation.clock import MORNING_PEAK_BASE_TIME, SimulationClock
from tests.conftest import api_test_session, bearer, login_dashboard, seed_user_row


async def _seed_trips_downtime_and_heartbeats() -> None:
    """Seed Mode 5 trips, downtime, and heartbeat rows anchored around MORNING_PEAK_BASE_TIME."""
    t0 = MORNING_PEAK_BASE_TIME
    async with DbConnectionPool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO trips (
                elevator_id, origin_floor, destination_floor, departure_time, arrival_time,
                travel_time_seconds, floors_traversed, motor_starts, traffic_profile,
                is_service_run, is_estimated
            )
            VALUES
                ('E1', 1, 10, $1, $2, 18.0, 9, 1, 'PEAK', FALSE, FALSE),
                ('E2', 6, -1, $2, $3, 14.0, 7, 1, 'PEAK', FALSE, FALSE);
            """,
            t0 + timedelta(seconds=45),
            t0 + timedelta(seconds=63),
            t0 + timedelta(seconds=77),
        )

        await conn.execute(
            """
            INSERT INTO car_downtime_intervals (
                elevator_id, state, reason, started_at, ended_at, duration_seconds
            )
            VALUES ('E3', 'MAINTENANCE', 'Scheduled cable check', $1, NULL, NULL);
            """,
            t0 + timedelta(seconds=120),
        )

        for sec in range(0, 181, 10):
            await conn.execute(
                """
                INSERT INTO system_heartbeats (timestamp, status, active_cars_count, active_requests_count)
                VALUES ($1, 'HEALTHY', 4, 1);
                """,
                t0 + timedelta(seconds=sec),
            )


async def _seed_hall_calls_with_traffic_profile() -> None:
    """Seed `hall_calls` rows including `traffic_profile` (requires schema matching `seed.sql`)."""
    t0 = MORNING_PEAK_BASE_TIME
    async with DbConnectionPool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO hall_calls (
                origin_floor, direction, assigned_elevator_id, status,
                created_at, assigned_at, arrived_at, completed_at, wait_time_seconds,
                reassignments_count, no_boarding, traffic_profile
            )
            VALUES
                (1, 'UP', 'E1', 'COMPLETED', $1, $2, $3, $4, 30.0, 0, FALSE, 'PEAK'),
                (5, 'DOWN', NULL, 'CANCELLED', $2, NULL, NULL, $4, NULL, 1, FALSE, 'PEAK');
            """,
            t0 + timedelta(seconds=10),
            t0 + timedelta(seconds=15),
            t0 + timedelta(seconds=40),
            t0 + timedelta(seconds=45),
        )


def test_analytics_role_gates_and_fleet_travel_security_kpis() -> None:
    async def scenario() -> None:
        async with api_test_session(base_time=MORNING_PEAK_BASE_TIME) as client:
            admin_token = await login_dashboard(client, "ADMIN-CARD-0001")
            tech_uid = await seed_user_row(full_name="Tech Henry", role="TECHNICIAN")
            await client.post(
                "/api/v1/acs/credentials/manage",
                headers=bearer(admin_token),
                json_body={
                    "action": "ISSUE",
                    "target_user_id": tech_uid,
                    "target_credential_id": "TECH-HENRY-01",
                },
            )
            tech_token = await login_dashboard(client, "TECH-HENRY-01")

            await _seed_trips_downtime_and_heartbeats()
            SimulationClock.advance(180)  # 07:03:00

            start = MORNING_PEAK_BASE_TIME.isoformat()
            end = SimulationClock.now().isoformat()

            # Technician denied VIEW_ANALYTICS and EXPORT_ANALYTICS_REPORTS -> 403 UNAUTHORIZED_ROLE
            tech_wait = await client.get(
                "/api/v1/analytics/wait-times",
                headers=bearer(tech_token),
                params={"start_time": start, "end_time": end},
            )
            assert tech_wait.status_code == 403
            assert tech_wait.json()["reason_code"] == DenialReason.UNAUTHORIZED_ROLE

            tech_export = await client.post(
                "/api/v1/analytics/export",
                headers=bearer(tech_token),
                json_body={"run_id": "run-001", "export_format": "JSON"},
            )
            assert tech_export.status_code == 403
            assert tech_export.json()["reason_code"] == DenialReason.UNAUTHORIZED_ROLE

            # Admin KPI queries not dependent on `hall_calls.traffic_profile`
            travel_resp = await client.get(
                "/api/v1/analytics/travel-times",
                headers=bearer(admin_token),
                params={"start_time": start, "end_time": end},
            )
            assert travel_resp.status_code == 200
            assert travel_resp.json()["total_trips"] == 2

            dist_resp = await client.get(
                "/api/v1/analytics/trip-distribution",
                headers=bearer(admin_token),
                params={"start_time": start, "end_time": end},
            )
            assert dist_resp.status_code == 200
            assert dist_resp.json()["up_trips_count"] == 1
            assert dist_resp.json()["down_to_b1_trips_count"] == 1

            util_resp = await client.get(
                "/api/v1/analytics/utilization",
                headers=bearer(admin_token),
                params={"start_time": start, "end_time": end},
            )
            assert util_resp.status_code == 200
            assert util_resp.json()["E1"]["floors_traversed"] == 9
            assert util_resp.json()["E3"]["maintenance_seconds"] == 60.0

            downtime_resp = await client.get(
                "/api/v1/analytics/downtime",
                headers=bearer(admin_token),
                params={"start_time": start, "end_time": end},
            )
            assert downtime_resp.status_code == 200
            assert len(downtime_resp.json()["ongoing_intervals"]) == 1
            assert downtime_resp.json()["total_downtime_seconds"] == 60.0
            assert downtime_resp.json()["per_car_downtime_seconds"]["E3"] == 60.0
            assert downtime_resp.json()["system_availability_pct"] == 100.0

            sec_resp = await client.get(
                "/api/v1/analytics/security",
                headers=bearer(admin_token),
                params={"start_time": start, "end_time": end},
            )
            assert sec_resp.status_code == 200
            assert "total_denials" in sec_resp.json()

    asyncio.run(scenario())


def test_analytics_wait_times_and_export() -> None:
    async def scenario() -> None:
        async with api_test_session(base_time=MORNING_PEAK_BASE_TIME) as client:
            admin_token = await login_dashboard(client, "ADMIN-CARD-0001")
            await _seed_trips_downtime_and_heartbeats()
            await _seed_hall_calls_with_traffic_profile()
            SimulationClock.advance(180)  # 07:03:00

            start = MORNING_PEAK_BASE_TIME.isoformat()
            end = SimulationClock.now().isoformat()

            wait_resp = await client.get(
                "/api/v1/analytics/wait-times",
                headers=bearer(admin_token),
                params={"start_time": start, "end_time": end, "traffic_profile": "PEAK"},
            )
            assert wait_resp.status_code == 200
            assert wait_resp.json()["total_calls"] == 2
            assert wait_resp.json()["completed_calls"] == 1
            assert wait_resp.json()["dropped_calls"] == 1
            assert wait_resp.json()["mean_wait_seconds"] == 30.0

            json_export = await client.post(
                "/api/v1/analytics/export",
                headers=bearer(admin_token),
                json_body={
                    "run_id": "peak-run-01",
                    "export_format": "JSON",
                    "include_per_request": True,
                    "start_time": start,
                    "end_time": end,
                },
            )
            assert json_export.status_code == 200
            j_body = json_export.json()
            assert j_body["export_format"] == "JSON"
            assert j_body["generated_at"] == SimulationClock.now().isoformat()
            assert j_body["record_count"] == 2
            parsed_content = json.loads(j_body["content"])
            assert parsed_content["run_id"] == "peak-run-01"

            csv_export = await client.post(
                "/api/v1/analytics/export",
                headers=bearer(admin_token),
                json_body={
                    "run_id": "peak-run-01",
                    "export_format": "CSV",
                    "include_per_request": True,
                    "start_time": start,
                    "end_time": end,
                },
            )
            assert csv_export.status_code == 200
            c_body = csv_export.json()
            assert c_body["export_format"] == "CSV"
            assert c_body["content"].startswith("call_id,type,origin,dest,")

            audit_resp = await client.get(
                "/api/v1/acs/logs/audit",
                headers=bearer(admin_token),
                params={"action_type": "REPORT_EXPORTED"},
            )
            assert audit_resp.status_code == 200
            assert audit_resp.json()["count"] == 2

    asyncio.run(scenario())
