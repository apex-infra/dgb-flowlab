"""M6.2 read-only experiment and Mixer Lab research reporting."""

import unittest

from flowlab import db
from flowlab.pool_experiment_research import experiment_research_report
from tests.test_pool_verifier import VerifierTests


class ExperimentResearchTests(unittest.TestCase):

    def setUp(self):
        VerifierTests.setUp(self)

        self.conn.execute(
            """
            INSERT INTO experiments(
                id, description, tags_json, operator_notes,
                state, config_version, created_at
            )
            VALUES (
                'research-exp', 'Research experiment', '[]',
                '', 'CREATED', 0, 't'
            )
            """
        )

    def tearDown(self):
        VerifierTests.tearDown(self)

    def report(self):
        return experiment_research_report(
            self.conn,
            experiment_id="research-exp",
            pool_id="test-pool",
            session_id="test-session",
        )

    def test_combined_research_report(self):
        result = self.report()

        self.assertEqual(result["experiment_id"], "research-exp")
        self.assertEqual(result["experiment_state"], "CREATED")
        self.assertEqual(result["config_version"], 0)
        self.assertIsNone(result["config_hash"])

        self.assertEqual(result["pool_id"], "test-pool")
        self.assertEqual(result["session_id"], "test-session")
        self.assertFalse(result["research_review_ready"])

        self.assertFalse(result["experiment_approved"])
        self.assertFalse(result["financial_actions_authorized"])

    def test_unknown_experiment_rejected(self):
        with self.assertRaises(ValueError):
            experiment_research_report(
                self.conn,
                experiment_id="missing-exp",
                pool_id="test-pool",
                session_id="test-session",
            )

    def test_report_is_read_only(self):
        before = self.conn.total_changes

        self.report()

        self.assertEqual(self.conn.total_changes, before)


    def test_configured_experiment_remains_unapproved(self):
        self.conn.execute(
            """
            UPDATE experiments
            SET state = 'CONFIGURED',
                config_version = 2,
                config_hash = ?
            WHERE id = 'research-exp'
            """,
            ("a" * 64,),
        )

        result = self.report()

        self.assertEqual(result["experiment_state"], "CONFIGURED")
        self.assertEqual(result["config_version"], 2)
        self.assertEqual(result["config_hash"], "a" * 64)
        self.assertFalse(result["experiment_approved"])
        self.assertFalse(result["financial_actions_authorized"])

    def test_approval_does_not_imply_research_readiness(self):
        self.conn.execute(
            """
            UPDATE experiments
            SET state = 'APPROVED',
                config_version = 1,
                config_hash = ?,
                approved_at = '2026-10-11T00:00:00+00:00'
            WHERE id = 'research-exp'
            """,
            ("b" * 64,),
        )

        result = self.report()

        self.assertTrue(result["experiment_approved"])
        self.assertFalse(result["research_review_ready"])
        self.assertFalse(result["financial_actions_authorized"])

    def test_approved_experiment_cannot_override_invalidated_evidence(self):
        from flowlab.pool_verification_log import record_verification
        from flowlab.pool_reconciliation_log import record_reconciliation

        self.conn.execute(
            """
            UPDATE experiments
            SET state = 'APPROVED',
                config_version = 1,
                config_hash = ?,
                approved_at = '2026-10-11T00:00:00+00:00'
            WHERE id = 'research-exp'
            """,
            ("c" * 64,),
        )

        record_verification(
            self.conn,
            self.rpc,
            observation_id=self.observation_id,
        )

        self.rpc.active_hash = "d" * 64

        record_reconciliation(
            self.conn,
            self.rpc,
            observation_id=self.observation_id,
        )

        result = self.report()

        self.assertTrue(result["experiment_approved"])
        self.assertFalse(result["research_review_ready"])
        self.assertEqual(result["invalidated_observations"], 1)
        self.assertFalse(result["financial_actions_authorized"])

    def test_unknown_participant_rejected(self):
        with self.assertRaises(ValueError):
            experiment_research_report(
                self.conn,
                experiment_id="research-exp",
                pool_id="test-pool",
                session_id="missing-session",
            )


if __name__ == "__main__":
    unittest.main()
