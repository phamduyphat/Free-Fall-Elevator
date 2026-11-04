"""Audit persistence and integrity tests."""

import unittest

from srems import Direction, SREMS
from srems.storage import SQLiteStore


class AuditTests(unittest.TestCase):
    def test_hash_chain_detects_modified_record(self) -> None:
        store = SQLiteStore()
        system = SREMS(store=store)
        system.request_hall_call(3, Direction.UP)
        system._audit("operator", "test_action", "SUCCESS", {"value": 1})
        valid = system.audit.verify()
        self.assertTrue(valid.valid)
        self.assertGreaterEqual(valid.entries_checked, 1)

        store._connection.execute(
            "UPDATE audit_entries SET details = ? WHERE sequence = 1", ('{"changed":true}',)
        )
        result = system.audit.verify()
        self.assertFalse(result.valid)
        self.assertEqual(result.first_invalid_sequence, 1)

    def test_event_history_is_persistent_and_queryable(self) -> None:
        store = SQLiteStore()
        system = SREMS(store=store)
        system.request_hall_call(3, Direction.UP)
        events = store.events(limit=10)
        self.assertTrue(any(item["event_type"] == "hall_call_received" for item in events))

    def test_hash_chain_detects_removal_of_latest_record(self) -> None:
        store = SQLiteStore()
        system = SREMS(store=store)
        system.request_hall_call(3, Direction.UP)
        system._audit("operator", "test_action", "SUCCESS", {})
        latest = store.audit_rows()[-1][0]
        store._connection.execute(
            "DELETE FROM audit_entries WHERE sequence = ?", (latest,)
        )
        result = system.audit.verify()
        self.assertFalse(result.valid)
        self.assertEqual(result.first_invalid_sequence, latest)
