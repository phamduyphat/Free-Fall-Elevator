from __future__ import annotations

import asyncio

from acs.enums import DenialReason
from acs.logs import Logging
from db import DbConnectionPool
from simulation.clock import MORNING_PEAK_BASE_TIME, SimulationClock
from tests.conftest import api_test_session, bearer, login_dashboard, seed_user_row


def test_credential_lifecycle_peer_admin_and_lost_card_replacement() -> None:
    async def scenario() -> None:
        async with api_test_session(base_time=MORNING_PEAK_BASE_TIME) as client:
            admin_token = await login_dashboard(client, "ADMIN-CARD-0001")

            # Peer-admin rule: Admin cannot suspend/revoke their own Admin card -> 403 PEER_ADMIN_REQUIRED
            self_suspend = await client.post(
                "/api/v1/acs/credentials/manage",
                headers=bearer(admin_token),
                json_body={
                    "action": "SUSPEND",
                    "target_credential_id": "ADMIN-CARD-0001",
                    "payload_details": {"reason": "LOST"},
                },
            )
            assert self_suspend.status_code == 403
            assert self_suspend.json()["reason_code"] == DenialReason.PEER_ADMIN_REQUIRED

            # Issue visitor pass and test duplicate ID conflict (409 CREDENTIAL_ALREADY_EXISTS)
            res_uid = await seed_user_row(
                full_name="Resident Eve", role="RESIDENT", apartment_unit="8A", assigned_floor=8
            )
            vis_uid = await seed_user_row(full_name="Visitor Frank", role="VISITOR")

            res_issue = await client.post(
                "/api/v1/acs/credentials/manage",
                headers=bearer(admin_token),
                json_body={
                    "action": "ISSUE",
                    "target_user_id": res_uid,
                    "target_credential_id": "RES-EVE-01",
                },
            )
            assert res_issue.status_code == 200

            dup_issue = await client.post(
                "/api/v1/acs/credentials/manage",
                headers=bearer(admin_token),
                json_body={
                    "action": "ISSUE",
                    "target_user_id": res_uid,
                    "target_credential_id": "RES-EVE-01",
                },
            )
            assert dup_issue.status_code == 409
            assert dup_issue.json()["reason_code"] == DenialReason.CREDENTIAL_ALREADY_EXISTS

            vis_issue = await client.post(
                "/api/v1/acs/credentials/manage",
                headers=bearer(admin_token),
                json_body={
                    "action": "ISSUE_VISITOR_PASS",
                    "target_user_id": vis_uid,
                    "target_credential_id": "VIS-FRANK-01",
                    "payload_details": {"allowed_floors": [1, 8], "duration_hours": 4},
                },
            )
            assert vis_issue.status_code == 200

            # Replace lost resident card atomically
            replace_resp = await client.post(
                "/api/v1/acs/credentials/replace-lost",
                headers=bearer(admin_token),
                json_body={
                    "lost_credential_id": "RES-EVE-01",
                    "replacement_credential_id": "RES-EVE-02",
                    "reason": "LOST_AND_REPLACED",
                },
            )
            assert replace_resp.status_code == 200
            assert replace_resp.json()["allowed"] is True

            # Swiping revoked old card raises 403 CARD_REVOKED + security alert
            old_swipe = await client.post(
                "/api/v1/acs/auth/cab/swipe",
                json_body={"elevator_id": "E1", "credential_id": "RES-EVE-01", "origin_floor": 1},
            )
            assert old_swipe.status_code == 403
            assert old_swipe.json()["reason_code"] == DenialReason.CARD_REVOKED
            assert old_swipe.json()["alert_triggered"] is True

    asyncio.run(scenario())


