from __future__ import annotations

import asyncio
from datetime import timedelta
import json

from acs.enums import DenialReason
from simulation.operational import operational_simulator
from simulation.clock import MORNING_PEAK_BASE_TIME
from tests.conftest import advance_clock, api_test_session, bearer, login_dashboard, seed_user_row
from typing import Any

async def _run_operational_activity_via_api(
    client: Any, admin_token: str, tech_token: str
) -> dict[str, Any]:
    """Drive Mode 1/2/4 operational activity through API endpoints so Mode 5 analytics measures it."""
    # 1. Swipe Admin card in E1 at floor 1 and travel 1 -> 10 (UP trip, 9 floors traversed)
    e1_swipe = await client.post(
        "/api/v1/acs/auth/cab/swipe",
        json_body={
            "elevator_id": "E1",
            "credential_id": "ADMIN-CARD-0001",
            "origin_floor": 1,
        },
    )
    assert e1_swipe.status_code == 200
    e1_dest = await client.post(
        "/api/v1/acs/cab/destination",
        json_body={"elevator_id": "E1", "origin_floor": 1, "requested_floor": 10},
    )
    assert e1_dest.status_code == 200

    # 2. Swipe Technician card in E2 at floor 1 and travel 1 -> -1 (DOWN to B1 trip)
    e2_swipe = await client.post(
        "/api/v1/acs/auth/cab/swipe",
        json_body={
            "elevator_id": "E2",
            "credential_id": "TECH-HENRY-01",
            "origin_floor": 1,
        },
    )
    assert e2_swipe.status_code == 200
    e2_dest = await client.post(
        "/api/v1/acs/cab/destination",
        json_body={"elevator_id": "E2", "origin_floor": 1, "requested_floor": -1},
    )
    assert e2_dest.status_code == 200

    # Advance 120s to 07:02:00 (completes E1 & E2 trips and emits heartbeats every 10s)
    await advance_clock(client, admin_token, 120)

    # 3. Technician puts E3 into MAINTENANCE at 07:02:00
    maint_resp = await client.post(
        "/api/v1/acs/fleet/maintenance",
        headers=bearer(tech_token),
        json_body={
            "elevator_id": "E3",
            "operation_type": "ENTER_MAINTENANCE",
        },
    )
    assert maint_resp.status_code == 200

    # Advance 60s to 07:03:00 so E3 has 60s of maintenance downtime
    return await advance_clock(client, admin_token, 60)


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

            clock_snap = await _run_operational_activity_via_api(
                client, admin_token, tech_token
            )  # 07:03:00

            start = MORNING_PEAK_BASE_TIME.isoformat()
            end = clock_snap["current_sim_time"]

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

            # Submit a PEAK hall call at floor 4 UP (assigned to an idle car at floor 1 -> 3s wait)
            hc_resp = await client.post(
                "/api/v1/operational/hall-calls",
                json_body={
                    "origin_floor": 4,
                    "direction": "UP",
                    "call_type": "ORDINARY",
                    "traffic_profile": "PEAK",
                },
            )
            assert hc_resp.status_code == 201

            clock_snap = await _run_operational_activity_via_api(
                client, admin_token, tech_token
            )  # 07:03:00

            start = MORNING_PEAK_BASE_TIME.isoformat()
            end = clock_snap["current_sim_time"]

            wait_resp = await client.get(
                "/api/v1/analytics/wait-times",
                headers=bearer(admin_token),
                params={"start_time": start, "end_time": end, "traffic_profile": "PEAK"},
            )
            assert wait_resp.status_code == 200
            assert wait_resp.json()["total_calls"] == 1
            assert wait_resp.json()["completed_calls"] == 1
            assert wait_resp.json()["dropped_calls"] == 0
            assert wait_resp.json()["mean_wait_seconds"] == 3.0

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
            assert j_body["generated_at"] == end
            assert j_body["record_count"] == 3
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
