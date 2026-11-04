"""Access-control behavior tests."""

import unittest
from datetime import datetime, timezone

from srems import Direction, RequestStatus, Role, SREMS


class AccessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.system = SREMS()
        self.system.bootstrap_admin("admin-card", "admin")
        self.system.issue_credential(
            "admin-card", "resident-card", "resident", Role.PASSENGER, {4}
        )

    def test_hall_calls_and_b1_do_not_require_a_card(self) -> None:
        hall_request = self.system.request_hall_call(2, Direction.UP)
        b1_request = self.system.submit_car_call("E1", -1)
        self.assertEqual(hall_request.status, RequestStatus.ASSIGNED)
        self.assertEqual(b1_request.status, RequestStatus.QUEUED)
        self.assertEqual(b1_request.destination, -1)

    def test_unauthorized_destination_never_reaches_interface(self) -> None:
        before = len(self.system.interface.commands)
        request = self.system.submit_car_call("E1", 5, "resident-card")
        self.assertEqual(request.status, RequestStatus.REJECTED)
        self.assertEqual(len(self.system.interface.commands), before)
        self.assertEqual(self.system.audit.verify().valid, True)

    def test_inactive_card_is_denied_and_alerted_immediately(self) -> None:
        self.system.suspend_credential("admin-card", "resident-card", "LOST")
        request = self.system.submit_car_call("E1", 4, "resident-card")
        self.assertEqual(request.status, RequestStatus.REJECTED)
        self.assertEqual(self.system.alerts[-1]["event_type"], "inactive_credential_use")

    def test_credential_expiration_denies_destination(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        credential = self.system.access.credentials["resident-card"]
        credential.expires_at = now
        request = self.system.submit_car_call(
            "E1", 4, "resident-card", now=datetime(2026, 1, 2, tzinfo=timezone.utc)
        )
        self.assertEqual(request.status, RequestStatus.REJECTED)
        self.assertEqual(credential.status.value, "EXPIRED")
