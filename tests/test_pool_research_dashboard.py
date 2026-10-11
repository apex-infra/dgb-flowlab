"""M6.3 read-only research dashboard snapshot tests."""

import unittest

from flowlab.pool_research_dashboard import research_dashboard_snapshot
from flowlab.pool_verification_log import record_verification
from flowlab.pool_reconciliation_log import record_reconciliation
from tests.test_pool_experiment_research import ExperimentResearchTests


class ResearchDashboardTests(unittest.TestCase):

    def setUp(self):
        ExperimentResearchTests.setUp(self)

    def tearDown(self):
        ExperimentResearchTests.tearDown(self)

    def snapshot(self):
        return research_dashboard_snapshot(
            self.conn,
            experiment_id="research-exp",
            pool_id="test-pool",
            session_id="test-session",
        )

    def test_experiment_identity_preserved(self):
        result = self.snapshot()

        self.assertEqual(result["experiment"]["id"], "research-exp")
        self.assertEqual(result["experiment"]["state"], "CREATED")
        self.assertEqual(result["experiment"]["config_version"], 0)
        self.assertIsNone(result["experiment"]["config_hash"])

    def test_unverified_evidence_requires_attention(self):
        result = self.snapshot()

        self.assertEqual(result["participant"]["pool_id"], "test-pool")
        self.assertEqual(result["participant"]["session_id"], "test-session")
        self.assertFalse(result["research"]["review_ready"])
        self.assertEqual(result["research"]["recheck_required"], 1)
        self.assertTrue(result["operator_attention"]["required"])

    def test_supported_evidence_can_be_reviewed(self):
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

        result = self.snapshot()

        self.assertTrue(result["research"]["review_ready"])
        self.assertEqual(result["research"]["supported_observations"], 1)
        self.assertFalse(result["operator_attention"]["required"])
        self.assertFalse(result["authorization"]["financial_actions"])

    def test_snapshot_is_read_only(self):
        before = self.conn.total_changes

        self.snapshot()

        self.assertEqual(self.conn.total_changes, before)
        self.assertFalse(self.snapshot()["authorization"]["financial_actions"])


    def test_invalidated_evidence_requires_operator_attention(self):
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

        self.assertTrue(self.snapshot()["research"]["review_ready"])

        self.rpc.active_hash = "d" * 64

        reconciliation = record_reconciliation(
            self.conn,
            self.rpc,
            observation_id=self.observation_id,
        )

        self.assertEqual(reconciliation["state"], "INVALIDATED")

        result = self.snapshot()

        self.assertFalse(result["research"]["review_ready"])
        self.assertEqual(result["research"]["invalidated_observations"], 1)
        self.assertTrue(result["operator_attention"]["required"])
        self.assertFalse(result["authorization"]["financial_actions"])

    def test_experiment_approval_does_not_override_missing_evidence(self):
        self.conn.execute(
            """
            UPDATE experiments
            SET state = 'APPROVED',
                config_version = 1,
                config_hash = ?,
                approved_at = '2026-10-11T00:00:00+00:00'
            WHERE id = 'research-exp'
            """,
            ("a" * 64,),
        )

        result = self.snapshot()

        self.assertTrue(result["experiment"]["approved"])
        self.assertFalse(result["research"]["review_ready"])
        self.assertTrue(result["operator_attention"]["required"])
        self.assertFalse(
            result["authorization"]["automatic_experiment_approval"]
        )
        self.assertFalse(result["authorization"]["financial_actions"])
        self.assertFalse(result["authorization"]["transaction_broadcast"])

    def test_configuration_changes_are_reflected_without_approval(self):
        self.conn.execute(
            """
            UPDATE experiments
            SET state = 'CONFIGURED',
                config_version = 3,
                config_hash = ?
            WHERE id = 'research-exp'
            """,
            ("b" * 64,),
        )

        result = self.snapshot()

        self.assertEqual(result["experiment"]["state"], "CONFIGURED")
        self.assertEqual(result["experiment"]["config_version"], 3)
        self.assertEqual(result["experiment"]["config_hash"], "b" * 64)
        self.assertFalse(result["experiment"]["approved"])

    def test_unknown_participant_is_rejected(self):
        from flowlab.pool_research_dashboard import research_dashboard_snapshot

        with self.assertRaises(ValueError):
            research_dashboard_snapshot(
                self.conn,
                experiment_id="research-exp",
                pool_id="test-pool",
                session_id="missing-session",
            )


if __name__ == "__main__":
    unittest.main()
