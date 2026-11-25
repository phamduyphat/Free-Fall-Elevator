"""User-defined scenarios (``simulation/custom_scenario.py``) and the full-load bypass they exposed.

Database-free. Run: ``python -m unittest tests.test_custom_scenario``.
"""

from __future__ import annotations

import unittest

from simulation import custom_scenario as cs
from simulation.operational import OperationalSimulator


def spec(**over):
    base = {
        "name": "t", "start": "08:00", "seed": 3,
        "groups": [{"count": 20, "from": "random", "to": "lobby", "start": 0, "over": 60}],
    }
    base.update(over)
    return base


class TestSpec(unittest.TestCase):
    def test_bad_specs_name_the_field(self):
        cases = [
            ({"groups": []}, "groups"),
            ({"groups": [{"count": 3, "from": 40}]}, "groups[1].from"),
            ({"groups": [{"count": 3, "from": 1, "to": 1}]}, "same single floor"),
            ({"groups": [{"count": 700}]}, "groups[1].count"),
            ({"groups": [{"count": 2, "from": -1, "to": 5, "kind": "freight"}]}, "B1"),
            ({"incidents": [{"type": "EXPLODE", "car": "E1"}]}, "incidents[1].type"),
            ({"cars": {"E5": {"floor": -1}}}, "cars.E5.floor"),
            ({"cars": {e: {"state": "MAINTENANCE"} for e in ("E1", "E2", "E3", "E4")}}, "at least one"),
            ({"start": "25:00"}, "start"),
        ]
        for over, needle in cases:
            with self.subTest(needle):
                with self.assertRaises(cs.SpecError) as ctx:
                    cs.normalise(spec(**over))
                self.assertIn(needle, str(ctx.exception))

    def test_floor_words_and_lists(self):
        n = cs.normalise(spec(groups=[{"count": 5, "from": "upper", "to": [12, "B1"]}]))
        self.assertEqual(n["groups"][0]["_from"], list(range(16, 31)))
        self.assertEqual(n["groups"][0]["_to"], [12, -1])

    def test_same_seed_same_people(self):
        n = cs.normalise(spec())
        self.assertEqual(cs.plan_people(n), cs.plan_people(n))
        self.assertNotEqual(cs.plan_people(n), cs.plan_people({**n, "seed": 4}))


class TestCompare(unittest.TestCase):
    def test_three_rules_same_people_everyone_delivered(self):
        story = cs.compare(cs.EXAMPLE)
        pols = story["comparison"]["policies"]
        self.assertEqual([p["key"] for p in pols], ["SREMS", "SREMS_NO_WEAR", "NEAREST"])
        for p in pols:
            self.assertEqual(p["kpis"]["delivered"], p["kpis"]["people"], p["key"])
        self.assertTrue(story["custom"])
        self.assertEqual([f["t"] for f in story["frames"]], list(range(story["until"] + 1)))
        safety = next(c for c in story["criteria"] if "doors open" in c["text"])
        self.assertIsNotNone(safety["passed_at"])
        self.assertTrue(story["comparison"]["verdict"])

    def test_srems_beats_nearest_car_on_a_busy_morning(self):
        story = cs.compare(spec(groups=[{"count": 60, "from": "random", "to": "lobby", "start": 0, "over": 180}]))
        k = {p["key"]: p["kpis"] for p in story["comparison"]["policies"]}
        self.assertLess(k["SREMS"]["avg_wait"], k["NEAREST"]["avg_wait"])

    def test_incidents_run_and_end(self):
        story = cs.compare(spec(incidents=[{"type": t, "car": "E2", "at": 10, "duration": 20}
                                           for t in ("MEDICAL", "MAINTENANCE")]
                                + [{"type": "FAULT", "car": "E3", "at": 15, "duration": 20}]))
        k = story["comparison"]["policies"][0]["kpis"]
        self.assertEqual(k["delivered"], k["people"])
        self.assertTrue(any(e["type"] == "Action" and "MEDICAL" in e["text"] for e in story["events"]))

    def test_freight_goes_by_e5(self):
        s, k = cs.run(cs.normalise(spec(groups=[{"count": 3, "from": 1, "to": [10, 20], "kind": "freight", "over": 20}])))
        self.assertEqual(k["delivered"], 3)
        self.assertTrue(all(j["car"] == "E5" for j in s.journeys))

    def test_failing_targets_are_reported_not_refused(self):
        story = cs.compare(spec(groups=[{"count": 120, "from": "random", "to": "lobby", "over": 30}],
                                targets={"avg_wait": 1, "p95_wait": 1}))
        failed = [c for c in story["criteria"] if c["passed_at"] is None]
        self.assertTrue(failed)


class TestFullLoadBypass(unittest.TestCase):
    """A full car used to be reassigned the call of the person it had just left behind,
    reopening at that floor forever (livelock found by a custom scenario)."""

    def test_lagging_car_under_wear_balancing_still_delivers_everyone(self):
        cars = {"E2": {"floor": 8, "floors_month": 456_000}}
        s, k = cs.run(cs.normalise({**cs.EXAMPLE, "cars": cars}), "SREMS")
        self.assertEqual(k["delivered"], k["people"])

    def test_full_car_takes_no_hall_calls_and_passes_pickups(self):
        sim = OperationalSimulator(auto_advance=False, load_from_db=False)
        sim.cars["E1"].current_floor = 10
        for eid in ("E2", "E3", "E4"):
            sim.cars[eid].current_floor = 30
        req, _ = sim.submit_hall_call(origin_floor=9, direction="DOWN")
        self.assertEqual(sim.hall_calls[req["request_id"]].assigned_car, "E1")
        sim.report_load("E1", 1.0)
        sim.advance(1)
        r = sim.hall_calls[req["request_id"]]
        self.assertNotEqual(r.assigned_car, "E1")
        self.assertEqual(r.reassignment_count, 1)
        self.assertTrue(any(e["event_type"] == "FULL_LOAD_BYPASS" for e in sim.events))
        req2, _ = sim.submit_hall_call(origin_floor=8, direction="DOWN")
        self.assertNotEqual(sim.hall_calls[req2["request_id"]].assigned_car, "E1")
        sim.report_load("E1", 0.5)
        self.assertFalse(sim._full(sim.cars["E1"]))

    def test_no_load_data_means_no_change(self):
        sim = OperationalSimulator(auto_advance=False, load_from_db=False)
        self.assertIsNone(sim.cars["E1"].load_fraction)
        self.assertFalse(sim._full(sim.cars["E1"]))


if __name__ == "__main__":
    unittest.main()
