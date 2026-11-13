from __future__ import annotations

import asyncio

from acs.enums import DenialReason
from simulation.clock import MORNING_PEAK_BASE_TIME, SimulationClock
from tests.conftest import api_test_session, bearer, login_dashboard, seed_user_row


def test_dashboard_auth_opaque_token_sliding_ttl_and_logout() -> None:
    async def scenario() -> None:
        async with api_test_session(base_time=MORNING_PEAK_BASE_TIME) as client:
            # Admin logs in; opaque Bearer token must NOT embed card ID
            login_resp = await client.post(
                "/api/v1/acs/auth/dashboard/login",
                json_body={"credential_id": "ADMIN-CARD-0001"},
            )
            assert login_resp.status_code == 200
            token = login_resp.json()["session_token"]
            assert token and "ADMIN-CARD-0001" not in token

            # Resident card denied dashboard login -> 403 DASHBOARD_ACCESS_DENIED
            res_uid = await seed_user_row(
                full_name="Resident Bob",
                role="RESIDENT",
                apartment_unit="12A",
                assigned_floor=12,
            )
            issue_res = await client.post(
                "/api/v1/acs/credentials/manage",
                headers=bearer(token),
                json_body={
                    "action": "ISSUE",
                    "target_user_id": res_uid,
                    "target_credential_id": "RES-CARD-12",
                },
            )
            assert issue_res.status_code == 200

            denied_login = await client.post(
                "/api/v1/acs/auth/dashboard/login",
                json_body={"credential_id": "RES-CARD-12"},
            )
            assert denied_login.status_code == 403
            assert denied_login.json()["allowed"] is False
            assert denied_login.json()["reason_code"] == DenialReason.DASHBOARD_ACCESS_DENIED

            # Advance 800 virtual seconds (< 15 min) -> session still valid and slides TTL
            SimulationClock.advance(800)
            still_ok = await client.get("/api/v1/acs/fleet/view", headers=bearer(token))
            assert still_ok.status_code == 200

            # Advance 901 virtual seconds past last activity -> 401 SESSION_EXPIRED
            SimulationClock.advance(901)
            expired_resp = await client.get("/api/v1/acs/fleet/view", headers=bearer(token))
            assert expired_resp.status_code == 401
            assert expired_resp.json()["allowed"] is False
            assert expired_resp.json()["reason_code"] == DenialReason.SESSION_EXPIRED

            # Log in again and test explicit logout -> ADMIN_LOGOUT (MANUAL)
            token2 = await login_dashboard(client, "ADMIN-CARD-0001")
            logout_resp = await client.post(
                "/api/v1/acs/auth/dashboard/logout",
                headers=bearer(token2),
            )
            assert logout_resp.status_code == 200
            assert logout_resp.json()["details"]["reason"] == "MANUAL"

            after_logout = await client.get("/api/v1/acs/fleet/view", headers=bearer(token2))
            assert after_logout.status_code == 401
            assert after_logout.json()["reason_code"] == DenialReason.SESSION_INVALID

    asyncio.run(scenario())


def test_cab_swipe_one_tap_b1_exception_e5_and_clone_detection() -> None:
    async def scenario() -> None:
        async with api_test_session(base_time=MORNING_PEAK_BASE_TIME) as client:
            admin_token = await login_dashboard(client, "ADMIN-CARD-0001")
            res_uid = await seed_user_row(
                full_name="Resident Carol",
                role="RESIDENT",
                apartment_unit="10B",
                assigned_floor=10,
            )
            staff_uid = await seed_user_row(
                full_name="Staff Dan",
                role="BUILDING_SERVICE",
            )
            for uid, cid in [(res_uid, "RES-CARD-10"), (staff_uid, "STAFF-CARD-01")]:
                resp = await client.post(
                    "/api/v1/acs/credentials/manage",
                    headers=bearer(admin_token),
                    json_body={"action": "ISSUE", "target_user_id": uid, "target_credential_id": cid},
                )
                assert resp.status_code == 200

            # B1 destination requires no card (SEC-2.5)
            b1_resp = await client.post(
                "/api/v1/acs/cab/destination",
                json_body={"elevator_id": "E1", "origin_floor": 1, "requested_floor": -1},
            )
            assert b1_resp.status_code == 200
            assert b1_resp.json()["allowed"] is True

            # Resident swipes in E1 -> selects floor 10 -> allowed; second tap denied (ONE_TAP_LIMIT_EXCEEDED)
            swipe_ok = await client.post(
                "/api/v1/acs/auth/cab/swipe",
                json_body={"elevator_id": "E1", "credential_id": "RES-CARD-10", "origin_floor": 1},
            )
            assert swipe_ok.status_code == 200

            first_tap = await client.post(
                "/api/v1/acs/cab/destination",
                json_body={"elevator_id": "E1", "origin_floor": 1, "requested_floor": 10},
            )
            assert first_tap.status_code == 200

            second_tap = await client.post(
                "/api/v1/acs/cab/destination",
                json_body={"elevator_id": "E1", "origin_floor": 1, "requested_floor": 10},
            )
            assert second_tap.status_code == 403
            assert second_tap.json()["reason_code"] == DenialReason.ONE_TAP_LIMIT_EXCEEDED

            # Unauthorized floor (floor 15 for floor 10 resident) -> 403 UNAUTHORIZED_FLOOR
            SimulationClock.advance(20)
            await client.post(
                "/api/v1/acs/auth/cab/swipe",
                json_body={"elevator_id": "E2", "credential_id": "RES-CARD-10", "origin_floor": 1},
            )
            unauth_floor = await client.post(
                "/api/v1/acs/cab/destination",
                json_body={"elevator_id": "E2", "origin_floor": 1, "requested_floor": 15},
            )
            assert unauth_floor.status_code == 403
            assert unauth_floor.json()["reason_code"] == DenialReason.UNAUTHORIZED_FLOOR

            # E5 service run: B1 forbidden, staff floor 20 allowed
            e5_b1 = await client.post(
                "/api/v1/acs/cab/e5/service-run",
                json_body={"elevator_id": "E5", "destination_floor": -1, "purpose": "DELIVERY"},
            )
            assert e5_b1.status_code == 403
            assert e5_b1.json()["reason_code"] == DenialReason.E5_B1_FORBIDDEN

            await client.post(
                "/api/v1/acs/auth/cab/swipe",
                json_body={"elevator_id": "E5", "credential_id": "STAFF-CARD-01", "origin_floor": 1},
            )
            e5_ok = await client.post(
                "/api/v1/acs/cab/e5/service-run",
                json_body={"elevator_id": "E5", "destination_floor": 20, "purpose": "FREIGHT"},
            )
            assert e5_ok.status_code == 200

            # Clone detection: same card swiped on E3 within 10s of E5 swipe
            SimulationClock.advance(3)
            cloned_resp = await client.post(
                "/api/v1/acs/auth/cab/swipe",
                json_body={"elevator_id": "E3", "credential_id": "STAFF-CARD-01", "origin_floor": 1},
            )
            assert cloned_resp.status_code == 403
            assert cloned_resp.json()["alert_triggered"] is True
            assert cloned_resp.json()["reason_code"] == DenialReason.CARD_SUSPENDED

    asyncio.run(scenario())