def test_fleet_permissions_alerts_logs_and_audit_chain_verification() -> None:
    async def scenario() -> None:
        async with api_test_session(base_time=MORNING_PEAK_BASE_TIME) as client:
            admin_token = await login_dashboard(client, "ADMIN-CARD-0001")
            tech_uid = await seed_user_row(full_name="Tech Grace", role="TECHNICIAN")
            await client.post(
                "/api/v1/acs/credentials/manage",
                headers=bearer(admin_token),
                json_body={
                    "action": "ISSUE",
                    "target_user_id": tech_uid,
                    "target_credential_id": "TECH-GRACE-01",
                },
            )
            tech_token = await login_dashboard(client, "TECH-GRACE-01")

            # Technician allowed on fleet maintenance, return-to-service, view, diagnostics, and alerts GET
            maint_resp = await client.post(
                "/api/v1/acs/fleet/maintenance",
                headers=bearer(tech_token),
                json_body={"elevator_id": "E2", "operation_type": "ENTER_MAINTENANCE"},
            )
            assert maint_resp.status_code == 200

            rts_missing_confirm = await client.post(
                "/api/v1/acs/fleet/return-to-service",
                headers=bearer(tech_token),
                json_body={"elevator_id": "E2", "technician_confirmation": False},
            )
            assert rts_missing_confirm.status_code == 400
            assert rts_missing_confirm.json()["reason_code"] == DenialReason.CONFIRMATION_REQUIRED

            rts_ok = await client.post(
                "/api/v1/acs/fleet/return-to-service",
                headers=bearer(tech_token),
                json_body={"elevator_id": "E2", "technician_confirmation": True},
            )
            assert rts_ok.status_code == 200

            assert (await client.get("/api/v1/acs/fleet/view", headers=bearer(tech_token))).status_code == 200
            assert (await client.get("/api/v1/acs/fleet/diagnostics", headers=bearer(tech_token))).status_code == 200

            # Technician denied on ADMIN-only operations (emergency override, rescue dispatch, audit logs)
            ovr_denied = await client.post(
                "/api/v1/acs/fleet/emergency-override",
                headers=bearer(tech_token),
                json_body={
                    "override_type": "MANUAL_RECALL",
                    "target_car_id": "E1",
                    "reason": "Drill",
                },
            )
            assert ovr_denied.status_code == 403
            assert ovr_denied.json()["reason_code"] == DenialReason.UNAUTHORIZED_ROLE

            audit_denied = await client.get("/api/v1/acs/logs/audit", headers=bearer(tech_token))
            assert audit_denied.status_code == 403
            assert audit_denied.json()["reason_code"] == DenialReason.UNAUTHORIZED_ROLE

            # Admin can run emergency override & rescue dispatch
            SimulationClock.advance(5)
            ovr_ok = await client.post(
                "/api/v1/acs/fleet/emergency-override",
                headers=bearer(admin_token),
                json_body={
                    "override_type": "DOOR_OVERRIDE",
                    "target_car_id": "E1",
                    "reason": "Inspection",
                },
            )
            assert ovr_ok.status_code == 200

            # Create an alert via unknown swipes / clone & inspect alerts under VIEW_DIAGNOSTICS (tech allowed)
            alert_id = await Logging.create_security_alert(
                created_at=SimulationClock.now(),
                severity="HIGH",
                alert_type="REPEATED_FAILURES",
                credential_id="UNKNOWN-999",
                primary_car_id="E1",
                secondary_car_id=None,
                details="3 failures in 60s",
                db_pool=DbConnectionPool,
            )

            tech_alerts = await client.get(
                "/api/v1/acs/logs/alerts",
                headers=bearer(tech_token),
                params={"status": "OPEN"},
            )
            assert tech_alerts.status_code == 200
            assert tech_alerts.json()["count"] >= 1

            # Alert PATCH requires ADMIN under EMERGENCY_OVERRIDE
            tech_patch = await client.patch(
                f"/api/v1/acs/logs/alerts/{alert_id}",
                headers=bearer(tech_token),
                json_body={"new_status": "ACKNOWLEDGED"},
            )
            assert tech_patch.status_code == 403

            admin_patch = await client.patch(
                f"/api/v1/acs/logs/alerts/{alert_id}",
                headers=bearer(admin_token),
                json_body={"new_status": "RESOLVED"},
            )
            assert admin_patch.status_code == 200
            assert admin_patch.json()["updated"] is True

            # Unknown alert -> 404; invalid status -> 400
            assert (
                await client.patch(
                    "/api/v1/acs/logs/alerts/missing-id",
                    headers=bearer(admin_token),
                    json_body={"new_status": "ACKNOWLEDGED"},
                )
            ).status_code == 404
            assert (
                await client.patch(
                    f"/api/v1/acs/logs/alerts/{alert_id}",
                    headers=bearer(admin_token),
                    json_body={"new_status": "INVALID_STATUS"},
                )
            ).status_code == 400

            # Time-range query on audit logs & integrity check
            window_start = MORNING_PEAK_BASE_TIME.isoformat()
            window_end = SimulationClock.now().isoformat()
            audit_list = await client.get(
                "/api/v1/acs/logs/audit",
                headers=bearer(admin_token),
                params={"start_time": window_start, "end_time": window_end, "limit": 500},
            )
            assert audit_list.status_code == 200
            assert audit_list.json()["count"] >= 4

            verify_ok = await client.post("/api/v1/acs/logs/audit/verify", headers=bearer(admin_token))
            assert verify_ok.status_code == 200
            assert verify_ok.json()["details"]["passed"] is True

            # Simulate database tampering and confirm verification detects mismatch
            async with DbConnectionPool.acquire() as conn:
                await conn.execute(
                    "UPDATE audit_log_chain SET actor_id = 'TAMPERED' WHERE log_index = 1;"
                )
            verify_tampered = await client.post(
                "/api/v1/acs/logs/audit/verify", headers=bearer(admin_token)
            )
            assert verify_tampered.status_code == 200
            assert verify_tampered.json()["details"]["passed"] is False
            assert verify_tampered.json()["details"]["mismatch_index"] == 1

    asyncio.run(scenario())
