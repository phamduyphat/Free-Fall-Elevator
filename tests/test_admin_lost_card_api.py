from __future__ import annotations

import asyncio

from acs.enums import (
    AlertStatus,
    AlertType,
    AuditActionType,
    AuthorizationOutcome,
    CredentialAction,
    CredentialStatus,
    CredentialType,
    DenialReason,
    SuspensionReason,
    UserRole,
)
from db import DbConnectionPool
from simulation.clock import MORNING_PEAK_BASE_TIME, SimulationClock
from tests.conftest import api_test_session, bearer, login_dashboard


async def _suspend_admin1_as_lost_by_peer(client, admin2_token: str):
    return await client.post(
        "/api/v1/acs/credentials/manage",
        headers=bearer(admin2_token),
        json_body={
            "action": CredentialAction.SUSPEND.value,
            "target_credential_id": "ADMIN-CARD-0001",
            "payload_details": {"suspension_reason": SuspensionReason.LOST.value},
        },
    )


def test_lost_card_report_by_self() -> None:
    async def scenario() -> None:
        async with api_test_session(base_time=MORNING_PEAK_BASE_TIME) as client:
            admin1_token = await login_dashboard(client, "ADMIN-CARD-0001")
            admin2_token = await login_dashboard(client, "ADMIN-CARD-0002")

            self_report = await client.post(
                "/api/v1/acs/credentials/manage",
                headers=bearer(admin1_token),
                json_body={
                    "action": CredentialAction.SUSPEND.value,
                    "target_credential_id": "ADMIN-CARD-0001",
                    "payload_details": {"suspension_reason": SuspensionReason.LOST.value},
                },
            )
            assert self_report.status_code == 403
            self_body = self_report.json()
            assert self_body["allowed"] is False
            assert self_body["outcome"] == AuthorizationOutcome.DENIED
            assert self_body["reason_code"] == DenialReason.PEER_ADMIN_REQUIRED

            # Verify unauthorized self-suspend was audited
            self_audit_resp = await client.get(
                "/api/v1/acs/logs/audit",
                headers=bearer(admin2_token),
                params={
                    "actor_id": "ADMIN-CARD-0001",
                    "action_type": AuditActionType.UNAUTHORIZED_CMD_REJECTED.value,
                    "target_id": "ADMIN-CARD-0001",
                },
            )
            assert self_audit_resp.status_code == 200
            assert self_audit_resp.json()["count"] >= 1
            assert (
                self_audit_resp.json()["items"][0]["event_payload"]["reason"]
                == DenialReason.PEER_ADMIN_REQUIRED
            )

            verify_resp = await client.post(
                "/api/v1/acs/logs/audit/verify",
                headers=bearer(admin2_token),
            )
            assert verify_resp.status_code == 200
            assert verify_resp.json()["details"]["passed"] is True

    asyncio.run(scenario())


def test_lost_card_report_by_peer() -> None:
    async def scenario() -> None:
        async with api_test_session(base_time=MORNING_PEAK_BASE_TIME) as client:
            admin2_token = await login_dashboard(client, "ADMIN-CARD-0002")

            peer_report = await _suspend_admin1_as_lost_by_peer(client, admin2_token)
            assert peer_report.status_code == 200
            peer_body = peer_report.json()
            assert peer_body["allowed"] is True
            assert peer_body["outcome"] == AuthorizationOutcome.ALLOWED
            assert peer_body["reason_code"] is None
            assert peer_body["credential_id"] == "ADMIN-CARD-0001"

            async with DbConnectionPool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT status, suspension_reason FROM credentials WHERE credential_id = 'ADMIN-CARD-0001'"
                )
                assert row is not None
                assert row["status"] == CredentialStatus.SUSPENDED
                assert row["suspension_reason"] == SuspensionReason.LOST

            suspend_audit_resp = await client.get(
                "/api/v1/acs/logs/audit",
                headers=bearer(admin2_token),
                params={
                    "actor_id": "ADMIN-CARD-0002",
                    "action_type": AuditActionType.CREDENTIAL_SUSPENDED.value,
                    "target_id": "ADMIN-CARD-0001",
                },
            )
            assert suspend_audit_resp.status_code == 200
            assert suspend_audit_resp.json()["count"] == 1
            susp_payload = suspend_audit_resp.json()["items"][0]["event_payload"]
            assert susp_payload["reason"] == SuspensionReason.LOST
            assert susp_payload["window_hours"] == 72

            verify_resp = await client.post(
                "/api/v1/acs/logs/audit/verify",
                headers=bearer(admin2_token),
            )
            assert verify_resp.status_code == 200
            assert verify_resp.json()["details"]["passed"] is True

    asyncio.run(scenario())


