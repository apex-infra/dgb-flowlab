"""M7.1 read-only interrupted coordination recovery assessment tests."""

import unittest

from flowlab.pool_recovery_assessment import assess_session_recovery
from flowlab.pool_verification_log import record_verification
from flowlab.pool_reconciliation_log import record_reconciliation
from tests.test_pool_verifier import VerifierTests
from tests.test_pool_lifecycle import PoolLifecycleTests


class RecoveryAssessmentTests(unittest.TestCase):

    def setUp(self):
        VerifierTests.setUp(self)

    def tearDown(self):
        VerifierTests.tearDown(self)

    def assess(self):
        return assess_session_recovery(
            self.conn,
            pool_id="test-pool",
            session_id="test-session",
        )

    def test_missing_verification_requires_work(self):
        result = self.assess()

        self.assertEqual(result["state"], "ACTION_REQUIRED")
        self.assertEqual(result["observation_count"], 1)
        self.assertEqual(
            result["observations"][0]["recovery_action"],
            "VERIFICATION_REQUIRED",
        )
        self.assertFalse(result["financial_actions_authorized"])

    def test_verified_without_reconciliation_requires_work(self):
        record_verification(
            self.conn,
            self.rpc,
            observation_id=self.observation_id,
        )

        result = self.assess()

        self.assertEqual(
            result["observations"][0]["recovery_action"],
            "RECONCILIATION_REQUIRED",
        )
        self.assertEqual(result["state"], "ACTION_REQUIRED")

    def test_fully_supported_evidence_needs_no_recovery(self):
        record_verification(
            self.conn,
            self.rpc,
            observation_id=self.observation_id,
        )
        record_reconciliation(
            self.conn,
            self.rpc,
            observation_id=self.observation_id,
        )

        result = self.assess()

        self.assertEqual(result["state"], "NO_ACTION")
        self.assertEqual(
            result["observations"][0]["recovery_action"],
            "NO_ACTION",
        )
        self.assertFalse(result["financial_actions_authorized"])

    def test_assessment_is_read_only(self):
        before = self.conn.total_changes

        self.assess()

        self.assertEqual(self.conn.total_changes, before)


    def test_invalidated_evidence_requires_operator_action(self):
        record_verification(
            self.conn, self.rpc,
            observation_id=self.observation_id,
        )

        self.rpc.active_hash = "d" * 64

        reconciliation = record_reconciliation(
            self.conn, self.rpc,
            observation_id=self.observation_id,
        )

        self.assertEqual(reconciliation["state"], "INVALIDATED")

        result = self.assess()

        self.assertEqual(result["state"], "ACTION_REQUIRED")
        self.assertEqual(
            result["observations"][0]["recovery_action"],
            "EVIDENCE_INVALIDATED",
        )
        self.assertFalse(result["financial_actions_authorized"])

    def test_later_pending_verification_requires_recheck(self):
        record_verification(
            self.conn, self.rpc,
            observation_id=self.observation_id,
        )
        record_reconciliation(
            self.conn, self.rpc,
            observation_id=self.observation_id,
        )

        self.assertEqual(self.assess()["state"], "NO_ACTION")

        self.rpc.active_hash = "d" * 64

        verification = record_verification(
            self.conn, self.rpc,
            observation_id=self.observation_id,
        )

        self.assertNotEqual(verification["state"], "VERIFIED")

        result = self.assess()

        self.assertEqual(result["state"], "ACTION_REQUIRED")
        self.assertEqual(
            result["observations"][0]["recovery_action"],
            "VERIFICATION_RECHECK_REQUIRED",
        )

    def test_new_verification_requires_fresh_reconciliation(self):
        record_verification(
            self.conn, self.rpc,
            observation_id=self.observation_id,
        )
        record_reconciliation(
            self.conn, self.rpc,
            observation_id=self.observation_id,
        )

        self.assertEqual(self.assess()["state"], "NO_ACTION")

        record_verification(
            self.conn, self.rpc,
            observation_id=self.observation_id,
        )

        result = self.assess()

        self.assertEqual(result["state"], "ACTION_REQUIRED")
        self.assertEqual(
            result["observations"][0]["recovery_action"],
            "RECONCILIATION_REQUIRED",
        )

    def test_unknown_session_rejected(self):
        with self.assertRaises(ValueError):
            assess_session_recovery(
                self.conn,
                pool_id="test-pool",
                session_id="missing-session",
            )


    def test_empty_session_has_no_evidence_recovery_action(self):
        self.conn.execute(
            "DELETE FROM pool_deposit_observations WHERE id = ?",
            (self.observation_id,),
        )

        result = self.assess()

        self.assertEqual(result["observation_count"], 0)
        self.assertEqual(result["observations"], [])
        self.assertEqual(result["state"], "NO_ACTION")
        self.assertEqual(result["lifecycle_state"], "AWAITING_DEPOSIT")
        self.assertFalse(result["financial_actions_authorized"])

    def test_mixed_evidence_is_assessed_per_observation(self):
        record_verification(
            self.conn,
            self.rpc,
            observation_id=self.observation_id,
        )
        record_reconciliation(
            self.conn,
            self.rpc,
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
                100000000, 'deposit-address',
                0, NULL, 'UNCONFIRMED', 't', 't'
            )
            """,
            ("e" * 64,),
        )

        result = self.assess()

        self.assertEqual(result["state"], "ACTION_REQUIRED")
        self.assertEqual(result["observation_count"], 2)

        actions = {
            item["txid"]: item["recovery_action"]
            for item in result["observations"]
        }

        self.assertEqual(actions["a" * 64], "NO_ACTION")
        self.assertEqual(
            actions["e" * 64],
            "VERIFICATION_REQUIRED",
        )
        self.assertFalse(result["financial_actions_authorized"])


if __name__ == "__main__":
    unittest.main()
