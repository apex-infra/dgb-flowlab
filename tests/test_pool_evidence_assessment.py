"""Read-only assessment of recorded Mixer Lab blockchain evidence."""

import unittest

from flowlab.pool_verification_log import record_verification
from flowlab.pool_reconciliation_log import record_reconciliation
from tests.test_pool_verifier import VerifierTests


class EvidenceAssessmentTests(unittest.TestCase):

    def setUp(self):
        VerifierTests.setUp(self)

    def tearDown(self):
        VerifierTests.tearDown(self)

    def assess(self):
        from flowlab.pool_evidence_assessment import assess_evidence

        return assess_evidence(
            self.conn,
            observation_id=self.observation_id,
        )

    def test_no_verification_requires_recheck(self):
        self.assertEqual(
            self.assess()["state"],
            "RECHECK_REQUIRED",
        )

    def test_verified_without_reconciliation_requires_recheck(self):
        record_verification(
            self.conn,
            self.rpc,
            observation_id=self.observation_id,
        )

        self.assertEqual(
            self.assess()["state"],
            "RECHECK_REQUIRED",
        )

    def test_current_reconciliation_supports_evidence(self):
        verified = record_verification(
            self.conn,
            self.rpc,
            observation_id=self.observation_id,
        )

        reconciliation = record_reconciliation(
            self.conn,
            self.rpc,
            observation_id=self.observation_id,
        )

        result = self.assess()

        self.assertEqual(result["state"], "CURRENTLY_SUPPORTED")
        self.assertEqual(
            result["verification_event_id"],
            verified["event_id"],
        )
        self.assertEqual(
            result["reconciliation_event_id"],
            reconciliation["event_id"],
        )

    def test_invalidated_reconciliation_is_not_supported(self):
        record_verification(
            self.conn,
            self.rpc,
            observation_id=self.observation_id,
        )

        self.rpc.active_hash = "c" * 64

        record_reconciliation(
            self.conn,
            self.rpc,
            observation_id=self.observation_id,
        )

        self.assertEqual(
            self.assess()["state"],
            "INVALIDATED",
        )

    def test_later_pending_verification_requires_recheck(self):
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

        self.rpc.active_hash = "c" * 64

        record_verification(
            self.conn,
            self.rpc,
            observation_id=self.observation_id,
        )

        self.assertEqual(
            self.assess()["state"],
            "RECHECK_REQUIRED",
        )

    def test_later_indeterminate_reconciliation_requires_recheck(self):
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

        self.rpc.header["hash"] = "c" * 64

        record_reconciliation(
            self.conn,
            self.rpc,
            observation_id=self.observation_id,
        )

        self.assertEqual(
            self.assess()["state"],
            "RECHECK_REQUIRED",
        )

    def test_invalid_observation_id_rejected(self):
        with self.assertRaises(ValueError):
            self.assess_invalid(0)

    def assess_invalid(self, observation_id):
        from flowlab.pool_evidence_assessment import assess_evidence

        return assess_evidence(
            self.conn,
            observation_id=observation_id,
        )


    def test_new_verification_does_not_erase_invalidation(self):
        record_verification(
            self.conn,
            self.rpc,
            observation_id=self.observation_id,
        )

        self.rpc.active_hash = "c" * 64

        record_reconciliation(
            self.conn,
            self.rpc,
            observation_id=self.observation_id,
        )

        self.assertEqual(self.assess()["state"], "INVALIDATED")

        # Restore node evidence and create a new verification,
        # but deliberately do not reconcile that new event.
        self.rpc.active_hash = "b" * 64

        record_verification(
            self.conn,
            self.rpc,
            observation_id=self.observation_id,
        )

        self.assertEqual(
            self.assess()["state"],
            "RECHECK_REQUIRED",
        )

    def test_old_current_does_not_support_new_verification(self):
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

        self.assertEqual(
            self.assess()["state"],
            "CURRENTLY_SUPPORTED",
        )

        # A later verification requires its own reconciliation.
        record_verification(
            self.conn,
            self.rpc,
            observation_id=self.observation_id,
        )

        self.assertEqual(
            self.assess()["state"],
            "RECHECK_REQUIRED",
        )

    def test_current_audit_does_not_override_changed_observation(self):
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
            UPDATE pool_deposit_observations
            SET blockhash=?, confirmations=1, state='CONFIRMED'
            WHERE id=?
            """,
            ("c" * 64, self.observation_id),
        )

        self.assertEqual(
            self.assess()["state"],
            "RECHECK_REQUIRED",
        )


    def test_reconciliation_timestamp_precedes_verification(self):
        verified = record_verification(
            self.conn,
            self.rpc,
            observation_id=self.observation_id,
        )

        # Direct SQL simulates malformed historical audit ordering.
        # The timestamp is deliberately earlier than verification.
        self.conn.execute(
            """
            INSERT INTO pool_reconciliation_events(
                observation_id, verification_event_id,
                state, reason, blockhash, reconciled_at
            )
            VALUES (?, ?, 'CURRENT', ?, ?, ?)
            """,
            (
                self.observation_id,
                verified["event_id"],
                "Synthetic older reconciliation",
                verified["blockhash"],
                "2000-01-01T00:00:00+00:00",
            ),
        )

        self.assertEqual(
            self.assess()["state"],
            "RECHECK_REQUIRED",
        )


if __name__ == "__main__":
    unittest.main()
