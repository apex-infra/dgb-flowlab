"""M7.2 automatic Mixer Lab recovery eligibility tests."""

import unittest
from unittest.mock import patch

from flowlab.pool_recovery_supervisor import eligible_sessions, run_recovery_pass
from tests.test_pool_observer import ObserverTests


class RecoverySupervisorTests(unittest.TestCase):

    def setUp(self):
        ObserverTests.setUp(self)

    def tearDown(self):
        ObserverTests.tearDown(self)

    def activate_pool(self, state):
        self.conn.execute(
            "UPDATE pools SET state = ? WHERE id = ?",
            (state, "observer-pool"),
        )

    def test_paused_pool_is_excluded(self):
        self.assertEqual(eligible_sessions(self.conn), [])

    def test_retired_pool_is_excluded(self):
        self.activate_pool("RETIRED")
        self.assertEqual(eligible_sessions(self.conn), [])

    def test_active_ready_session_is_eligible(self):
        self.activate_pool("ACTIVE")

        self.assertEqual(
            eligible_sessions(self.conn),
            [{
                "pool_id": "observer-pool",
                "session_id": "observer-session",
            }],
        )

    def test_incomplete_registration_is_excluded(self):
        self.activate_pool("ACTIVE")

        from flowlab.pool_ledger import create_session

        create_session(
            self.conn,
            pool_id="observer-pool",
            session_id="incomplete-session",
            fee_basis_points=500,
        )

        self.assertEqual(
            eligible_sessions(self.conn),
            [{
                "pool_id": "observer-pool",
                "session_id": "observer-session",
            }],
        )

    def test_selector_is_read_only(self):
        self.activate_pool("ACTIVE")

        before = self.conn.total_changes

        eligible_sessions(self.conn)

        self.assertEqual(self.conn.total_changes, before)


    def add_uncertain_session(self, state):
        from flowlab.pool_registration import register_participant

        register_participant(
            self.conn,
            self.rpc,
            pool_id="observer-pool",
            session_id="uncertain-session",
            payouts=[
                {"address": "payout-uncertain", "allocation_bps": 10000}
            ],
        )

        self.conn.execute(
            """
            INSERT INTO pool_deposit_addresses(
                session_id, wallet, request_label,
                state, created_at, updated_at
            )
            VALUES (?, ?, ?, 'REQUESTING', ?, ?)
            """,
            (
                "uncertain-session",
                "observer-reserve",
                "uncertain-address-request",
                "2026-10-10T00:00:00Z",
                "2026-10-10T00:00:00Z",
            ),
        )

        if state == "RECOVERY_REQUIRED":
            self.conn.execute(
                """
                UPDATE pool_deposit_addresses
                SET state = 'RECOVERY_REQUIRED'
                WHERE session_id = ?
                """,
                ("uncertain-session",),
            )

    def test_requesting_address_is_excluded(self):
        self.activate_pool("ACTIVE")
        self.add_uncertain_session("REQUESTING")

        self.assertEqual(
            eligible_sessions(self.conn),
            [{
                "pool_id": "observer-pool",
                "session_id": "observer-session",
            }],
        )

    def test_recovery_required_address_is_excluded(self):
        self.activate_pool("ACTIVE")
        self.add_uncertain_session("RECOVERY_REQUIRED")

        self.assertEqual(
            eligible_sessions(self.conn),
            [{
                "pool_id": "observer-pool",
                "session_id": "observer-session",
            }],
        )


    def test_recovery_pass_processes_eligible_session(self):
        self.activate_pool("ACTIVE")

        with patch(
            "flowlab.pool_recovery_supervisor.recover_session"
        ) as recover:
            recover.return_value = {
                "pass_state": "COMPLETED",
                "financial_actions_authorized": False,
            }

            result = run_recovery_pass(self.conn, self.rpc)

        recover.assert_called_once()

        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertFalse(result["financial_actions_authorized"])

    def test_recovery_pass_skips_paused_pool(self):
        with patch(
            "flowlab.pool_recovery_supervisor.recover_session"
        ) as recover:
            result = run_recovery_pass(self.conn, self.rpc)

        recover.assert_not_called()
        self.assertEqual(result["completed"], 0)
        self.assertEqual(result["failed"], 0)

    def test_recovery_pass_reports_failure(self):
        self.activate_pool("ACTIVE")

        with patch(
            "flowlab.pool_recovery_supervisor.recover_session",
            side_effect=RuntimeError("simulated RPC failure"),
        ):
            result = run_recovery_pass(self.conn, self.rpc)

        self.assertEqual(result["completed"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertFalse(result["financial_actions_authorized"])


    def test_one_failure_does_not_block_other_sessions(self):
        from flowlab.pool_registration import register_participant
        from flowlab.pool_addresses import request_deposit_address

        self.activate_pool("ACTIVE")

        register_participant(
            self.conn,
            self.rpc,
            pool_id="observer-pool",
            session_id="second-session",
            payouts=[
                {"address": "second-payout", "allocation_bps": 10000}
            ],
        )

        from unittest.mock import patch

        with patch.object(
            self.rpc,
            "get_new_address",
            return_value="second-deposit-address",
        ):
            request_deposit_address(
                self.conn,
                self.rpc,
                pool_id="observer-pool",
                session_id="second-session",
            )

        attempts = []

        def simulate_recovery(conn, rpc, *, pool_id, session_id):
            attempts.append(session_id)

            if session_id == "observer-session":
                raise RuntimeError("Simulated RPC interruption")

            return {
                "pass_state": "COMPLETED",
                "coordination": {
                    "review_state": "AWAITING_DEPOSIT",
                },
                "financial_actions_authorized": False,
            }

        with patch(
            "flowlab.pool_recovery_supervisor.recover_session",
            side_effect=simulate_recovery,
        ):
            result = run_recovery_pass(self.conn, self.rpc)

        self.assertEqual(len(attempts), 2)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertFalse(result["financial_actions_authorized"])

        states = {
            item["session_id"]: item["state"]
            for item in result["results"]
        }

        self.assertEqual(states["observer-session"], "FAILED")
        self.assertEqual(states["second-session"], "COMPLETED")


if __name__ == "__main__":
    unittest.main()
