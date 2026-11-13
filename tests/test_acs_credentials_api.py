from __future__ import annotations

import asyncio
from datetime import timedelta

from acs.enums import (
    AuditActionType,
    CredentialAction,
    CredentialStatus,
    CredentialType,
    DenialReason,
    SuspensionReason,
    UserRole,
)
from db import DbConnectionPool
from simulation.clock import MORNING_PEAK_BASE_TIME, SimulationClock
from tests.conftest import api_test_session, bearer, login_dashboard, seed_user_row


async def _fetch_credential_row(credential_id: str) -> dict | None:
    async with DbConnectionPool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT credential_id, user_id, credential_type, status, suspension_reason,
                   suspension_start, revocation_reason, revoked_at, expires_at,
                   allowed_floors, issued_by_admin
            FROM credentials
            WHERE credential_id = $1
            """,
            credential_id,
        )
        return dict(row) if row is not None else None


async def _fetch_user_row(user_id: str) -> dict | None:
    async with DbConnectionPool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id, full_name, role, apartment_unit, assigned_floor FROM users WHERE user_id = $1",
            user_id,
        )
        return dict(row) if row is not None else None


async def _fetch_audit_actions(target_id: str) -> list[str]:
    async with DbConnectionPool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT action_type FROM audit_log_chain WHERE target_id = $1 ORDER BY log_index ASC",
            target_id,
        )
        return [r["action_type"] for r in rows]


def test_credentials_endpoints_require_admin_bearer_auth() -> None:
    async def scenario() -> None:
        async with api_test_session(base_time=MORNING_PEAK_BASE_TIME) as client:
            admin_token = await login_dashboard(client, "ADMIN-CARD-0001")

            tech_uid = await seed_user_row(full_name="Tech Auth", role=UserRole.TECHNICIAN)
            issue_tech = await client.post(
                "/api/v1/acs/credentials/manage",
                headers=bearer(admin_token),
                json_body={
                    "action": CredentialAction.ISSUE.value,
                    "target_user_id": tech_uid,
                    "target_credential_id": "TECH-AUTH-01",
                },
            )
            assert issue_tech.status_code == 200
            tech_token = await login_dashboard(client, "TECH-AUTH-01")

            res_uid = await seed_user_row(
                full_name="Res Target", role=UserRole.RESIDENT, assigned_floor=5
            )
            await client.post(
                "/api/v1/acs/credentials/manage",
                headers=bearer(admin_token),
                json_body={
                    "action": CredentialAction.ISSUE.value,
                    "target_user_id": res_uid,
                    "target_credential_id": "RES-AUTH-01",
                },
            )

            # Unauthenticated & invalid bearer tokens -> 401 SESSION_INVALID
            for endpoint, body in [
                (
                    "/api/v1/acs/credentials/manage",
                    {"action": CredentialAction.SUSPEND.value, "target_credential_id": "RES-AUTH-01"},
                ),
                (
                    "/api/v1/acs/credentials/replace-lost",
                    {"lost_credential_id": "RES-AUTH-01", "replacement_credential_id": "RES-AUTH-02"},
                ),
            ]:
                no_auth = await client.post(endpoint, json_body=body)
                assert no_auth.status_code == 401
                assert no_auth.json()["allowed"] is False
                assert no_auth.json()["reason_code"] == DenialReason.SESSION_INVALID

                bad_auth = await client.post(
                    endpoint,
                    headers=bearer("non-existent-opaque-token"),
                    json_body=body,
                )
                assert bad_auth.status_code == 401
                assert bad_auth.json()["reason_code"] == DenialReason.SESSION_INVALID

                # Technician cannot manage or replace credentials -> 403 UNAUTHORIZED_ROLE
                tech_resp = await client.post(
                    endpoint,
                    headers=bearer(tech_token),
                    json_body=body,
                )
                assert tech_resp.status_code == 403
                assert tech_resp.json()["allowed"] is False
                assert tech_resp.json()["reason_code"] == DenialReason.UNAUTHORIZED_ROLE

    asyncio.run(scenario())


def test_manage_issue_and_issue_visitor_pass_actions() -> None:
    async def scenario() -> None:
        async with api_test_session(base_time=MORNING_PEAK_BASE_TIME) as client:
            admin_token = await login_dashboard(client, "ADMIN-CARD-0001")

            # 1. CredentialAction.ISSUE for an existing RESIDENT user
            res_uid = await seed_user_row(
                full_name="Resident Issue",
                role=UserRole.RESIDENT,
                apartment_unit="15C",
                assigned_floor=15,
            )
            res_resp = await client.post(
                "/api/v1/acs/credentials/manage",
                headers=bearer(admin_token),
                json_body={
                    "action": CredentialAction.ISSUE.value,
                    "target_user_id": res_uid,
                    "target_credential_id": "RES-ISSUE-15",
                },
            )
            assert res_resp.status_code == 200
            res_body = res_resp.json()
            assert res_body["allowed"] is True
            assert res_body["credential_id"] == "RES-ISSUE-15"
            assert res_body["details"]["role"] == UserRole.RESIDENT
            assert set(res_body["details"]["allowed_floors"]) == {-1, 1, 15}

            res_row = await _fetch_credential_row("RES-ISSUE-15")
            assert res_row is not None
            assert res_row["status"] == CredentialStatus.ACTIVE
            assert res_row["credential_type"] == CredentialType.RESIDENT
            assert AuditActionType.CREDENTIAL_ISSUED in await _fetch_audit_actions("RES-ISSUE-15")

            # 2. CredentialAction.ISSUE auto-creating a new BUILDING_SERVICE user when target_user_id is omitted
            staff_resp = await client.post(
                "/api/v1/acs/credentials/manage",
                headers=bearer(admin_token),
                json_body={
                    "action": CredentialAction.ISSUE.value,
                    "target_credential_id": "STAFF-AUTO-01",
                    "payload_details": {
                        "full_name": "Auto Staff",
                        "role": UserRole.BUILDING_SERVICE.value,
                        "allowed_floors": [1, 2, 3],
                    },
                },
            )
            assert staff_resp.status_code == 200
            auto_user_id = staff_resp.json()["details"]["user_id"]
            auto_user = await _fetch_user_row(auto_user_id)
            assert auto_user is not None
            assert auto_user["full_name"] == "Auto Staff"
            assert auto_user["role"] == UserRole.BUILDING_SERVICE

            staff_row = await _fetch_credential_row("STAFF-AUTO-01")
            assert staff_row is not None
            assert staff_row["credential_type"] == CredentialType.STAFF

            # 3. ISSUE error paths: missing target_credential_id -> 400; duplicate -> 409
            missing_id_resp = await client.post(
                "/api/v1/acs/credentials/manage",
                headers=bearer(admin_token),
                json_body={"action": CredentialAction.ISSUE.value, "target_user_id": res_uid},
            )
            assert missing_id_resp.status_code == 400
            assert missing_id_resp.json()["reason_code"] == DenialReason.MALFORMED_MSG_REJECTED

            dup_resp = await client.post(
                "/api/v1/acs/credentials/manage",
                headers=bearer(admin_token),
                json_body={
                    "action": CredentialAction.ISSUE.value,
                    "target_user_id": res_uid,
                    "target_credential_id": "RES-ISSUE-15",
                },
            )
            assert dup_resp.status_code == 409
            assert dup_resp.json()["reason_code"] == DenialReason.CREDENTIAL_ALREADY_EXISTS

            # 4. CredentialAction.ISSUE_VISITOR_PASS with default 4h duration
            vis_uid = await seed_user_row(full_name="Visitor Default", role=UserRole.VISITOR)
            vis_default_resp = await client.post(
                "/api/v1/acs/credentials/manage",
                headers=bearer(admin_token),
                json_body={
                    "action": CredentialAction.ISSUE_VISITOR_PASS.value,
                    "target_user_id": vis_uid,
                    "target_credential_id": "VIS-DEFAULT-01",
                    "payload_details": {"allowed_floors": [12]},
                },
            )
            assert vis_default_resp.status_code == 200
            vis_default_body = vis_default_resp.json()
            assert vis_default_body["details"]["role"] == UserRole.VISITOR
            assert set(vis_default_body["details"]["allowed_floors"]) == {-1, 1, 12}

            vis_row = await _fetch_credential_row("VIS-DEFAULT-01")
            assert vis_row is not None
            assert vis_row["credential_type"] == CredentialType.VISITOR
            assert vis_row["expires_at"] == MORNING_PEAK_BASE_TIME + timedelta(hours=4)
            assert AuditActionType.VISITOR_PASS_ISSUED in await _fetch_audit_actions("VIS-DEFAULT-01")

            # 5. CredentialAction.ISSUE_VISITOR_PASS with custom duration_seconds and expiry check
            vis_short_resp = await client.post(
                "/api/v1/acs/credentials/manage",
                headers=bearer(admin_token),
                json_body={
                    "action": CredentialAction.ISSUE_VISITOR_PASS.value,
                    "target_credential_id": "VIS-SHORT-01",
                    "payload_details": {
                        "full_name": "Short Visitor",
                        "allowed_floors": [9],
                        "duration_seconds": 60,
                    },
                },
            )
            assert vis_short_resp.status_code == 200

            # Valid before expiration
            swipe_ok = await client.post(
                "/api/v1/acs/auth/cab/swipe",
                json_body={"elevator_id": "E1", "credential_id": "VIS-SHORT-01", "origin_floor": 1},
            )
            assert swipe_ok.status_code == 200

            # Advance clock past 60s -> visitor pass expires (403 CARD_EXPIRED)
            SimulationClock.advance(61)
            swipe_expired = await client.post(
                "/api/v1/acs/auth/cab/swipe",
                json_body={"elevator_id": "E1", "credential_id": "VIS-SHORT-01", "origin_floor": 1},
            )
            assert swipe_expired.status_code == 403
            assert swipe_expired.json()["reason_code"] == DenialReason.CARD_EXPIRED

            # ISSUE_VISITOR_PASS missing target_credential_id -> 400; duplicate -> 409
            vis_missing = await client.post(
                "/api/v1/acs/credentials/manage",
                headers=bearer(admin_token),
                json_body={"action": CredentialAction.ISSUE_VISITOR_PASS.value},
            )
            assert vis_missing.status_code == 400
            assert vis_missing.json()["reason_code"] == DenialReason.MALFORMED_MSG_REJECTED

            vis_dup = await client.post(
                "/api/v1/acs/credentials/manage",
                headers=bearer(admin_token),
                json_body={
                    "action": CredentialAction.ISSUE_VISITOR_PASS.value,
                    "target_credential_id": "VIS-DEFAULT-01",
                },
            )
            assert vis_dup.status_code == 409
            assert vis_dup.json()["reason_code"] == DenialReason.CREDENTIAL_ALREADY_EXISTS

    asyncio.run(scenario())


def test_manage_suspend_reinstate_revoke_and_invalid_transitions() -> None:
    async def scenario() -> None:
        async with api_test_session(base_time=MORNING_PEAK_BASE_TIME) as client:
            admin_token = await login_dashboard(client, "ADMIN-CARD-0001")

            res_uid = await seed_user_row(
                full_name="Lifecycle Resident", role=UserRole.RESIDENT, assigned_floor=7
            )
            for cid in ("RES-LIFE-01", "RES-LIFE-02"):
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

            # Validation errors for status-modifying actions: missing ID, unknown ID, unsupported action
            for action in (
                CredentialAction.SUSPEND.value,
                CredentialAction.REINSTATE.value,
                CredentialAction.REVOKE.value,
            ):
                missing = await client.post(
                    "/api/v1/acs/credentials/manage",
                    headers=bearer(admin_token),
                    json_body={"action": action},
                )
                assert missing.status_code == 400
                assert missing.json()["reason_code"] == DenialReason.MALFORMED_MSG_REJECTED

                unknown = await client.post(
                    "/api/v1/acs/credentials/manage",
                    headers=bearer(admin_token),
                    json_body={"action": action, "target_credential_id": "NO-SUCH-CARD"},
                )
                assert unknown.status_code == 401
                assert unknown.json()["reason_code"] == DenialReason.UNREGISTERED_CARD

            unsupported = await client.post(
                "/api/v1/acs/credentials/manage",
                headers=bearer(admin_token),
                json_body={"action": "ARCHIVE_CARD", "target_credential_id": "RES-LIFE-01"},
            )
            assert unsupported.status_code == 400
            assert unsupported.json()["reason_code"] == DenialReason.MALFORMED_MSG_REJECTED

            # Cannot REINSTATE an already ACTIVE card -> 409 INVALID_STATE_TRANSITION
            reinstate_active = await client.post(
                "/api/v1/acs/credentials/manage",
                headers=bearer(admin_token),
                json_body={
                    "action": CredentialAction.REINSTATE.value,
                    "target_credential_id": "RES-LIFE-01",
                },
            )
            assert reinstate_active.status_code == 409
            assert reinstate_active.json()["reason_code"] == DenialReason.INVALID_STATE_TRANSITION

            # CredentialAction.SUSPEND (ACTIVE -> SUSPENDED) with default ADMIN_MANUAL fallback when omitted
            suspend_manual = await client.post(
                "/api/v1/acs/credentials/manage",
                headers=bearer(admin_token),
                json_body={
                    "action": CredentialAction.SUSPEND.value,
                    "target_credential_id": "RES-LIFE-01",
                },
            )
            assert suspend_manual.status_code == 200
            row_susp = await _fetch_credential_row("RES-LIFE-01")
            assert row_susp is not None
            assert row_susp["status"] == CredentialStatus.SUSPENDED
            assert row_susp["suspension_reason"] == SuspensionReason.ADMIN_MANUAL

            # Suspended card blocked at cab reader -> 403 CARD_SUSPENDED
            susp_swipe = await client.post(
                "/api/v1/acs/auth/cab/swipe",
                json_body={"elevator_id": "E1", "credential_id": "RES-LIFE-01", "origin_floor": 1},
            )
            assert susp_swipe.status_code == 403
            assert susp_swipe.json()["reason_code"] == DenialReason.CARD_SUSPENDED

            # Cannot SUSPEND an already SUSPENDED card -> 409 INVALID_STATE_TRANSITION
            double_suspend = await client.post(
                "/api/v1/acs/credentials/manage",
                headers=bearer(admin_token),
                json_body={
                    "action": CredentialAction.SUSPEND.value,
                    "target_credential_id": "RES-LIFE-01",
                    "payload_details": {"suspension_reason": SuspensionReason.LOST.value},
                },
            )
            assert double_suspend.status_code == 409
            assert double_suspend.json()["reason_code"] == DenialReason.INVALID_STATE_TRANSITION

            # CredentialAction.REINSTATE (SUSPENDED -> ACTIVE)
            reinstate_ok = await client.post(
                "/api/v1/acs/credentials/manage",
                headers=bearer(admin_token),
                json_body={
                    "action": CredentialAction.REINSTATE.value,
                    "target_credential_id": "RES-LIFE-01",
                    "payload_details": {"notes": "Resident verified in lobby"},
                },
            )
            assert reinstate_ok.status_code == 200
            row_reinstated = await _fetch_credential_row("RES-LIFE-01")
            assert row_reinstated is not None
            assert row_reinstated["status"] == CredentialStatus.ACTIVE
            assert row_reinstated["suspension_reason"] is None
            assert row_reinstated["suspension_start"] is None

            # Reinstated card works again at cab reader
            reinstated_swipe = await client.post(
                "/api/v1/acs/auth/cab/swipe",
                json_body={"elevator_id": "E1", "credential_id": "RES-LIFE-01", "origin_floor": 1},
            )
            assert reinstated_swipe.status_code == 200

            # CredentialAction.SUSPEND with explicit CLONE_SUSPECTED then REVOKE from SUSPENDED
            suspend_clone = await client.post(
                "/api/v1/acs/credentials/manage",
                headers=bearer(admin_token),
                json_body={
                    "action": CredentialAction.SUSPEND.value,
                    "target_credential_id": "RES-LIFE-01",
                    "payload_details": {"suspension_reason": SuspensionReason.CLONE_SUSPECTED.value},
                },
            )
            assert suspend_clone.status_code == 200
            assert (await _fetch_credential_row("RES-LIFE-01"))["suspension_reason"] == SuspensionReason.CLONE_SUSPECTED

            # CredentialAction.REVOKE from SUSPENDED -> REVOKED
            revoke_from_susp = await client.post(
                "/api/v1/acs/credentials/manage",
                headers=bearer(admin_token),
                json_body={
                    "action": CredentialAction.REVOKE.value,
                    "target_credential_id": "RES-LIFE-01",
                    "payload_details": {"revocation_reason": "CONFIRMED_CLONE"},
                },
            )
            assert revoke_from_susp.status_code == 200
            row_revoked = await _fetch_credential_row("RES-LIFE-01")
            assert row_revoked is not None
            assert row_revoked["status"] == CredentialStatus.REVOKED
            assert row_revoked["revocation_reason"] == "CONFIRMED_CLONE"

            # CredentialAction.REVOKE directly from ACTIVE -> REVOKED
            revoke_active = await client.post(
                "/api/v1/acs/credentials/manage",
                headers=bearer(admin_token),
                json_body={
                    "action": CredentialAction.REVOKE.value,
                    "target_credential_id": "RES-LIFE-02",
                    "payload_details": {"revocation_reason": "LEASE_ENDED"},
                },
            )
            assert revoke_active.status_code == 200
            assert (await _fetch_credential_row("RES-LIFE-02"))["status"] == CredentialStatus.REVOKED

            # Once REVOKED, SUSPEND / REINSTATE / REVOKE all fail with 409 INVALID_STATE_TRANSITION
            for action in (
                CredentialAction.SUSPEND.value,
                CredentialAction.REINSTATE.value,
                CredentialAction.REVOKE.value,
            ):
                post_revoke = await client.post(
                    "/api/v1/acs/credentials/manage",
                    headers=bearer(admin_token),
                    json_body={"action": action, "target_credential_id": "RES-LIFE-01"},
                )
                assert post_revoke.status_code == 409
                assert post_revoke.json()["reason_code"] == DenialReason.INVALID_STATE_TRANSITION

            # Check full audit trail on RES-LIFE-01
            actions_seen = await _fetch_audit_actions("RES-LIFE-01")
            assert actions_seen == [
                AuditActionType.CREDENTIAL_ISSUED,
                AuditActionType.CREDENTIAL_SUSPENDED,
                AuditActionType.CREDENTIAL_REINSTATED,
                AuditActionType.CREDENTIAL_SUSPENDED,
                AuditActionType.CREDENTIAL_REVOKED,
            ]

            # Expired visitor pass cannot be suspended, reinstated, or revoked -> 409 INVALID_STATE_TRANSITION
            assert (
                await client.post(
                    "/api/v1/acs/credentials/manage",
                    headers=bearer(admin_token),
                    json_body={
                        "action": CredentialAction.ISSUE_VISITOR_PASS.value,
                        "target_credential_id": "VIS-EXP-01",
                        "payload_details": {"duration_seconds": 30},
                    },
                )
            ).status_code == 200
            SimulationClock.advance(31)
            for action in (
                CredentialAction.SUSPEND.value,
                CredentialAction.REINSTATE.value,
                CredentialAction.REVOKE.value,
            ):
                exp_res = await client.post(
                    "/api/v1/acs/credentials/manage",
                    headers=bearer(admin_token),
                    json_body={"action": action, "target_credential_id": "VIS-EXP-01"},
                )
                assert exp_res.status_code == 409
                assert exp_res.json()["reason_code"] == DenialReason.INVALID_STATE_TRANSITION

    asyncio.run(scenario())


def test_peer_admin_restrictions_and_cross_admin_management() -> None:
    async def scenario() -> None:
        async with api_test_session(base_time=MORNING_PEAK_BASE_TIME) as client:
            admin1_token = await login_dashboard(client, "ADMIN-CARD-0001")

            # Seed a second distinct Admin user + card
            admin2_uid = await seed_user_row(full_name="Peer Admin Two", role=UserRole.ADMIN)
            issue_admin2 = await client.post(
                "/api/v1/acs/credentials/manage",
                headers=bearer(admin1_token),
                json_body={
                    "action": CredentialAction.ISSUE.value,
                    "target_user_id": admin2_uid,
                    "target_credential_id": "ADMIN-CARD-0002",
                },
            )
            assert issue_admin2.status_code == 200
            admin2_token = await login_dashboard(client, "ADMIN-CARD-0002")

            # Admin1 cannot self-suspend, self-revoke, or self-replace their own card -> 403 PEER_ADMIN_REQUIRED
            for action in (
                CredentialAction.SUSPEND.value,
                CredentialAction.REVOKE.value,
            ):
                self_op = await client.post(
                    "/api/v1/acs/credentials/manage",
                    headers=bearer(admin1_token),
                    json_body={"action": action, "target_credential_id": "ADMIN-CARD-0001"},
                )
                assert self_op.status_code == 403
                assert self_op.json()["reason_code"] == DenialReason.PEER_ADMIN_REQUIRED

            self_replace = await client.post(
                "/api/v1/acs/credentials/replace-lost",
                headers=bearer(admin1_token),
                json_body={
                    "lost_credential_id": "ADMIN-CARD-0001",
                    "replacement_credential_id": "ADMIN-CARD-0001-NEW",
                },
            )
            assert self_replace.status_code == 403
            assert self_replace.json()["reason_code"] == DenialReason.PEER_ADMIN_REQUIRED

            # Admin1 also cannot manage a second card tied to Admin1's own user_id
            admin1_row = await _fetch_credential_row("ADMIN-CARD-0001")
            assert admin1_row is not None
            issue_spare_admin1 = await client.post(
                "/api/v1/acs/credentials/manage",
                headers=bearer(admin2_token),
                json_body={
                    "action": CredentialAction.ISSUE.value,
                    "target_user_id": admin1_row["user_id"],
                    "target_credential_id": "ADMIN-CARD-0001-SPARE",
                },
            )
            assert issue_spare_admin1.status_code == 200

            same_user_suspend = await client.post(
                "/api/v1/acs/credentials/manage",
                headers=bearer(admin1_token),
                json_body={
                    "action": CredentialAction.SUSPEND.value,
                    "target_credential_id": "ADMIN-CARD-0001-SPARE",
                },
            )
            assert same_user_suspend.status_code == 403
            assert same_user_suspend.json()["reason_code"] == DenialReason.PEER_ADMIN_REQUIRED

            # Admin1 CAN suspend and reinstate peer Admin2's card
            peer_susp = await client.post(
                "/api/v1/acs/credentials/manage",
                headers=bearer(admin1_token),
                json_body={
                    "action": CredentialAction.SUSPEND.value,
                    "target_credential_id": "ADMIN-CARD-0002",
                    "payload_details": {"suspension_reason": SuspensionReason.LOST.value},
                },
            )
            assert peer_susp.status_code == 200

            peer_reinst = await client.post(
                "/api/v1/acs/credentials/manage",
                headers=bearer(admin1_token),
                json_body={
                    "action": CredentialAction.REINSTATE.value,
                    "target_credential_id": "ADMIN-CARD-0002",
                },
            )
            assert peer_reinst.status_code == 200

            # Admin2 CAN atomically replace peer Admin1's lost card (preserving ADMIN type & all floors)
            peer_replace = await client.post(
                "/api/v1/acs/credentials/replace-lost",
                headers=bearer(admin2_token),
                json_body={
                    "lost_credential_id": "ADMIN-CARD-0001",
                    "replacement_credential_id": "ADMIN-CARD-0001-R2",
                    "reason": "PEER_VERIFIED_LOSS",
                },
            )
            assert peer_replace.status_code == 200
            assert peer_replace.json()["credential_id"] == "ADMIN-CARD-0001-R2"
            assert (await _fetch_credential_row("ADMIN-CARD-0001"))["status"] == CredentialStatus.REVOKED
            new_admin1 = await _fetch_credential_row("ADMIN-CARD-0001-R2")
            assert new_admin1 is not None
            assert new_admin1["status"] == CredentialStatus.ACTIVE
            assert new_admin1["credential_type"] == CredentialType.ADMIN

            # Replacement Admin card can immediately log into the dashboard
            r2_token = await login_dashboard(client, "ADMIN-CARD-0001-R2")
            assert r2_token

    asyncio.run(scenario())


def test_replace_lost_card_preserves_attributes_atomicity_and_error_cases() -> None:
    async def scenario() -> None:
        async with api_test_session(base_time=MORNING_PEAK_BASE_TIME) as client:
            admin_token = await login_dashboard(client, "ADMIN-CARD-0001")

            # Unknown lost_credential_id -> 401 UNREGISTERED_CARD
            missing_lost = await client.post(
                "/api/v1/acs/credentials/replace-lost",
                headers=bearer(admin_token),
                json_body={
                    "lost_credential_id": "GHOST-CARD-01",
                    "replacement_credential_id": "GHOST-CARD-02",
                },
            )
            assert missing_lost.status_code == 401
            assert missing_lost.json()["reason_code"] == DenialReason.UNREGISTERED_CARD

            # Issue a visitor pass with custom floors & expiry, then suspend it as LOST, then replace-lost
            vis_uid = await seed_user_row(full_name="Lost Visitor", role=UserRole.VISITOR)
            assert (
                await client.post(
                    "/api/v1/acs/credentials/manage",
                    headers=bearer(admin_token),
                    json_body={
                        "action": CredentialAction.ISSUE_VISITOR_PASS.value,
                        "target_user_id": vis_uid,
                        "target_credential_id": "VIS-LOST-01",
                        "payload_details": {"allowed_floors": [18, 19], "duration_seconds": 3600},
                    },
                )
            ).status_code == 200

            assert (
                await client.post(
                    "/api/v1/acs/credentials/manage",
                    headers=bearer(admin_token),
                    json_body={
                        "action": CredentialAction.SUSPEND.value,
                        "target_credential_id": "VIS-LOST-01",
                        "payload_details": {"suspension_reason": SuspensionReason.LOST.value},
                    },
                )
            ).status_code == 200

            # Seed an already-used replacement ID and confirm 409 REPLACEMENT_ID_IN_USE rolls back atomically
            other_uid = await seed_user_row(full_name="Other User", role=UserRole.RESIDENT, assigned_floor=3)
            assert (
                await client.post(
                    "/api/v1/acs/credentials/manage",
                    headers=bearer(admin_token),
                    json_body={
                        "action": CredentialAction.ISSUE.value,
                        "target_user_id": other_uid,
                        "target_credential_id": "TAKEN-CARD-99",
                    },
                )
            ).status_code == 200

            conflict_resp = await client.post(
                "/api/v1/acs/credentials/replace-lost",
                headers=bearer(admin_token),
                json_body={
                    "lost_credential_id": "VIS-LOST-01",
                    "replacement_credential_id": "TAKEN-CARD-99",
                    "reason": "SHOULD_ROLL_BACK",
                },
            )
            assert conflict_resp.status_code == 409
            assert conflict_resp.json()["reason_code"] == DenialReason.REPLACEMENT_ID_IN_USE
            # VIS-LOST-01 must remain SUSPENDED (not REVOKED) after conflict
            assert (await _fetch_credential_row("VIS-LOST-01"))["status"] == CredentialStatus.SUSPENDED

            # Now replace with a fresh replacement_credential_id
            replace_ok = await client.post(
                "/api/v1/acs/credentials/replace-lost",
                headers=bearer(admin_token),
                json_body={
                    "lost_credential_id": "VIS-LOST-01",
                    "replacement_credential_id": "VIS-FOUND-02",
                    "reason": "REISSUED_AT_DESK",
                },
            )
            assert replace_ok.status_code == 200
            body = replace_ok.json()
            assert body["allowed"] is True
            assert body["credential_id"] == "VIS-FOUND-02"
            assert body["details"]["user_id"] == vis_uid
            assert set(body["details"]["assigned_floors"]) == {-1, 1, 18, 19}

            old_row = await _fetch_credential_row("VIS-LOST-01")
            new_row = await _fetch_credential_row("VIS-FOUND-02")
            assert old_row is not None and old_row["status"] == CredentialStatus.REVOKED
            assert old_row["revocation_reason"] == "REISSUED_AT_DESK"
            assert new_row is not None and new_row["status"] == CredentialStatus.ACTIVE
            assert new_row["credential_type"] == CredentialType.VISITOR
            assert new_row["expires_at"] == MORNING_PEAK_BASE_TIME + timedelta(hours=1)

            # New card grants floor 18, denies unauthorized floor 20; old card raises REVOKED alert
            swipe_new = await client.post(
                "/api/v1/acs/auth/cab/swipe",
                json_body={"elevator_id": "E1", "credential_id": "VIS-FOUND-02", "origin_floor": 1},
            )
            assert swipe_new.status_code == 200
            dest_ok = await client.post(
                "/api/v1/acs/cab/destination",
                json_body={"elevator_id": "E1", "origin_floor": 1, "requested_floor": 18},
            )
            assert dest_ok.status_code == 200

            swipe_old = await client.post(
                "/api/v1/acs/auth/cab/swipe",
                json_body={"elevator_id": "E2", "credential_id": "VIS-LOST-01", "origin_floor": 1},
            )
            assert swipe_old.status_code == 403
            assert swipe_old.json()["reason_code"] == DenialReason.CARD_REVOKED
            assert swipe_old.json()["alert_triggered"] is True

            assert AuditActionType.CREDENTIAL_REPLACED in await _fetch_audit_actions("VIS-FOUND-02")

    asyncio.run(scenario())

