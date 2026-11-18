"""Tests for SRS Verification Scenarios: VT-21, VT-22, VT-27, VT-30, VT-31, and VT-32."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
from typing import Any, AsyncIterator, Optional
import unittest

from acs.cab_accesscontrol import CabAccessControl
from acs.dashboard_accesscontrol import DashboardAccessControl
from acs.enums import AuditActionType, DenialReason
from api import app
from db import get_db_pool
from simulation.clock import MORNING_PEAK_BASE_TIME, SimulationClock
from simulation.operational import OperationalError, operational_simulator
from tests.conftest import AsgiClient


class _FakeConn:
    def __init__(self, store: _FakeDbStore) -> None:
        self._store = store

    async def fetchrow(self, query: str, *args: Any) -> Optional[dict[str, Any]]:
        q = " ".join(query.split())
        if "FROM credentials c" in q and "WHERE c.credential_id = $1" in q:
            return self._store.credentials.get(str(args[0]))
        if "FROM audit_log_chain" in q and "ORDER BY log_index DESC" in q:
            if not self._store.audit_log_chain:
                return None
            last = self._store.audit_log_chain[-1]
            return {"log_index": last["log_index"], "current_hash": last["current_hash"]}
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        q = " ".join(query.split())
        if "FROM audit_log_chain" in q and "ORDER BY log_index ASC" in q:
            return [
                {
                    "log_index": row["log_index"],
                    "timestamp": row["timestamp"],
                    "actor_id": row["actor_id"],
                    "actor_role": row["actor_role"],
                    "action_type": row["action_type"],
                    "target_id": row["target_id"],
                    "event_payload_text": json.dumps(row["event_payload"]),
                    "prev_hash": row["prev_hash"],
                    "current_hash": row["current_hash"],
                }
                for row in self._store.audit_log_chain
            ]
        return []

    async def fetchval(self, query: str, *args: Any) -> Any:
        q = " ".join(query.split())
        if q.startswith("SELECT 1 FROM users WHERE user_id = $1"):
            return 1
        return None

    async def execute(self, query: str, *args: Any) -> str:
        q = " ".join(query.split())
        if q.startswith("INSERT INTO audit_log_chain"):
            self._store.audit_log_chain.append(
                {
                    "log_index": args[0],
                    "timestamp": args[1],
                    "actor_id": args[2],
                    "actor_role": args[3],
                    "action_type": args[4],
                    "target_id": args[5],
                    "event_payload": json.loads(args[6]),
                    "prev_hash": args[7],
                    "current_hash": args[8],
                }
            )
        elif q.startswith("INSERT INTO access_authorization_events"):
            self._store.authorization_events.append(
                {
                    "event_id": args[0],
                    "timestamp": args[1],
                    "credential_id": args[2],
                    "elevator_id": args[3],
                    "origin_floor": args[4],
                    "destination_floor": args[5],
                    "outcome": args[6],
                    "denial_reason": args[7],
                    "alert_triggered": args[8],
                }
            )
        elif q.startswith("INSERT INTO security_alerts"):
            self._store.security_alerts.append(
                {
                    "alert_id": args[0],
                    "severity": args[1],
                    "alert_type": args[2],
                    "credential_id": args[3],
                    "primary_car_id": args[4],
                    "secondary_car_id": args[5],
                    "details": args[6],
                    "status": args[7],
                    "created_at": args[8],
                }
            )
        elif q.startswith("INSERT INTO credentials"):
            cid = str(args[0])
            self._store.credentials[cid] = {
                "credential_id": cid,
                "user_id": str(args[1]),
                "credential_type": str(args[2]),
                "status": str(args[3]),
                "suspension_reason": None,
                "suspension_start": None,
                "revocation_reason": None,
                "revoked_at": None,
                "expires_at": args[6],
                "allowed_floors": json.loads(args[7]),
                "issued_by_admin": str(args[8]),
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
                "full_name": "Dynamic User",
                "user_role": str(args[2]),
                "apartment_unit": None,
                "assigned_floor": None,
            }
        elif q.startswith("UPDATE credentials"):
            cid = str(args[0])
            if cid in self._store.credentials:
                self._store.credentials[cid]["status"] = args[1]
        return "INSERT 0 1"


class _FakeDbStore:
    def __init__(self) -> None:
        now = datetime.now(timezone.utc)
        all_floors = json.dumps([-1] + list(range(1, 31)))
        self.credentials: dict[str, dict[str, Any]] = {
            "ADMIN-CARD-0001": {
                "credential_id": "ADMIN-CARD-0001",
                "user_id": "00000000-0000-0000-0000-000000000001",
                "credential_type": "ADMIN",
                "status": "ACTIVE",
                "suspension_reason": None,
                "suspension_start": None,
                "revocation_reason": None,
                "revoked_at": None,
                "expires_at": None,
                "allowed_floors": all_floors,
                "issued_by_admin": "00000000-0000-0000-0000-000000000001",
                "created_at": now,
                "updated_at": now,
                "full_name": "Initial Admin",
                "user_role": "ADMIN",
                "apartment_unit": None,
                "assigned_floor": None,
            },
            "TECH-CARD-01": {
                "credential_id": "TECH-CARD-01",
                "user_id": "user-tech-1",
                "credential_type": "TECHNICIAN",
                "status": "ACTIVE",
                "suspension_reason": None,
                "suspension_start": None,
                "revocation_reason": None,
                "revoked_at": None,
                "expires_at": None,
                "allowed_floors": all_floors,
                "issued_by_admin": "00000000-0000-0000-0000-000000000001",
                "created_at": now,
                "updated_at": now,
                "full_name": "Technician One",
                "user_role": "TECHNICIAN",
                "apartment_unit": None,
                "assigned_floor": None,
            },
            "RES-CARD-15": {
                "credential_id": "RES-CARD-15",
                "user_id": "user-res-15",
                "credential_type": "RESIDENT",
                "status": "ACTIVE",
                "suspension_reason": None,
                "suspension_start": None,
                "revocation_reason": None,
                "revoked_at": None,
                "expires_at": None,
                "allowed_floors": json.dumps([-1, 1, 15]),
                "issued_by_admin": "00000000-0000-0000-0000-000000000001",
                "created_at": now,
                "updated_at": now,
                "full_name": "Resident 15",
                "user_role": "RESIDENT",
                "apartment_unit": "15B",
                "assigned_floor": 15,
            },
        }
        self.audit_log_chain: list[dict[str, Any]] = []
        self.authorization_events: list[dict[str, Any]] = []
        self.security_alerts: list[dict[str, Any]] = []


def _make_fake_pool(store: _FakeDbStore) -> type:
    class _FakeDbPool:
        @classmethod
        @asynccontextmanager
        async def acquire(cls) -> AsyncIterator[_FakeConn]:
            yield _FakeConn(store)

        @classmethod
        @asynccontextmanager
        async def transaction(cls) -> AsyncIterator[_FakeConn]:
            yield _FakeConn(store)

    return _FakeDbPool


def _reset_state() -> None:
    CabAccessControl.reset_in_memory_state()
    DashboardAccessControl.reset_in_memory_state()
    operational_simulator.reset(base_time=MORNING_PEAK_BASE_TIME)
    SimulationClock.reset(
        base_time=MORNING_PEAK_BASE_TIME,
        sim_seconds_per_tick=1.0,
        tick_interval_seconds=0.0,
        start_loop=False,
    )


async def _login(client: AsgiClient, card_id: str) -> str:
    resp = await client.post(
        "/api/v1/acs/auth/dashboard/login",
        json_body={"credential_id": card_id},
    )
    assert resp.status_code == 200
    return resp.json()["session_token"]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestVerificationScenarios(unittest.TestCase):
    """Explicit compliance verification for SRS Table 9 scenarios."""

    def test_vt21_repeated_emergency_triggers_and_false_alarm(self) -> None:
        """VT-21: Four triggers within 60s create 1 low-severity alert; Admin closes FALSE_ALARM audited."""
        async def scenario() -> None:
            _reset_state()
            store = _FakeDbStore()
            app.dependency_overrides[get_db_pool] = lambda: _make_fake_pool(store)
            try:
                client = AsgiClient(app)
                admin_token = await _login(client, "ADMIN-CARD-0001")

                # Four emergency triggers on E2 within 60 seconds
                incidents = []
                for _ in range(4):
                    r = await client.post(
                        "/api/v1/operational/emergency/trigger",
                        json_body={"elevator_id": "E2", "option": "HELP"},
                    )
                    self.assertEqual(r.status_code, 201)
                    incidents.append(r.json()["incident"])

                # Verify 4 triggers are recorded as valid incidents
                self.assertEqual(len(incidents), 4)

                # Exactly one low-severity REPEATED_EMERGENCY incident is created
                repeated = [
                    i for i in operational_simulator.list_incidents()
                    if i["kind"] == "REPEATED_EMERGENCY" and i["elevator_id"] == "E2"
                ]
                self.assertEqual(len(repeated), 1)
                self.assertEqual(repeated[0]["severity"], "LOW")

                # Admin resolves/closes the incident as FALSE_ALARM
                target_id = incidents[0]["incident_id"]
                close_resp = await client.post(
                    "/api/v1/operational/incidents/action",
                    headers=_bearer(admin_token),
                    json_body={"incident_id": target_id, "action": "FALSE_ALARM"},
                )
                self.assertEqual(close_resp.status_code, 200)
                self.assertEqual(close_resp.json()["incident"]["status"], "FALSE_ALARM")

                # Verify audited closure
                self.assertTrue(
                    any(
                        e["action_type"] == AuditActionType.FALSE_ALARM_CLOSED.value
                        and e["target_id"] == target_id
                        for e in store.audit_log_chain
                    )
                )
            finally:
                app.dependency_overrides.pop(get_db_pool, None)

        asyncio.run(scenario())

    def test_vt22_sensor_fault_obstruction_and_position(self) -> None:
        """VT-22: Stuck obstruction inhibits close and escalates after 10s. Silent position sensor marks STALE."""
        async def scenario() -> None:
            _reset_state()
            store = _FakeDbStore()
            app.dependency_overrides[get_db_pool] = lambda: _make_fake_pool(store)
            try:
                client = AsgiClient(app)
                admin_token = await _login(client, "ADMIN-CARD-0001")

                # 1. Obstruction active on E1 inhibits close command
                await client.post(
                    "/api/v1/operational/stub/obstruction",
                    headers=_bearer(admin_token),
                    json_body={"elevator_id": "E1", "active": True},
                )
                close_try = await client.post(
                    "/api/v1/operational/doors",
                    json_body={"elevator_id": "E1", "action": "CLOSE"},
                )
                self.assertEqual(close_try.status_code, 409)

                # Persisting obstruction >= 10s triggers escalation incident
                operational_simulator.advance(10)
                obs_incidents = [
                    i for i in operational_simulator.list_incidents()
                    if i["kind"] == "DOOR_OBSTRUCTION" and i["elevator_id"] == "E1"
                ]
                self.assertEqual(len(obs_incidents), 1)

                # 2. Position sensor loss / communication loss makes car state STALE
                comm_resp = await client.post(
                    "/api/v1/operational/stub/communication-loss",
                    headers=_bearer(admin_token),
                    json_body={"elevator_id": "E2"},
                )
                self.assertEqual(comm_resp.status_code, 200)
                self.assertEqual(operational_simulator.cars["E2"].availability, "STALE")
                self.assertFalse(operational_simulator._eligible(operational_simulator.cars["E2"]))
            finally:
                app.dependency_overrides.pop(get_db_pool, None)

        asyncio.run(scenario())

    def test_vt27_escalation_and_concurrent_incidents(self) -> None:
        """VT-27: Unresolved incident escalates after threshold. Concurrent fire and stuck car preserves stuck incident."""
        async def scenario() -> None:
            _reset_state()
            store = _FakeDbStore()
            app.dependency_overrides[get_db_pool] = lambda: _make_fake_pool(store)
            try:
                client = AsgiClient(app)
                admin_token = await _login(client, "ADMIN-CARD-0001")

                # 1. Stuck car on E1
                help_resp = await client.post(
                    "/api/v1/operational/emergency/trigger",
                    json_body={"elevator_id": "E1", "option": "HELP"},
                )
                help_id = help_resp.json()["incident"]["incident_id"]
                await client.post(
                    "/api/v1/operational/incidents/confirm-stuck",
                    headers=_bearer(admin_token),
                    json_body={"incident_id": help_id},
                )
                self.assertEqual(operational_simulator.cars["E1"].availability, "EMERGENCY_UNAVAILABLE")

                # Advance 60s -> escalation recorded
                operational_simulator.advance(60)
                stuck_inc = operational_simulator.incidents[help_id]
                self.assertTrue(stuck_inc.escalated)
                self.assertIn("ESCALATED", stuck_inc.history)

                # 2. Concurrent FIRE incident injected
                fire_resp = await client.post(
                    "/api/v1/operational/stub/building-event",
                    headers=_bearer(admin_token),
                    json_body={"kind": "FIRE"},
                )
                fire_id = fire_resp.json()["incident"]["incident_id"]

                # Under building-wide fire, all cars are EMERGENCY_UNAVAILABLE
                for eid in operational_simulator.ELEVATOR_IDS:
                    self.assertEqual(operational_simulator.cars[eid].availability, "EMERGENCY_UNAVAILABLE")

                # Resolve FIRE -> other cars recover, but E1 stuck car incident remains OPEN
                await client.post(
                    "/api/v1/operational/incidents/action",
                    headers=_bearer(admin_token),
                    json_body={"incident_id": fire_id, "action": "RESOLVE", "recovery_confirmed": True},
                )
                self.assertEqual(operational_simulator.cars["E1"].availability, "EMERGENCY_UNAVAILABLE")
                self.assertEqual(operational_simulator.cars["E2"].availability, "IN_SERVICE")
                self.assertEqual(stuck_inc.status, "OPEN")
            finally:
                app.dependency_overrides.pop(get_db_pool, None)

        asyncio.run(scenario())

    def test_vt30_help_request_dispatch_eligibility_and_stuck_transition(self) -> None:
        """VT-30: HELP creates incident/alert without removing car from dispatch. 5 presses keep car serving until confirmed stuck."""
        async def scenario() -> None:
            _reset_state()
            store = _FakeDbStore()
            app.dependency_overrides[get_db_pool] = lambda: _make_fake_pool(store)
            try:
                client = AsgiClient(app)
                admin_token = await _login(client, "ADMIN-CARD-0001")

                # 5 HELP presses on E1
                inc_ids = []
                for _ in range(5):
                    r = await client.post(
                        "/api/v1/operational/emergency/trigger",
                        json_body={"elevator_id": "E1", "option": "HELP"},
                    )
                    inc_ids.append(r.json()["incident"]["incident_id"])

                # Car E1 remains IN_SERVICE and eligible for dispatch
                self.assertEqual(operational_simulator.cars["E1"].availability, "IN_SERVICE")
                self.assertTrue(operational_simulator._eligible(operational_simulator.cars["E1"]))

                # Submit hall call and ensure E1 can be assigned
                hall_call = (
                    await client.post(
                        "/api/v1/operational/hall-calls",
                        json_body={"origin_floor": 2, "direction": "UP", "call_type": "ORDINARY"},
                    )
                ).json()["request"]
                self.assertEqual(hall_call["assigned_car"], "E1")

                # Once Admin confirms stuck, car transitions to EMERGENCY_UNAVAILABLE and hall call requeued
                await client.post(
                    "/api/v1/operational/incidents/confirm-stuck",
                    headers=_bearer(admin_token),
                    json_body={"incident_id": inc_ids[0]},
                )
                self.assertEqual(operational_simulator.cars["E1"].availability, "EMERGENCY_UNAVAILABLE")

                # Hall call requeued to an eligible car
                updated_hall = operational_simulator.hall_calls[hall_call["request_id"]]
                self.assertNotEqual(updated_hall.assigned_car, "E1")
                self.assertIn(updated_hall.assigned_car, ("E2", "E3", "E4"))
            finally:
                app.dependency_overrides.pop(get_db_pool, None)

        asyncio.run(scenario())

    def test_vt31_one_presentation_and_visitor_pass_scope(self) -> None:
        """VT-31: Single presentation restricts to one floor selection. Visitor pass opens only named floors."""
        async def scenario() -> None:
            _reset_state()
            store = _FakeDbStore()
            app.dependency_overrides[get_db_pool] = lambda: _make_fake_pool(store)
            try:
                client = AsgiClient(app)

                # Swipe resident card in E1
                swipe_res = await client.post(
                    "/api/v1/acs/auth/cab/swipe",
                    json_body={"elevator_id": "E1", "credential_id": "RES-CARD-15", "origin_floor": 1},
                )
                self.assertEqual(swipe_res.status_code, 200)

                # First destination selection: floor 15 allowed
                sel1 = await client.post(
                    "/api/v1/acs/cab/destination",
                    json_body={"elevator_id": "E1", "origin_floor": 1, "requested_floor": 15},
                )
                self.assertEqual(sel1.status_code, 200)

                # Second destination without re-swipe: denied ONE_TAP_LIMIT_EXCEEDED
                sel2 = await client.post(
                    "/api/v1/acs/cab/destination",
                    json_body={"elevator_id": "E1", "origin_floor": 1, "requested_floor": 15},
                )
                self.assertEqual(sel2.status_code, 403)
                self.assertEqual(sel2.json()["reason_code"], DenialReason.ONE_TAP_LIMIT_EXCEEDED)

                # Visitor pass with allowed floors [1, 20]
                store.credentials["VISITOR-PASS-20"] = {
                    "credential_id": "VISITOR-PASS-20",
                    "user_id": "user-vis-20",
                    "credential_type": "VISITOR",
                    "status": "ACTIVE",
                    "suspension_reason": None,
                    "suspension_start": None,
                    "revocation_reason": None,
                    "revoked_at": None,
                    "expires_at": None,
                    "allowed_floors": json.dumps([-1, 1, 20]),
                    "issued_by_admin": "00000000-0000-0000-0000-000000000001",
                    "created_at": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc),
                    "full_name": "Visitor Bob",
                    "user_role": "VISITOR",
                    "apartment_unit": None,
                    "assigned_floor": None,
                }

                # Swipe visitor pass in E2
                v_swipe = await client.post(
                    "/api/v1/acs/auth/cab/swipe",
                    json_body={"elevator_id": "E2", "credential_id": "VISITOR-PASS-20", "origin_floor": 1},
                )
                self.assertEqual(v_swipe.status_code, 200)

                # Selecting unassigned floor 18 -> UNAUTHORIZED_FLOOR
                unauth_sel = await client.post(
                    "/api/v1/acs/cab/destination",
                    json_body={"elevator_id": "E2", "origin_floor": 1, "requested_floor": 18},
                )
                self.assertEqual(unauth_sel.status_code, 403)
                self.assertEqual(unauth_sel.json()["reason_code"], DenialReason.UNAUTHORIZED_FLOOR)
            finally:
                app.dependency_overrides.pop(get_db_pool, None)

        asyncio.run(scenario())

    def test_vt32_card_scanning_alert_and_command_conflict(self) -> None:
        """VT-32: Eleven distinct unregistered IDs within 5 min trigger CARD_SCANNING alert. Conflicting rescue dispatch rejected."""
        async def scenario() -> None:
            _reset_state()
            store = _FakeDbStore()
            app.dependency_overrides[get_db_pool] = lambda: _make_fake_pool(store)
            try:
                client = AsgiClient(app)
                admin_token = await _login(client, "ADMIN-CARD-0001")

                # Present 11 distinct unregistered card IDs in cab within 5 simulated minutes
                for i in range(1, 12):
                    unknown_id = f"UNKNOWN-CARD-{i:03d}"
                    resp = await client.post(
                        "/api/v1/acs/auth/cab/swipe",
                        json_body={"elevator_id": "E1", "credential_id": unknown_id, "origin_floor": 1},
                    )
                    self.assertEqual(resp.status_code, 401)
                    self.assertEqual(resp.json()["reason_code"], DenialReason.UNREGISTERED_CARD)

                # Card scanning alert emitted in security alerts
                scanning_alerts = [
                    a for a in store.security_alerts
                    if a.get("alert_type") == "CARD_SCANNING"
                ]
                self.assertEqual(len(scanning_alerts), 1)

                # Command conflict: rescue dispatch to a car in MAINTENANCE is rejected
                # 1. Put E1 into MAINTENANCE
                await client.post(
                    "/api/v1/acs/fleet/maintenance",
                    headers=_bearer(admin_token),
                    json_body={"elevator_id": "E1", "operation_type": "ENTER_MAINTENANCE"},
                )
                self.assertEqual(operational_simulator.cars["E1"].availability, "MAINTENANCE")

                # 2. Create medical incident on floor 10
                med = operational_simulator.phone_medical_incident(10)

                # 3. Dispatch rescue specifying E1 (which is in MAINTENANCE)
                rescue_try = await client.post(
                    "/api/v1/acs/fleet/rescue-dispatch",
                    headers=_bearer(admin_token),
                    json_body={
                        "incident_id": med["incident_id"],
                        "rescue_car_id": "E1",
                        "incident_floor": 10,
                        "dwell_wait_seconds": 120,
                    },
                )
                self.assertEqual(rescue_try.status_code, 409)
                self.assertEqual(rescue_try.json()["reason_code"], DenialReason.COMMAND_CONFLICT_REJECTED)
            finally:
                app.dependency_overrides.pop(get_db_pool, None)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
