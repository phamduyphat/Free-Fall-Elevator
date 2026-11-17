"""Phase 1 verification: no unauthenticated car-call bypass and DB fail-closed with B1 exception."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
from typing import Any, AsyncIterator, Optional

from acs.cab_accesscontrol import CabAccessControl
from acs.enums import AuthorizationOutcome, DenialReason
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

    async def fetchval(self, query: str, *args: Any) -> Any:
        q = " ".join(query.split())
        if q.startswith("UPDATE credentials"):
            cid = str(args[0])
            rec = self._store.credentials.get(cid)
            if rec is None:
                return None
            rec["status"] = args[1]
            rec["suspension_reason"] = args[2]
            rec["suspension_start"] = args[3]
            return cid
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
        self.credentials: dict[str, dict[str, Any]] = {
            "RES-CARD-10": {
                "credential_id": "RES-CARD-10",
                "user_id": "user-res-10",
                "credential_type": "RESIDENT",
                "status": "ACTIVE",
                "suspension_reason": None,
                "suspension_start": None,
                "revocation_reason": None,
                "revoked_at": None,
                "expires_at": None,
                "allowed_floors": json.dumps([-1, 1, 10]),
                "issued_by_admin": "admin-1",
                "created_at": now,
                "updated_at": now,
                "full_name": "Resident Floor 10",
                "user_role": "RESIDENT",
                "apartment_unit": "10A",
                "assigned_floor": 10,
            }
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


def _reset_local_state() -> None:
    CabAccessControl.reset_in_memory_state()
    operational_simulator.reset(base_time=MORNING_PEAK_BASE_TIME)
    SimulationClock.reset(
        base_time=MORNING_PEAK_BASE_TIME,
        sim_seconds_per_tick=1.0,
        tick_interval_seconds=0.0,
        start_loop=False,
    )


def test_operational_car_calls_bypass_endpoint_is_removed() -> None:
    async def scenario() -> None:
        _reset_local_state()
        client = AsgiClient(app)

        resp = await client.post(
            "/api/v1/operational/car-calls",
            json_body={"elevator_id": "E1", "destination_floor": 15},
        )
        assert resp.status_code == 404
        assert operational_simulator.car_calls == {}
        assert operational_simulator.cars["E1"].stops == []

    asyncio.run(scenario())


def test_acs_blocks_unauthenticated_and_unauthorized_car_calls_without_moving_car() -> None:
    async def scenario() -> None:
        _reset_local_state()
        store = _FakeDbStore()
        fake_pool = _make_fake_pool(store)
        app.dependency_overrides[get_db_pool] = lambda: fake_pool
        try:
            client = AsgiClient(app)

            # 1. Restricted floor without card swipe -> 401 NO_ACTIVE_CREDENTIAL, no stop queued
            no_card = await client.post(
                "/api/v1/acs/cab/destination",
                json_body={"elevator_id": "E1", "origin_floor": 1, "requested_floor": 10},
            )
            assert no_card.status_code == 401
            assert no_card.json()["allowed"] is False
            assert no_card.json()["reason_code"] == DenialReason.NO_ACTIVE_CREDENTIAL
            assert operational_simulator.cars["E1"].stops == []
            assert operational_simulator.car_calls == {}

            # 2. Swipe Resident (floor 10), attempt restricted floor 15 -> 403 UNAUTHORIZED_FLOOR, no stop queued
            swipe_ok = await client.post(
                "/api/v1/acs/auth/cab/swipe",
                json_body={"elevator_id": "E1", "credential_id": "RES-CARD-10", "origin_floor": 1},
            )
            assert swipe_ok.status_code == 200
            assert swipe_ok.json()["allowed"] is True

            unauth_dest = await client.post(
                "/api/v1/acs/cab/destination",
                json_body={"elevator_id": "E1", "origin_floor": 1, "requested_floor": 15},
            )
            assert unauth_dest.status_code == 403
            assert unauth_dest.json()["allowed"] is False
            assert unauth_dest.json()["reason_code"] == DenialReason.UNAUTHORIZED_FLOOR
            assert operational_simulator.cars["E1"].stops == []
            operational_simulator.advance(15)
            assert operational_simulator.cars["E1"].current_floor == 1

            # Denial was logged to authorization events & audit chain
            assert any(
                ev["outcome"] == AuthorizationOutcome.DENIED
                and ev["denial_reason"] == DenialReason.UNAUTHORIZED_FLOOR
                and ev["destination_floor"] == 15
                for ev in store.authorization_events
            )
            assert any(
                entry["action_type"] == "ACCESS_DENIAL"
                and entry["event_payload"].get("requested_floor") == 15
                for entry in store.audit_log_chain
            )
        finally:
            app.dependency_overrides.pop(get_db_pool, None)

    asyncio.run(scenario())


def test_one_tap_and_expired_window_cannot_dispatch_car() -> None:
    async def scenario() -> None:
        _reset_local_state()
        store = _FakeDbStore()
        fake_pool = _make_fake_pool(store)
        app.dependency_overrides[get_db_pool] = lambda: fake_pool
        try:
            client = AsgiClient(app)

            assert (
                await client.post(
                    "/api/v1/acs/auth/cab/swipe",
                    json_body={"elevator_id": "E2", "credential_id": "RES-CARD-10", "origin_floor": 1},
                )
            ).status_code == 200

            # First authorized tap -> 200 and dispatches E2 to floor 10
            first_tap = await client.post(
                "/api/v1/acs/cab/destination",
                json_body={"elevator_id": "E2", "origin_floor": 1, "requested_floor": 10},
            )
            assert first_tap.status_code == 200
            assert len(operational_simulator.cars["E2"].stops) == 1
            assert operational_simulator.cars["E2"].stops[0].floor == 10

            # Second tap on same presentation -> 403 ONE_TAP_LIMIT_EXCEEDED, no second stop
            second_tap = await client.post(
                "/api/v1/acs/cab/destination",
                json_body={"elevator_id": "E2", "origin_floor": 1, "requested_floor": 10},
            )
            assert second_tap.status_code == 403
            assert second_tap.json()["reason_code"] == DenialReason.ONE_TAP_LIMIT_EXCEEDED
            assert len(operational_simulator.cars["E2"].stops) == 1

            # Expired 5s resident window on E3 -> 401 TOKEN_WINDOW_EXPIRED, no stop
            SimulationClock.advance(steps=11)  # Avoid 10s clone-detection window
            assert (
                await client.post(
                    "/api/v1/acs/auth/cab/swipe",
                    json_body={"elevator_id": "E3", "credential_id": "RES-CARD-10", "origin_floor": 1},
                )
            ).status_code == 200

            SimulationClock.advance(steps=6)
            expired_tap = await client.post(
                "/api/v1/acs/cab/destination",
                json_body={"elevator_id": "E3", "origin_floor": 1, "requested_floor": 10},
            )
            assert expired_tap.status_code == 401
            assert expired_tap.json()["reason_code"] == DenialReason.TOKEN_WINDOW_EXPIRED
            assert operational_simulator.cars["E3"].stops == []
        finally:
            app.dependency_overrides.pop(get_db_pool, None)

    asyncio.run(scenario())


def test_db_unavailable_fails_closed_for_restricted_floors_but_allows_b1_exception() -> None:
    async def scenario() -> None:
        _reset_local_state()
        previous_pool = DbConnectionPool._pool
        DbConnectionPool._pool = None
        app.dependency_overrides.pop(get_db_pool, None)
        try:
            client = AsgiClient(app)

            # Card swipe fails closed when DB is down -> 503 DB_FAIL_CLOSED
            swipe_down = await client.post(
                "/api/v1/acs/auth/cab/swipe",
                json_body={"elevator_id": "E1", "credential_id": "RES-CARD-10", "origin_floor": 1},
            )
            assert swipe_down.status_code == 503
            assert swipe_down.json()["allowed"] is False
            assert swipe_down.json()["reason_code"] == DenialReason.DB_FAIL_CLOSED

            # Restricted floor fails closed -> 503 DB_FAIL_CLOSED and no car stop queued
            restricted_down = await client.post(
                "/api/v1/acs/cab/destination",
                json_body={"elevator_id": "E1", "origin_floor": 1, "requested_floor": 10},
            )
            assert restricted_down.status_code == 503
            assert restricted_down.json()["allowed"] is False
            assert restricted_down.json()["reason_code"] == DenialReason.DB_FAIL_CLOSED
            assert operational_simulator.cars["E1"].stops == []

            # B1 exception on residential elevator E1 still works when DB is down (MNT-4.11, VT-26)
            b1_down = await client.post(
                "/api/v1/acs/cab/destination",
                json_body={"elevator_id": "E1", "origin_floor": 1, "requested_floor": -1},
            )
            assert b1_down.status_code == 200
            assert b1_down.json()["allowed"] is True
            assert len(operational_simulator.cars["E1"].stops) == 1
            assert operational_simulator.cars["E1"].stops[0].floor == -1

            # E5 to B1 remains forbidden even when DB is down
            e5_b1_down = await client.post(
                "/api/v1/acs/cab/destination",
                json_body={"elevator_id": "E5", "origin_floor": 1, "requested_floor": -1},
            )
            assert e5_b1_down.status_code == 403
            assert e5_b1_down.json()["allowed"] is False
            assert e5_b1_down.json()["reason_code"] == DenialReason.E5_B1_FORBIDDEN
            assert operational_simulator.cars["E5"].stops == []
        finally:
            DbConnectionPool._pool = previous_pool

    asyncio.run(scenario())

