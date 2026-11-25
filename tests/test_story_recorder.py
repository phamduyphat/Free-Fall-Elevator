"""Scenario player recordings (``simulation/story_recorder.py``).

Every scenario the player shows must come from the real simulator and meet all of its pass
criteria; the frames the player animates must be consistent with the events it explains.
Database-free. Run: ``python -m unittest tests.test_story_recorder``.
"""

from __future__ import annotations

from pathlib import Path
import unittest

from simulation import story_recorder

REPO = Path(__file__).resolve().parent.parent


class TestStoryRecorder(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = story_recorder.record_all()
        cls.stories = {s["key"]: s for s in cls.data["stories"]}

    def test_every_scenario_meets_all_its_pass_criteria(self):
        self.assertEqual(len(self.stories), len(story_recorder.STORIES))
        for key, s in self.stories.items():
            with self.subTest(key):
                self.assertTrue(s["criteria"])
                for c in s["criteria"]:
                    self.assertIsNotNone(c["passed_at"], c["text"])
                    self.assertIsNone(c["failed_at"], c["text"])
                    self.assertLessEqual(c["passed_at"], s["until"])

    def test_one_frame_per_simulated_second(self):
        for key, s in self.stories.items():
            with self.subTest(key):
                self.assertEqual([f["t"] for f in s["frames"]], list(range(s["until"] + 1)))
                for f in s["frames"]:
                    self.assertEqual(len(f["c"]), len(self.data["cars"]))

    def test_cars_move_at_most_one_floor_per_second(self):
        level = lambda f: 0 if f == -1 else f
        for key, s in self.stories.items():
            with self.subTest(key):
                for a, b in zip(s["frames"], s["frames"][1:]):
                    for ca, cb in zip(a["c"], b["c"]):
                        self.assertLessEqual(abs(level(ca[0]) - level(cb[0])), 1)

    def test_doors_never_open_while_moving(self):
        for key, s in self.stories.items():
            with self.subTest(key):
                for a, b in zip(s["frames"], s["frames"][1:]):
                    for ca, cb in zip(a["c"], b["c"]):
                        if ca[0] != cb[0]:
                            self.assertEqual(ca[2], "C", f"t={a['t']} car left floor {ca[0]} with doors {ca[2]}")

    def test_events_are_in_time_order_and_have_key_moments(self):
        for key, s in self.stories.items():
            with self.subTest(key):
                times = [e["t"] for e in s["events"]]
                self.assertEqual(times, sorted(times))
                self.assertTrue(any(e.get("caption") for e in s["events"]), "no key moment")
                self.assertTrue(all(e["lane"] in ("stub", "dispatch", "emergency", "maintenance", "audit")
                                    for e in s["events"]))

    def test_dispatch_explanations_match_the_chosen_car(self):
        for key, s in self.stories.items():
            for e in s["events"]:
                w = e.get("why") or {}
                if w.get("kind") != "decision" or w.get("call_type") == "RESCUE":
                    continue
                with self.subTest(key=key, t=e["t"]):
                    # FR-1.5: lowest cost, then fewer floors this month, then lower car number
                    best = min(w["candidates"], key=lambda c: (c["cost"], c["floors_month"], int(c["car"][1:])))
                    self.assertEqual(best["car"], w["chosen"])
                    for c in w["candidates"]:
                        self.assertLessEqual(c["penalty"], w["cap"] + 1e-9)

    def test_code_references_point_at_the_named_function(self):
        for key, refs in self.data["code"].items():
            for ref in refs:
                with self.subTest(code=key, fn=ref["fn"]):
                    lines = (REPO / ref["file"]).read_text(encoding="utf-8").splitlines()
                    self.assertIn(f"def {ref['fn'].split('.')[-1]}(", lines[ref["line"] - 1])

    def test_record_one_matches_record_all(self):
        one = story_recorder.record_one("medical")
        self.assertEqual(one["frames"], self.stories["medical"]["frames"])
        with self.assertRaises(KeyError):
            story_recorder.record_one("nope")


if __name__ == "__main__":
    unittest.main()
