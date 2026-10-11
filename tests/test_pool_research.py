"""M6.1 read-only Mixer Lab research assessment tests."""

import unittest

from flowlab.pool_research import assess_research_session
from flowlab.pool_verification_log import record_verification
from flowlab.pool_reconciliation_log import record_reconciliation
from tests.test_pool_verifier import VerifierTests


class ResearchAssessmentTests(unittest.TestCase):

    def setUp(self):
        VerifierTests.setUp(self)

    def tearDown(self):
        VerifierTests.tearDown(self)

    def assess(self):
        return assess_research_session(
            self.conn,
            pool_id="test-pool",
            session_id="test-session",
        )

    def test_unverified_deposit_requires_recheck(self):
        result = self.assess()

        self.assertEqual(result["lifecycle_state"], "OBSERVING")
        self.assertFalse(result["research_review_ready"])
        self.assertEqual(result["recheck_required"], 1)
        self.assertFalse(result["financial_actions_authorized"])

    def test_verified_reconciled_deposit_supports_research_review(self):
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

        self.assertEqual(result["lifecycle_state"], "READY_FOR_REVIEW")
        self.assertTrue(result["research_review_ready"])
        self.assertEqual(result["supported_observations"], 1)
        self.assertFalse(result["financial_actions_authorized"])

    def test_assessment_is_read_only(self):
        before = self.conn.total_changes

        self.assess()

        self.assertEqual(self.conn.total_changes, before)


    def test_invalidated_evidence_revokes_research_readiness(self):
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

        self.assertFalse(result["research_review_ready"])
        self.assertEqual(result["lifecycle_state"], "EVIDENCE_REVIEW_REQUIRED")
        self.assertEqual(result["invalidated_observations"], 1)
        self.assertFalse(result["financial_actions_authorized"])

    def test_later_pending_verification_revokes_readiness(self):
        record_verification(
            self.conn, self.rpc,
            observation_id=self.observation_id,
        )
        record_reconciliation(
            self.conn, self.rpc,
            observation_id=self.observation_id,
        )

        self.assertTrue(self.assess()["research_review_ready"])

        self.rpc.active_hash = "d" * 64

        verification = record_verification(
            self.conn, self.rpc,
            observation_id=self.observation_id,
        )

        self.assertNotEqual(verification["state"], "VERIFIED")

        result = self.assess()

        self.assertFalse(result["research_review_ready"])
        self.assertEqual(result["recheck_required"], 1)
        self.assertFalse(result["financial_actions_authorized"])

    def test_conflicted_observation_never_research_ready(self):
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

        result = self.assess()

        self.assertFalse(result["research_review_ready"])
        self.assertEqual(result["lifecycle_state"], "EVIDENCE_REVIEW_REQUIRED")
        self.assertFalse(result["financial_actions_authorized"])

    def test_multiple_observations_require_complete_evidence(self):
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
                100000000, 'deposit-address',
                0, NULL, 'UNCONFIRMED', 't', 't'
            )
            """,
            ("e" * 64,),
        )

        result = self.assess()

        self.assertFalse(result["research_review_ready"])
        self.assertEqual(result["observation_count"], 2)
        self.assertEqual(result["supported_observations"], 1)
        self.assertEqual(result["recheck_required"], 1)
        self.assertFalse(result["financial_actions_authorized"])


if __name__ == "__main__":
    unittest.main()