def test_attempted_lost_card_usage() -> None:
    async def scenario() -> None:
        async with api_test_session(base_time=MORNING_PEAK_BASE_TIME) as client:
            admin2_token = await login_dashboard(client, "ADMIN-CARD-0002")

            peer_report = await _suspend_admin1_as_lost_by_peer(client, admin2_token)
            assert peer_report.status_code == 200

            SimulationClock.advance(1)
            cab_attempt = await client.post(
                "/api/v1/acs/auth/cab/swipe",
                json_body={
                    "elevator_id": "E1",
                    "credential_id": "ADMIN-CARD-0001",
                    "origin_floor": 1,
                },
            )
            assert cab_attempt.status_code == 403
            cab_body = cab_attempt.json()
            assert cab_body["allowed"] is False
            assert cab_body["outcome"] == AuthorizationOutcome.DENIED
            assert cab_body["reason_code"] == DenialReason.CARD_SUSPENDED
            assert cab_body["alert_triggered"] is True

            # Verify authorization event, security alert (first attempt), and audit denial log
            auth_events_resp = await client.get(
                "/api/v1/acs/logs/authorization-events",
                headers=bearer(admin2_token),
                params={
                    "credential_id": "ADMIN-CARD-0001",
                    "outcome": AuthorizationOutcome.DENIED.value,
                    "denial_reason": DenialReason.CARD_SUSPENDED.value,
                },
            )
            assert auth_events_resp.status_code == 200
            assert auth_events_resp.json()["count"] >= 1
            assert auth_events_resp.json()["items"][0]["alert_triggered"] is True

            alerts_resp = await client.get(
                "/api/v1/acs/logs/alerts",
                headers=bearer(admin2_token),
                params={
                    "credential_id": "ADMIN-CARD-0001",
                    "alert_type": AlertType.LOST_CARD_USAGE.value,
                    "status": AlertStatus.OPEN.value,
                },
            )
            assert alerts_resp.status_code == 200
            assert alerts_resp.json()["count"] >= 1

            denial_audit_resp = await client.get(
                "/api/v1/acs/logs/audit",
                headers=bearer(admin2_token),
                params={
                    "actor_id": "ADMIN-CARD-0001",
                    "action_type": AuditActionType.ACCESS_DENIAL.value,
                },
            )
            assert denial_audit_resp.status_code == 200
            assert denial_audit_resp.json()["count"] >= 1

            verify_resp = await client.post(
                "/api/v1/acs/logs/audit/verify",
                headers=bearer(admin2_token),
            )
            assert verify_resp.status_code == 200
            assert verify_resp.json()["details"]["passed"] is True

    asyncio.run(scenario())


def test_lost_card_void_and_reissue_for_admin_by_peer() -> None:
    async def scenario() -> None:
        async with api_test_session(base_time=MORNING_PEAK_BASE_TIME) as client:
            admin2_token = await login_dashboard(client, "ADMIN-CARD-0002")

            peer_report = await _suspend_admin1_as_lost_by_peer(client, admin2_token)
            assert peer_report.status_code == 200

            SimulationClock.advance(1)
            reissue_resp = await client.post(
                "/api/v1/acs/credentials/replace-lost",
                headers=bearer(admin2_token),
                json_body={
                    "lost_credential_id": "ADMIN-CARD-0001",
                    "replacement_credential_id": "ADMIN-CARD-0001-REISSUED",
                    "reason": "LOST_AND_REPLACED",
                },
            )
            assert reissue_resp.status_code == 200
            reissue_body = reissue_resp.json()
            assert reissue_body["allowed"] is True
            assert reissue_body["outcome"] == AuthorizationOutcome.ALLOWED
            assert reissue_body["reason_code"] is None
            assert reissue_body["credential_id"] == "ADMIN-CARD-0001-REISSUED"
            assert reissue_body["details"]["lost_credential_id"] == "ADMIN-CARD-0001"
            assert reissue_body["details"]["role"] == UserRole.ADMIN

            async with DbConnectionPool.acquire() as conn:
                old_row = await conn.fetchrow(
                    "SELECT status, revocation_reason FROM credentials WHERE credential_id = 'ADMIN-CARD-0001'"
                )
                new_row = await conn.fetchrow(
                    "SELECT status, credential_type FROM credentials WHERE credential_id = 'ADMIN-CARD-0001-REISSUED'"
                )
                assert old_row is not None
                assert old_row["status"] == CredentialStatus.REVOKED
                assert old_row["revocation_reason"] == "LOST_AND_REPLACED"
                assert new_row is not None
                assert new_row["status"] == CredentialStatus.ACTIVE
                assert new_row["credential_type"] == CredentialType.ADMIN

            replace_audit_resp = await client.get(
                "/api/v1/acs/logs/audit",
                headers=bearer(admin2_token),
                params={
                    "actor_id": "ADMIN-CARD-0002",
                    "action_type": AuditActionType.CREDENTIAL_REPLACED.value,
                    "target_id": "ADMIN-CARD-0001-REISSUED",
                },
            )
            assert replace_audit_resp.status_code == 200
            assert replace_audit_resp.json()["count"] == 1
            rep_payload = replace_audit_resp.json()["items"][0]["event_payload"]
            assert rep_payload["lost_credential_id"] == "ADMIN-CARD-0001"
            assert rep_payload["replacement_credential_id"] == "ADMIN-CARD-0001-REISSUED"

            # Reissued admin card can log into dashboard; self replace-lost by Admin2 is denied
            reissued_token = await login_dashboard(client, "ADMIN-CARD-0001-REISSUED")
            assert reissued_token

            self_replace = await client.post(
                "/api/v1/acs/credentials/replace-lost",
                headers=bearer(admin2_token),
                json_body={
                    "lost_credential_id": "ADMIN-CARD-0002",
                    "replacement_credential_id": "ADMIN-CARD-0002-SELF",
                },
            )
            assert self_replace.status_code == 403
            assert self_replace.json()["reason_code"] == DenialReason.PEER_ADMIN_REQUIRED

            verify_resp = await client.post(
                "/api/v1/acs/logs/audit/verify",
                headers=bearer(reissued_token),
            )
            assert verify_resp.status_code == 200
            assert verify_resp.json()["details"]["passed"] is True

    asyncio.run(scenario())
