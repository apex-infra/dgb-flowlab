"""M7.2 explicitly invoked Mixer Lab coordination recovery tests."""

import unittest
from unittest.mock import patch

from flowlab.pool_recovery_coordinator import recover_session
from tests.test_pool_coordinator import CoordinatorTests


class RecoveryCoordinatorTests(unittest.TestCase):

    def setUp(self):
        CoordinatorTests.setUp(self)

    def tearDown(self):
        CoordinatorTests.tearDown(self)

    def recover(self):
        return recover_session(
            self.conn,
            self.rpc,
            pool_id="observer-pool",
            session_id="observer-session",
        )

    def test_completed_recovery_reports_before_and_after(self):
        result = self.recover()

        self.assertEqual(result["pass_state"], "COMPLETED")
        self.assertEqual(result["before"]["state"], "NO_ACTION")
        self.assertEqual(result["after"]["state"], "NO_ACTION")
        self.assertEqual(result["coordination"]["review_state"], "AWAITING_DEPOSIT")
        self.assertFalse(result["financial_actions_authorized"])

    def test_recovery_delegates_to_existing_coordinator(self):
        with patch(
            "flowlab.pool_recovery_coordinator.coordinate_session"
        ) as coordinate:
            coordinate.return_value = {
                "pass_state": "COMPLETED",
                "review_state": "AWAITING_DEPOSIT",
            }

            result = self.recover()

        coordinate.assert_called_once()
        self.assertEqual(result["pass_state"], "COMPLETED")

    def test_failed_coordination_propagates(self):
        with patch(
            "flowlab.pool_recovery_coordinator.coordinate_session",
            side_effect=RuntimeError("simulated interruption"),
        ):
            with self.assertRaises(RuntimeError):
                self.recover()

    def test_recovery_does_not_create_financial_records(self):
        before = self.conn.execute(
            "SELECT COUNT(*) FROM pool_deposits"
        ).fetchone()[0]

        self.recover()

        after = self.conn.execute(
            "SELECT COUNT(*) FROM pool_deposits"
        ).fetchone()[0]

        self.assertEqual(before, after)
        self.assertEqual(after, 0)


if __name__ == "__main__":
    unittest.main()
