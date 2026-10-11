"""Tests for derived Mixer Lab participant lifecycle status."""

import unittest

from flowlab.pool_lifecycle import session_snapshot
from flowlab.pool_verification_log import record_verification
from flowlab.pool_reconciliation_log import record_reconciliation
from tests.test_pool_verifier import VerifierTests


class PoolLifecycleTests(unittest.TestCase):

    def setUp(self):
        VerifierTests.setUp(self)

    def tearDown(self):
        VerifierTests.tearDown(self)

    def test_ready_address_without_deposits(self):
        self.conn.execute(
            "DELETE FROM pool_deposit_observations"
        )

        result = session_snapshot(
            self.conn,
            pool_id="test-pool",
            session_id="test-session",
        )

        self.assertEqual(result["state"], "AWAITING_DEPOSIT")
        self.assertEqual(result["observation_count"], 0)
        self.assertEqual(result["address_state"], "READY")

    def test_registered_session_without_address(self):
        # A second session has completed registration but no
        # deposit-address request.
        self.conn.execute(
            """
            INSERT INTO pool_sessions(
                id, pool_id, fee_basis_points, created_at
            )
            VALUES ('second-session', 'test-pool', 500, 't')
            """
        )

        self.conn.execute(
            """
            INSERT INTO pool_payout_instructions(
                session_id, position,
                payout_address, allocation_bps
            )
            VALUES ('second-session', 1, 'payout', 10000)
            """
        )

        self.conn.execute(
            """
            INSERT INTO pool_registration_seals(
                session_id, completed_at
            )
            VALUES ('second-session', 't')
            """
        )

        result = session_snapshot(
            self.conn,
            pool_id="test-pool",
            session_id="second-session",
        )

        self.assertEqual(result["state"], "ADDRESS_REQUIRED")
        self.assertIsNone(result["address_state"])

    def test_wrong_pool_rejected(self):
        with self.assertRaises(ValueError):
            session_snapshot(
                self.conn,
                pool_id="wrong-pool",
                session_id="test-session",
            )


    def test_incomplete_registration_blocks_progress(self):
        self.conn.execute(
            """
            INSERT INTO pool_sessions(
                id, pool_id, fee_basis_points, created_at
            )
            VALUES ('unsealed-session', 'test-pool', 500, 't')
            """
        )

        result = session_snapshot(
            self.conn,
            pool_id="test-pool",
            session_id="unsealed-session",
        )

        self.assertEqual(result["state"], "REGISTRATION_REQUIRED")
        self.assertFalse(result["registration_complete"])
        self.assertFalse(result["financial_actions_authorized"])

    def test_uncertain_address_requires_recovery(self):
        self.conn.execute(
            """
            INSERT INTO pool_sessions(
                id, pool_id, fee_basis_points, created_at
            )
            VALUES ('recovery-session', 'test-pool', 500, 't')
            """
        )
        self.conn.execute(
            """
            INSERT INTO pool_payout_instructions(
                session_id, position, payout_address, allocation_bps
            )
            VALUES ('recovery-session', 1, 'recovery-payout', 10000)
            """
        )
        self.conn.execute(
            """
            INSERT INTO pool_registration_seals(
                session_id, completed_at
            )
            VALUES ('recovery-session', 't')
            """
        )
        self.conn.execute(
            """
            INSERT INTO pool_deposit_addresses(
                session_id, wallet, request_label, address,
                state, created_at, updated_at
            )
            VALUES (
                'recovery-session', 'test-reserve',
                'recovery-label', NULL,
                'REQUESTING', 't', 't'
            )
            """
        )

        self.conn.execute(
            """
            UPDATE pool_deposit_addresses
            SET state = 'RECOVERY_REQUIRED',
                updated_at = 'recovery-t'
            WHERE session_id = 'recovery-session'
              AND state = 'REQUESTING'
            """
        )

        result = session_snapshot(
            self.conn,
            pool_id="test-pool",
            session_id="recovery-session",
        )

        self.assertEqual(result["state"], "ADDRESS_RECOVERY_REQUIRED")
        self.assertEqual(result["address_state"], "RECOVERY_REQUIRED")
        self.assertIsNone(result["deposit_address"])

    def test_unverified_observation_is_observing(self):
        self.conn.execute(
            """
            UPDATE pool_deposit_observations
            SET confirmations = 0,
                blockhash = NULL,
                state = 'UNCONFIRMED'
            WHERE id = ?
            """,
            (self.observation_id,),
        )

        result = session_snapshot(
            self.conn,
            pool_id="test-pool",
            session_id="test-session",
        )

        self.assertEqual(result["state"], "OBSERVING")
        self.assertEqual(result["observation_count"], 1)
        self.assertEqual(
            result["evidence_counts"]["RECHECK_REQUIRED"], 1
        )
        self.assertFalse(result["financial_actions_authorized"])

    def test_unknown_session_rejected(self):
        with self.assertRaises(ValueError):
            session_snapshot(
                self.conn,
                pool_id="test-pool",
                session_id="nonexistent-session",
            )

    def test_snapshot_does_not_modify_database(self):
        before = self.conn.total_changes

        session_snapshot(
            self.conn,
            pool_id="test-pool",
            session_id="test-session",
        )

        self.assertEqual(self.conn.total_changes, before)


    def test_verified_reconciled_deposit_ready_for_review(self):
        verification = record_verification(
            self.conn,
            self.rpc,
            observation_id=self.observation_id,
        )

        self.assertEqual(verification["state"], "VERIFIED")

        reconciliation = record_reconciliation(
            self.conn,
            self.rpc,
            observation_id=self.observation_id,
        )

        self.assertEqual(reconciliation["state"], "CURRENT")

        result = session_snapshot(
            self.conn,
            pool_id="test-pool",
            session_id="test-session",
        )

        self.assertEqual(result["state"], "READY_FOR_REVIEW")
        self.assertEqual(
            result["evidence_counts"]["CURRENTLY_SUPPORTED"], 1
        )
        self.assertFalse(result["financial_actions_authorized"])

    def test_invalidated_evidence_requires_review(self):
        verification = record_verification(
            self.conn,
            self.rpc,
            observation_id=self.observation_id,
        )

        self.assertEqual(verification["state"], "VERIFIED")

        # Simulate a blockchain reorganization after verification.
        self.rpc.active_hash = "d" * 64

        reconciliation = record_reconciliation(
            self.conn,
            self.rpc,
            observation_id=self.observation_id,
        )

        self.assertEqual(reconciliation["state"], "INVALIDATED")

        result = session_snapshot(
            self.conn,
            pool_id="test-pool",
            session_id="test-session",
        )

        self.assertEqual(
            result["state"], "EVIDENCE_REVIEW_REQUIRED"
        )
        self.assertEqual(
            result["evidence_counts"]["INVALIDATED"], 1
        )
        self.assertFalse(result["financial_actions_authorized"])


    def test_new_pending_verification_revokes_ready_status(self):
        record_verification(
            self.conn, self.rpc,
            observation_id=self.observation_id,
        )
        record_reconciliation(
            self.conn, self.rpc,
            observation_id=self.observation_id,
        )

        before = session_snapshot(
            self.conn, pool_id="test-pool", session_id="test-session"
        )
        self.assertEqual(before["state"], "READY_FOR_REVIEW")

        self.rpc.active_hash = "d" * 64
        verification = record_verification(
            self.conn, self.rpc,
            observation_id=self.observation_id,
        )
        self.assertNotEqual(verification["state"], "VERIFIED")

        after = session_snapshot(
            self.conn, pool_id="test-pool", session_id="test-session"
        )
        self.assertEqual(after["state"], "OBSERVING")
        self.assertEqual(
            after["evidence_counts"]["RECHECK_REQUIRED"], 1
        )
        self.assertFalse(after["financial_actions_authorized"])

    def test_multiple_deposits_require_individual_evidence(self):
        record_verification(
            self.conn, self.rpc,
            observation_id=self.observation_id,
        )
        record_reconciliation(
            self.conn, self.rpc,
            observation_id=self.observation_id,
        )

        self.conn.execute(
            """
            INSERT INTO pool_deposit_observations(
                pool_id, session_id, txid, vout,
                amount_sats, address, confirmations,
                blockhash, state, first_seen_at, last_seen_at
            )
            VALUES (
                'test-pool', 'test-session', ?, 0,
                100000000, 'deposit-address', 0,
                NULL, 'UNCONFIRMED', 't', 't'
            )
            """,
            ("e" * 64,),
        )

        result = session_snapshot(
            self.conn, pool_id="test-pool", session_id="test-session"
        )

        self.assertEqual(result["state"], "OBSERVING")
        self.assertEqual(result["observation_count"], 2)
        self.assertEqual(
            result["evidence_counts"]["CURRENTLY_SUPPORTED"], 1
        )
        self.assertEqual(
            result["evidence_counts"]["RECHECK_REQUIRED"], 1
        )
        self.assertFalse(result["financial_actions_authorized"])

    def test_requesting_address_is_not_exposed(self):
        self.conn.execute(
            """
            INSERT INTO pool_sessions(
                id, pool_id, fee_basis_points, created_at
            )
            VALUES ('pending-session', 'test-pool', 500, 't')
            """
        )
        self.conn.execute(
            """
            INSERT INTO pool_payout_instructions(
                session_id, position, payout_address, allocation_bps
            )
            VALUES ('pending-session', 1, 'pending-payout', 10000)
            """
        )
        self.conn.execute(
            """
            INSERT INTO pool_registration_seals(
                session_id, completed_at
            )
            VALUES ('pending-session', 't')
            """
        )
        self.conn.execute(
            """
            INSERT INTO pool_deposit_addresses(
                session_id, wallet, request_label, address,
                state, created_at, updated_at
            )
            VALUES (
                'pending-session', 'test-reserve',
                'pending-label', NULL, 'REQUESTING', 't', 't'
            )
            """
        )

        result = session_snapshot(
            self.conn, pool_id="test-pool", session_id="pending-session"
        )

        self.assertEqual(
            result["state"], "ADDRESS_RECOVERY_REQUIRED"
        )
        self.assertEqual(result["address_state"], "REQUESTING")
        self.assertIsNone(result["deposit_address"])
        self.assertFalse(result["financial_actions_authorized"])


    def test_conflicted_observation_requires_review(self):
        self.conn.execute(
            """
            UPDATE pool_deposit_observations
            SET confirmations = -1,
                blockhash = NULL,
                state = 'CONFLICTED'
            WHERE id = ?
            """,
            (self.observation_id,),
        )

        result = session_snapshot(
            self.conn,
            pool_id="test-pool",
            session_id="test-session",
        )

        self.assertEqual(
            result["state"], "EVIDENCE_REVIEW_REQUIRED"
        )
        self.assertEqual(result["observation_count"], 1)
        self.assertEqual(
            result["observations"][0]["observation_state"],
            "CONFLICTED",
        )
        self.assertFalse(result["financial_actions_authorized"])

    def test_incomplete_payout_registration_stays_blocked(self):
        self.conn.execute(
            """
            INSERT INTO pool_sessions(
                id, pool_id, fee_basis_points, created_at
            )
            VALUES ('partial-session', 'test-pool', 500, 't')
            """
        )

        self.conn.execute(
            """
            INSERT INTO pool_payout_instructions(
                session_id, position, payout_address, allocation_bps
            )
            VALUES ('partial-session', 1, 'partial-payout', 5000)
            """
        )

        result = session_snapshot(
            self.conn,
            pool_id="test-pool",
            session_id="partial-session",
        )

        self.assertEqual(result["state"], "REGISTRATION_REQUIRED")
        self.assertFalse(result["registration_complete"])
        self.assertEqual(result["payout_count"], 1)
        self.assertFalse(result["financial_actions_authorized"])

    def test_invalid_session_identifiers_rejected(self):
        for invalid in ("", "   ", None, 123):
            with self.subTest(session_id=invalid):
                with self.assertRaises(ValueError):
                    session_snapshot(
                        self.conn,
                        pool_id="test-pool",
                        session_id=invalid,
                    )


if __name__ == "__main__":
    unittest.main()
