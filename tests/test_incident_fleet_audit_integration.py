"""Phase 2 verification: api/incident.py and api/operational.py routed through FleetAccessControl & audit_log_chain."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
from typing import Any, AsyncIterator, Optional

from acs.cab_accesscontrol import CabAccessControl
from acs.dashboard_accesscontrol import DashboardAccessControl
from acs.enums import AuditActionType, DenialReason
from acs.logs import Logging
from api import app
from db import DbConnectionPool, get_db_pool
from simulation.clock import MORNING_PEAK_BASE_TIME, SimulationClock
from simulation.operational import operational_simulator
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


def test_rescue_and_medical_lifecycle_through_incident_api_writes_audit_chain() -> None:
    async def scenario() -> None:
        _reset_state()
        store = _FakeDbStore()
        fake_pool = _make_fake_pool(store)
        app.dependency_overrides[get_db_pool] = lambda: fake_pool
        try:
            client = AsgiClient(app)
            admin_token = await _login(client, "ADMIN-CARD-0001")

            med_resp = await client.post(
                "/api/v1/operational/incidents/phone-medical",
                headers=_bearer(admin_token),
                json_body={"floor": 10},
            )
            assert med_resp.status_code == 200
            incident_id = med_resp.json()["incident"]["incident_id"]

            rescue_resp = await client.post(
                "/api/v1/operational/incidents/rescue",
                headers=_bearer(admin_token),
                json_body={"incident_id": incident_id, "confirmed_empty": True, "wait_seconds": 60},
            )
            assert rescue_resp.status_code == 200
            assert rescue_resp.json()["rescue_car_id"] == "E1"

            operational_simulator.advance(9)
            board_resp = await client.post(
                "/api/v1/operational/incidents/confirm-boarding",
                headers=_bearer(admin_token),
                json_body={"incident_id": incident_id},
            )
            assert board_resp.status_code == 200

            operational_simulator.advance(9)
            assert operational_simulator.cars["E1"].current_floor == 1

            ack_resp = await client.post(
                "/api/v1/operational/incidents/action",
                headers=_bearer(admin_token),
                json_body={"incident_id": incident_id, "action": "ACKNOWLEDGE"},
            )
            assert ack_resp.status_code == 200

            res_resp = await client.post(
                "/api/v1/operational/incidents/action",
                headers=_bearer(admin_token),
                json_body={
                    "incident_id": incident_id,
                    "action": "RESOLVE",
                    "recovery_confirmed": True,
                },
            )
            assert res_resp.status_code == 200
            assert operational_simulator.cars["E1"].availability == "IN_SERVICE"

            actions = [entry["action_type"] for entry in store.audit_log_chain]
            for expected in (
                AuditActionType.EMERGENCY_OVERRIDE.value,
                AuditActionType.RESCUE_CAR_DISPATCHED.value,
                AuditActionType.RESCUE_CAR_BOARDED.value,
                AuditActionType.INCIDENT_ACKNOWLEDGED.value,
                AuditActionType.INCIDENT_RESOLVED.value,
                AuditActionType.RETURN_TO_SERVICE.value,
            ):
                assert expected in actions

            integrity = await Logging.verify_audit_chain(db_pool=fake_pool)
            assert integrity.passed is True
        finally:
            app.dependency_overrides.pop(get_db_pool, None)

    asyncio.run(scenario())


def test_admin_can_cancel_rescue_car_and_release_to_service() -> None:
    async def scenario() -> None:
        _reset_state()
        store = _FakeDbStore()
        fake_pool = _make_fake_pool(store)
        app.dependency_overrides[get_db_pool] = lambda: fake_pool
        try:
            client = AsgiClient(app)
            admin_token = await _login(client, "ADMIN-CARD-0001")

            med_resp = await client.post(
                "/api/v1/operational/incidents/phone-medical",
                headers=_bearer(admin_token),
                json_body={"floor": 5},
            )
            incident_id = med_resp.json()["incident"]["incident_id"]

            assert (
                await client.post(
                    "/api/v1/operational/incidents/rescue",
                    headers=_bearer(admin_token),
                    json_body={"incident_id": incident_id, "confirmed_empty": True, "wait_seconds": 120},
                )
            ).status_code == 200
            assert operational_simulator.cars["E1"].rescue_active is True

            cancel_resp = await client.post(
                "/api/v1/operational/incidents/rescue/cancel",
                headers=_bearer(admin_token),
                json_body={"incident_id": incident_id, "reason": "Patient declined transport"},
            )
            assert cancel_resp.status_code == 200
            assert cancel_resp.json()["incident"]["status"] == "OPEN"
            assert cancel_resp.json()["incident"]["rescue_car_id"] is None
            assert operational_simulator.cars["E1"].rescue_active is False
            assert operational_simulator._eligible(operational_simulator.cars["E1"]) is True

            assert any(
                e["action_type"] == AuditActionType.RESCUE_CAR_RELEASED.value
                and e["event_payload"]["reason"] == "Patient declined transport"
                for e in store.audit_log_chain
            )
        finally:
            app.dependency_overrides.pop(get_db_pool, None)

    asyncio.run(scenario())


def test_technician_role_boundaries_and_fault_resolution_audit() -> None:
    async def scenario() -> None:
        _reset_state()
        store = _FakeDbStore()
        fake_pool = _make_fake_pool(store)
        app.dependency_overrides[get_db_pool] = lambda: fake_pool
        try:
            client = AsgiClient(app)
            tech_token = await _login(client, "TECH-CARD-01")

            # Passenger triggers HELP and MEDICAL
            help_inc = (
                await client.post(
                    "/api/v1/operational/emergency/trigger",
                    json_body={"elevator_id": "E1", "option": "HELP"},
                )
            ).json()["incident"]
            med_inc = (
                await client.post(
                    "/api/v1/operational/emergency/trigger",
                    json_body={"elevator_id": "E2", "option": "MEDICAL"},
                )
            ).json()["incident"]

            # Technician denied admin-only endpoints & admin-only incident resolution -> 403 + audited
            for path, body in (
                ("/api/v1/operational/incidents/confirm-stuck", {"incident_id": help_inc["incident_id"]}),
                ("/api/v1/operational/incidents/phone-medical", {"floor": 4}),
                (
                    "/api/v1/operational/incidents/rescue",
                    {"incident_id": med_inc["incident_id"], "confirmed_empty": True},
                ),
                (
                    "/api/v1/operational/incidents/action",
                    {"incident_id": med_inc["incident_id"], "action": "RESOLVE", "recovery_confirmed": True},
                ),
            ):
                resp = await client.post(path, headers=_bearer(tech_token), json_body=body)
                assert resp.status_code == 403

            unauth_audits = [
                e for e in store.audit_log_chain
                if e["action_type"] == AuditActionType.UNAUTHORIZED_CMD_REJECTED.value
            ]
            assert len(unauth_audits) >= 4

            # Technician CAN report, clear, acknowledge, and resolve a BLOCKING_FAULT
            fault_resp = await client.post(
                "/api/v1/operational/stub/fault",
                headers=_bearer(tech_token),
                json_body={"elevator_id": "E4", "fault_code": "DOOR_INTERLOCK"},
            )
            assert fault_resp.status_code == 200
            fault_id = fault_resp.json()["incident"]["incident_id"]
            assert operational_simulator.downtime_intervals[-1]["technician_id"] == "TECH-CARD-01"

            assert (
                await client.post(
                    "/api/v1/operational/stub/fault-clear",
                    headers=_bearer(tech_token),
                    json_body={"elevator_id": "E4"},
                )
            ).status_code == 200

            assert (
                await client.post(
                    "/api/v1/operational/incidents/action",
                    headers=_bearer(tech_token),
                    json_body={"incident_id": fault_id, "action": "ACKNOWLEDGE"},
                )
            ).status_code == 200

            assert (
                await client.post(
                    "/api/v1/operational/incidents/action",
                    headers=_bearer(tech_token),
                    json_body={
                        "incident_id": fault_id,
                        "action": "RESOLVE",
                        "recovery_confirmed": True,
                    },
                )
            ).status_code == 200
            assert operational_simulator.cars["E4"].availability == "IN_SERVICE"
            assert any(
                e["action_type"] == AuditActionType.RETURN_TO_SERVICE.value and e["target_id"] == "E4"
                for e in store.audit_log_chain
            )
        finally:
            app.dependency_overrides.pop(get_db_pool, None)

    asyncio.run(scenario())


def test_rescue_dispatch_command_conflict_and_precondition_safety() -> None:
    async def scenario() -> None:
        _reset_state()
        store = _FakeDbStore()
        fake_pool = _make_fake_pool(store)
        app.dependency_overrides[get_db_pool] = lambda: fake_pool
        try:
            client = AsgiClient(app)
            admin_token = await _login(client, "ADMIN-CARD-0001")

            med_resp = await client.post(
                "/api/v1/operational/incidents/phone-medical",
                headers=_bearer(admin_token),
                json_body={"floor": 8},
            )
            incident_id = med_resp.json()["incident"]["incident_id"]

            # Put E1 into MAINTENANCE via stub/state
            assert (
                await client.post(
                    "/api/v1/operational/stub/state",
                    headers=_bearer(admin_token),
                    json_body={"elevator_id": "E1", "state": "MAINTENANCE"},
                )
            ).status_code == 200

            # Explicit rescue dispatch to E1 (in MAINTENANCE) -> 409 COMMAND_CONFLICT_REJECTED & audited
            conflict_maint = await client.post(
                "/api/v1/operational/incidents/rescue",
                headers=_bearer(admin_token),
                json_body={
                    "incident_id": incident_id,
                    "confirmed_empty": True,
                    "wait_seconds": 60,
                    "rescue_car_id": "E1",
                },
            )
            assert conflict_maint.status_code == 409
            assert conflict_maint.json()["reason_code"] == DenialReason.COMMAND_CONFLICT_REJECTED

            # Explicit rescue dispatch to E5 -> 409 COMMAND_CONFLICT_REJECTED & audited
            conflict_e5 = await client.post(
                "/api/v1/operational/incidents/rescue",
                headers=_bearer(admin_token),
                json_body={
                    "incident_id": incident_id,
                    "confirmed_empty": True,
                    "wait_seconds": 60,
                    "rescue_car_id": "E5",
                },
            )
            assert conflict_e5.status_code == 409
            assert conflict_e5.json()["reason_code"] == DenialReason.COMMAND_CONFLICT_REJECTED

            conflicts = [
                e for e in store.audit_log_chain
                if e["action_type"] == AuditActionType.COMMAND_CONFLICT_REJECTED.value
            ]
            assert len(conflicts) == 2

            # Missing recovery_confirmed -> 400 CONFIRMATION_REQUIRED, no INCIDENT_RESOLVED written
            missing_confirm = await client.post(
                "/api/v1/operational/incidents/action",
                headers=_bearer(admin_token),
                json_body={"incident_id": incident_id, "action": "RESOLVE", "recovery_confirmed": False},
            )
            assert missing_confirm.status_code == 400
            assert missing_confirm.json()["reason_code"] == DenialReason.CONFIRMATION_REQUIRED
            assert operational_simulator.incidents[incident_id].status == "OPEN"
            assert not any(
                e["action_type"] == AuditActionType.INCIDENT_RESOLVED.value
                for e in store.audit_log_chain
            )
        finally:
            app.dependency_overrides.pop(get_db_pool, None)

    asyncio.run(scenario())


def test_admin_hall_call_cancel_and_false_alarm_audit() -> None:
    async def scenario() -> None:
        _reset_state()
        store = _FakeDbStore()
        fake_pool = _make_fake_pool(store)
        app.dependency_overrides[get_db_pool] = lambda: fake_pool
        try:
            client = AsgiClient(app)
            admin_token = await _login(client, "ADMIN-CARD-0001")

            hc = await client.post(
                "/api/v1/operational/hall-calls",
                json_body={"origin_floor": 12, "direction": "UP", "call_type": "ORDINARY"},
            )
            assert hc.status_code == 201
            req_id = hc.json()["request"]["request_id"]

            cancel = await client.post(
                "/api/v1/operational/hall-calls/cancel",
                headers=_bearer(admin_token),
                json_body={"request_id": req_id, "reason": "Passenger took stairs"},
            )
            assert cancel.status_code == 200
            assert cancel.json()["request"]["status"] == "CANCELLED"
            assert cancel.json()["request"]["cancellation_reason"] == "Passenger took stairs"
            assert any(
                e["action_type"] == AuditActionType.CALL_CANCELLED_BY_ADMIN.value
                and e["event_payload"]["reason"] == "Passenger took stairs"
                for e in store.audit_log_chain
            )

            help_inc = (
                await client.post(
                    "/api/v1/operational/emergency/trigger",
                    json_body={"elevator_id": "E3", "option": "HELP"},
                )
            ).json()["incident"]

            false_alarm = await client.post(
                "/api/v1/operational/incidents/action",
                headers=_bearer(admin_token),
                json_body={"incident_id": help_inc["incident_id"], "action": "FALSE_ALARM"},
            )
            assert false_alarm.status_code == 200
            assert false_alarm.json()["incident"]["status"] == "FALSE_ALARM"
            assert any(
                e["action_type"] == AuditActionType.FALSE_ALARM_CLOSED.value
                and e["target_id"] == help_inc["incident_id"]
                for e in store.audit_log_chain
            )
        finally:
            app.dependency_overrides.pop(get_db_pool, None)

    asyncio.run(scenario())


def test_malformed_external_inputs_audited_and_fail_closed() -> None:
    async def scenario() -> None:
        _reset_state()
        store = _FakeDbStore()
        fake_pool = _make_fake_pool(store)
        app.dependency_overrides[get_db_pool] = lambda: fake_pool
        try:
            client = AsgiClient(app)
            admin_token = await _login(client, "ADMIN-CARD-0001")

            bad_calls = [
                ("/api/v1/operational/hall-calls", {}, {"origin_floor": 31, "direction": "UP"}),
                ("/api/v1/operational/emergency/trigger", {}, {"elevator_id": "E9", "option": "HELP"}),
                ("/api/v1/operational/doors", {}, {"elevator_id": "E1", "action": "FORCE_CLOSE"}),
                ("/api/v1/operational/stub/fault", _bearer(admin_token), {"elevator_id": "E9", "fault_code": "X"}),
                (
                    "/api/v1/operational/incidents/action",
                    _bearer(admin_token),
                    {"incident_id": "inc-1", "action": "IGNORE"},
                ),
            ]
            for path, headers, body in bad_calls:
                resp = await client.post(path, headers=headers or None, json_body=body)
                assert resp.status_code == 400, f"{path} returned {resp.status_code}: {resp.text}"
                assert resp.json()["reason_code"] == DenialReason.MALFORMED_MSG_REJECTED

            malformed = [
                e for e in store.audit_log_chain
                if e["action_type"] == AuditActionType.MALFORMED_MSG_REJECTED.value
            ]
            assert len(malformed) == len(bad_calls)

            # Verify fail-closed when DB becomes unavailable while session exists
            app.dependency_overrides.pop(get_db_pool, None)
            previous_pool = DbConnectionPool._pool
            DbConnectionPool._pool = None
            try:
                down_resp = await client.post(
                    "/api/v1/operational/incidents/phone-medical",
                    headers=_bearer(admin_token),
                    json_body={"floor": 5},
                )
                assert down_resp.status_code == 503
                assert down_resp.json()["reason_code"] == DenialReason.DB_FAIL_CLOSED
                assert operational_simulator.incidents == {}
            finally:
                DbConnectionPool._pool = previous_pool
        finally:
            app.dependency_overrides.pop(get_db_pool, None)

    asyncio.run(scenario())



def test_fire_incident_across_restart_restores_service_even_after_prior_comm_loss() -> None:
    from tests.test_schema_persistence_alignment import _FakeDbConn

    async def scenario() -> None:
        _reset_state()
        store = _FakeDbStore()
        fake_pool = _make_fake_pool(store)
        app.dependency_overrides[get_db_pool] = lambda: fake_pool
        op_db = _FakeDbConn()
        try:
            client = AsgiClient(app)
            admin_token = await _login(client, "ADMIN-CARD-0001")

            # 1. Earlier communication loss on E1, synced while open, then reconciled and resolved
            comm_resp = await client.post(
                "/api/v1/operational/stub/communication-loss",
                headers=_bearer(admin_token),
                json_body={"elevator_id": "E1"},
            )
            assert comm_resp.status_code == 200
            comm_id = comm_resp.json()["incident"]["incident_id"]
            await operational_simulator.sync_to_db(op_db)

            assert (
                await client.post(
                    "/api/v1/operational/stub/reconcile",
                    headers=_bearer(admin_token),
                    json_body={"elevator_id": "E1"},
                )
            ).status_code == 200
            assert (
                await client.post(
                    "/api/v1/operational/incidents/action",
                    headers=_bearer(admin_token),
                    json_body={
                        "incident_id": comm_id,
                        "action": "RESOLVE",
                        "recovery_confirmed": True,
                    },
                )
            ).status_code == 200
            await operational_simulator.sync_to_db(op_db)

            # Closed STALE interval must have ended_at synced to DB
            assert op_db.tables["car_downtime_intervals"][0]["ended_at"] is not None

            # 2. Trigger FIRE and leave it OPEN across a simulated server restart
            fire_resp = await client.post(
                "/api/v1/operational/stub/building-event",
                headers=_bearer(admin_token),
                json_body={"kind": "FIRE"},
            )
            assert fire_resp.status_code == 200
            fire_id = fire_resp.json()["incident"]["incident_id"]
            await operational_simulator.sync_to_db(op_db)

            DashboardAccessControl.reset_in_memory_state()
            operational_simulator.reset(base_time=MORNING_PEAK_BASE_TIME)
            loaded = await operational_simulator.load_from_db(op_db)
            assert loaded is True
            assert all(
                c.availability == "EMERGENCY_UNAVAILABLE" and c.connected
                for c in operational_simulator.cars.values()
            )

            # 3. Re-login after restart, resolve and close FIRE -> all cars restore to IN_SERVICE
            admin_token_2 = await _login(client, "ADMIN-CARD-0001")
            assert (
                await client.post(
                    "/api/v1/operational/incidents/action",
                    headers=_bearer(admin_token_2),
                    json_body={
                        "incident_id": fire_id,
                        "action": "RESOLVE",
                        "recovery_confirmed": True,
                    },
                )
            ).status_code == 200
            assert (
                await client.post(
                    "/api/v1/operational/incidents/action",
                    headers=_bearer(admin_token_2),
                    json_body={"incident_id": fire_id, "action": "CLOSE"},
                )
            ).status_code == 200

            fleet = (await client.get("/api/v1/fleet/status")).json()["elevators"]
            assert all(c["state"] == "IN_SERVICE" and c["available"] is True for c in fleet)

            # 4. Sync and restart once more -> cars stay IN_SERVICE
            await operational_simulator.sync_to_db(op_db)
            operational_simulator.reset(base_time=MORNING_PEAK_BASE_TIME)
            await operational_simulator.load_from_db(op_db)
            assert all(
                c.availability == "IN_SERVICE" and operational_simulator._eligible(c)
                for c in operational_simulator.cars.values()
            )
        finally:
            app.dependency_overrides.pop(get_db_pool, None)

    asyncio.run(scenario())
