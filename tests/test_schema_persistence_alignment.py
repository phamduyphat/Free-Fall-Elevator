"""Offline verification of seed.sql schema alignment with sync_to_db and SQL readers."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import unittest

from acs.logs import Logging
from analytics.export_engine import ExportEngine
from analytics.fleet_metrics import FleetMetrics
from analytics.security_metrics import SecurityMetrics
from analytics.traffic_metrics import TrafficMetrics
from simulation.operational import OperationalSimulator

REPO_ROOT = Path(__file__).resolve().parent.parent


def _parse_seed_tables(seed_sql: str) -> dict[str, set[str]]:
    tables: dict[str, set[str]] = {}
    for match in re.finditer(
        r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\((.*?)\n\);",
        seed_sql,
        flags=re.DOTALL | re.IGNORECASE,
    ):
        table_name = match.group(1)
        body = match.group(2)
        cols: set[str] = set()
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("--") or stripped.startswith("CHECK"):
                continue
            col_match = re.match(r"^([a-z_][a-z0-9_]*)\s+[A-Z]+", stripped)
            if col_match:
                cols.add(col_match.group(1))
        tables[table_name] = cols
    return tables


def _extract_sync_inserts(operational_py: str) -> list[tuple[str, list[str]]]:
    sync_start = operational_py.index("async def sync_to_db")
    sync_body = operational_py[sync_start:]
    inserts: list[tuple[str, list[str]]] = []
    for match in re.finditer(
        r"INSERT INTO\s+(\w+)\s*\((.*?)\)\s*VALUES",
        sync_body,
        flags=re.DOTALL | re.IGNORECASE,
    ):
        table_name = match.group(1)
        cols = [c.strip() for c in match.group(2).split(",") if c.strip()]
        inserts.append((table_name, cols))
    return inserts


class _FakeDbConn:
    def __init__(self) -> None:
        self.tables: dict[str, list[dict]] = {
            "hall_calls": [],
            "trips": [],
            "car_calls": [],
            "incidents": [],
            "car_downtime_intervals": [],
            "system_heartbeats": [],
            "access_authorization_events": [],
            "security_alerts": [],
            "audit_log_chain": [],
        }

    async def execute(self, query: str, *args) -> str:
        q = " ".join(query.split())
        if q.startswith("INSERT INTO hall_calls"):
            row = {
                "call_id": args[0],
                "origin_floor": args[1],
                "direction": args[2],
                "is_freight": args[3],
                "assigned_elevator_id": args[4],
                "status": args[5],
                "created_at": args[6],
                "assigned_at": args[7],
                "approaching_at": args[8],
                "arrived_at": args[9],
                "completed_at": args[10],
                "wait_time_seconds": args[11],
                "reassignments_count": args[12],
                "no_boarding": args[13],
                "cancellation_reason": args[14],
                "traffic_profile": args[15],
            }
            self._upsert("hall_calls", "call_id", row)
        elif q.startswith("INSERT INTO trips"):
            row = {
                "trip_id": args[0],
                "elevator_id": args[1],
                "origin_floor": args[2],
                "destination_floor": args[3],
                "departure_time": args[4],
                "arrival_time": args[5],
                "travel_time_seconds": args[6],
                "floors_traversed": args[7],
                "motor_starts": args[8],
                "traffic_profile": args[9],
                "is_service_run": args[10],
                "is_estimated": args[11],
            }
            if not any(r["trip_id"] == row["trip_id"] for r in self.tables["trips"]):
                self.tables["trips"].append(row)
        elif q.startswith("INSERT INTO car_calls"):
            row = {
                "call_id": args[0],
                "elevator_id": args[1],
                "destination_floor": args[2],
                "credential_id": args[3],
                "status": args[4],
                "created_at": args[5],
                "command_sent_at": None,
                "completed_at": args[6],
                "travel_time_seconds": args[7],
                "is_service_run": args[8],
                "cancellation_reason": args[9],
            }
            self._upsert("car_calls", "call_id", row)
        elif q.startswith("INSERT INTO incidents"):
            row = {
                "incident_id": args[0],
                "incident_type": args[1],
                "elevator_id": args[2],
                "floor": args[3],
                "status": args[4],
                "severity": args[5],
                "details": args[6],
                "created_at": args[7],
                "acknowledged_at": args[8],
                "resolved_at": args[9],
                "resolved_by": args[10],
                "rescue_car_id": args[11],
            }
            self._upsert("incidents", "incident_id", row)
        elif q.startswith("INSERT INTO car_downtime_intervals"):
            row = {
                "interval_id": args[0],
                "elevator_id": args[1],
                "state": args[2],
                "cause": args[3],
                "started_at": args[4],
                "ended_at": args[5],
                "duration_seconds": args[6],
                "is_ongoing": args[7],
                "technician_id": args[8],
            }
            self._upsert("car_downtime_intervals", "interval_id", row)
        elif q.startswith("INSERT INTO system_heartbeats"):
            self.tables["system_heartbeats"].append(
                {
                    "timestamp": args[0],
                    "status": args[1],
                    "active_elevators_count": args[2],
                    "active_requests_count": args[3],
                    "active_incidents_count": args[4],
                }
            )
        return "INSERT 0 1"

    def _upsert(self, table: str, key: str, row: dict) -> None:
        rows = self.tables[table]
        for idx, existing in enumerate(rows):
            if existing[key] == row[key]:
                rows[idx] = row
                return
        rows.append(row)

    async def fetch(self, query: str, *args) -> list[dict]:
        q = " ".join(query.split())
        if "FROM hall_calls" in q:
            if not args:
                return list(self.tables["hall_calls"])
            st, et = args[0], args[1]
            rows = [r for r in self.tables["hall_calls"] if st <= r["created_at"] <= et]
            if len(args) > 2:
                prof = args[2]
                rows = [r for r in rows if r["traffic_profile"] in (None, prof)]
            return rows
        if "FROM trips" in q:
            if not args:
                return list(self.tables["trips"])
            st, et = args[0], args[1]
            return [r for r in self.tables["trips"] if st <= r["departure_time"] <= et]
        if "FROM car_calls" in q:
            if not args:
                return list(self.tables["car_calls"])
            st, et = args[0], args[1]
            return [
                {
                    "call_id": r["call_id"],
                    "type": "CAR",
                    "origin": None,
                    "dest": r["destination_floor"],
                    "created_at": r["created_at"],
                    "arrived_at": r["command_sent_at"],
                    "completed_at": r["completed_at"],
                    "wait_seconds": None,
                    "status": r["status"],
                    "reassignments": 0,
                }
                for r in self.tables["car_calls"]
                if st <= r["created_at"] <= et
            ]
        if "FROM car_downtime_intervals" in q:
            if not args:
                return list(self.tables["car_downtime_intervals"])
            st, et = args[0], args[1]
            return [
                {**r, "reason": r["cause"]}
                for r in self.tables["car_downtime_intervals"]
                if r["started_at"] <= et and (r["ended_at"] is None or r["ended_at"] >= st)
            ]
        if "FROM system_heartbeats" in q:
            if not args:
                return list(self.tables["system_heartbeats"])
            st, et = args[0], args[1]
            return [r for r in self.tables["system_heartbeats"] if st <= r["timestamp"] <= et]
        if "FROM incidents" in q and "GROUP BY incident_type" in q:
            st, et = args[0], args[1]
            counts: dict[str, int] = {}
            for r in self.tables["incidents"]:
                if st <= r["created_at"] <= et:
                    counts[r["incident_type"]] = counts.get(r["incident_type"], 0) + 1
            return [{"incident_type": k, "cnt": v} for k, v in counts.items()]
        if "FROM incidents" in q:
            rows = []
            for r in self.tables["incidents"]:
                if args and isinstance(args[0], str) and "incident_type =" in q and r["incident_type"] != args[0]:
                    continue
                rows.append(
                    {
                        **r,
                        "emergency_type": r["incident_type"],
                        "kind": r["incident_type"],
                        "incident_floor": r["floor"],
                        "description": r["details"],
                    }
                )
            return rows
        return []

    async def fetchrow(self, query: str, *args) -> dict | None:
        q = " ".join(query.split())
        if "FROM hall_calls" in q and "FROM trips" in q:
            ts = [r["created_at"] for r in self.tables["hall_calls"]] + [
                r["departure_time"] for r in self.tables["trips"]
            ]
            if not ts:
                return {"start_ts": None, "end_ts": None}
            return {"start_ts": min(ts), "end_ts": max(ts)}
        return None


def _make_fake_pool(conn: _FakeDbConn):
    class _FakePool:
        @classmethod
        @asynccontextmanager
        async def acquire(cls):
            yield conn

    return _FakePool


class TestSchemaAndPersistenceAlignment(unittest.TestCase):
    def test_seed_sql_covers_every_column_written_by_sync_to_db(self) -> None:
        seed_sql = (REPO_ROOT / "seed.sql").read_text(encoding="utf-8")
        operational_py = (REPO_ROOT / "simulation" / "operational.py").read_text(encoding="utf-8")

        seed_tables = _parse_seed_tables(seed_sql)
        sync_inserts = _extract_sync_inserts(operational_py)
        self.assertEqual(len(sync_inserts), 6)

        for table_name, cols in sync_inserts:
            self.assertIn(table_name, seed_tables, f"Missing table {table_name} in seed.sql")
            missing = set(cols) - seed_tables[table_name]
            self.assertEqual(
                missing,
                set(),
                f"seed.sql table {table_name} is missing columns written by sync_to_db: {missing}",
            )

    def test_sql_readers_work_against_synced_rows(self) -> None:
        base = datetime(2026, 1, 1, 7, 0, 0)
        sim = OperationalSimulator(auto_advance=False)
        sim.reset(base_time=base)

        sim.submit_hall_call(origin_floor=4, direction="UP", traffic_profile="PEAK")
        sim.submit_car_call("E1", 10)
        sim.advance(20)
        sim.trigger_emergency("E1", "HELP")
        sim.isolate("E3", "MAINTENANCE")
        sim.advance(60)

        conn = _FakeDbConn()
        asyncio.run(sim.sync_to_db(conn))
        fake_pool = _make_fake_pool(conn)

        end = base + timedelta(seconds=80)

        async def run_queries():
            wait_rep = await TrafficMetrics.get_wait_time_kpis(base, end, "PEAK", db_pool=fake_pool)
            travel_rep = await TrafficMetrics.get_travel_time_kpis(base, end, db_pool=fake_pool)
            dist_rep = await TrafficMetrics.get_trip_distribution(base, end, db_pool=fake_pool)
            util_rep = await FleetMetrics.get_utilization_kpis(base, end, db_pool=fake_pool)
            down_rep = await FleetMetrics.get_downtime_summary(base, end, report_time=end, db_pool=fake_pool)
            sec_rep = await SecurityMetrics.get_security_kpis(base, end, db_pool=fake_pool)
            inc_list = await Logging.search_incidents(db_pool=fake_pool)
            export_res = await ExportEngine.export_run_report(
                "align-test", "JSON", True, start_time=base, end_time=end, db_pool=fake_pool
            )
            return wait_rep, travel_rep, dist_rep, util_rep, down_rep, sec_rep, inc_list, export_res

        wait_rep, travel_rep, dist_rep, util_rep, down_rep, sec_rep, inc_list, export_res = asyncio.run(
            run_queries()
        )

        self.assertEqual(wait_rep.total_calls, 1)
        self.assertEqual(wait_rep.completed_calls, 1)
        self.assertEqual(wait_rep.mean_wait_seconds, 3.0)
        self.assertGreaterEqual(travel_rep.total_trips, 1)
        self.assertGreaterEqual(dist_rep.up_trips_count, 1)
        self.assertEqual(util_rep["E3"].maintenance_seconds, 60.0)
        self.assertEqual(len(down_rep.ongoing_intervals), 1)
        self.assertEqual(down_rep.per_car_downtime_seconds["E3"], 60.0)
        self.assertEqual(sec_rep.total_incidents, 1)
        self.assertEqual(sec_rep.emergency_incidents_by_type, {"HELP_REQUEST": 1})
        self.assertEqual(len(inc_list), 1)
        self.assertEqual(inc_list[0]["incident_type"], "HELP_REQUEST")
        self.assertEqual(export_res.record_count, 2)

    def test_load_from_db_restores_simulator_state(self) -> None:
        base = datetime(2026, 1, 1, 7, 0, 0)
        sim = OperationalSimulator(auto_advance=False, load_from_db=False)
        sim.reset(base_time=base)

        hc, _ = sim.submit_hall_call(origin_floor=4, direction="UP", traffic_profile="PEAK")
        cc = sim.submit_car_call("E1", 10)
        sim.advance(20)
        inc = sim.trigger_emergency("E1", "HELP")
        sim.isolate("E3", "MAINTENANCE")
        sim.advance(10)

        conn = _FakeDbConn()
        asyncio.run(sim.sync_to_db(conn))

        restored = OperationalSimulator(auto_advance=False, load_from_db=False)
        before_load = datetime.now(timezone.utc)
        loaded = asyncio.run(restored.load_from_db(conn))
        after_load = datetime.now(timezone.utc)
        self.assertTrue(loaded)
        self.assertGreaterEqual(restored.base_time, before_load)
        self.assertLessEqual(restored.base_time, after_load)
        self.assertEqual(restored.sim_tick, 0)
        self.assertEqual(restored.cars["E1"].current_floor, 10)
        self.assertEqual(restored.cars["E3"].availability, "MAINTENANCE")
        self.assertIn(hc["request_id"], restored.hall_calls)
        self.assertEqual(restored.hall_calls[hc["request_id"]].status, "COMPLETED")
        self.assertIn(cc["request_id"], restored.car_calls)
        self.assertEqual(restored.car_calls[cc["request_id"]].status, "COMPLETED")
        self.assertIn(inc["incident_id"], restored.incidents)
        self.assertEqual(restored.incidents[inc["incident_id"]].kind, "HELP")
        self.assertEqual(len(restored.trips), len(sim.trips))
        self.assertEqual(len(restored.downtime_intervals), len(sim.downtime_intervals))
        self.assertEqual(len(restored.heartbeats), len(sim.heartbeats) + 1)
        self.assertEqual(restored._synced_trips_idx, len(restored.trips))
        self.assertEqual(restored._synced_heartbeats_idx, len(sim.heartbeats))

        empty_conn = _FakeDbConn()
        fresh = OperationalSimulator(auto_advance=False, load_from_db=False)
        self.assertFalse(asyncio.run(fresh.load_from_db(empty_conn)))


if __name__ == "__main__":
    unittest.main()

