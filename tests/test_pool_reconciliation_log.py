"""Tests for persistent Mixer Lab reconciliation evidence."""

import sqlite3
import unittest
from unittest.mock import patch

from flowlab import pool_ledger
from flowlab.pool_verification_log import record_verification
from tests.test_pool_verifier import VerifierTests


class ReconciliationLogTests(unittest.TestCase):

    def setUp(self):
        VerifierTests.setUp(self)

    def tearDown(self):
        VerifierTests.tearDown(self)

    def record_verified(self):
        return record_verification(
            self.conn,
            self.rpc,
            observation_id=self.observation_id,
        )

    def record(self):
        from flowlab.pool_reconciliation_log import record_reconciliation

        return record_reconciliation(
            self.conn,
            self.rpc,
            observation_id=self.observation_id,
        )

    def test_current_event_persisted(self):
        verified = self.record_verified()
        result = self.record()

        self.assertEqual(result["state"], "CURRENT")
        self.assertEqual(
            result["verification_event_id"],
            verified["event_id"],
        )

        row = self.conn.execute(
            "SELECT * FROM pool_reconciliation_events"
        ).fetchone()

        self.assertEqual(row["state"], "CURRENT")
        self.assertEqual(
            row["verification_event_id"],
            verified["event_id"],
        )

    def test_invalidated_event_persisted(self):
        self.record_verified()
        self.rpc.active_hash = "c" * 64

        result = self.record()

        self.assertEqual(result["state"], "INVALIDATED")

        row = self.conn.execute(
            "SELECT state FROM pool_reconciliation_events"
        ).fetchone()

        self.assertEqual(row["state"], "INVALIDATED")

    def test_missing_history_is_indeterminate(self):
        result = self.record()

        self.assertEqual(result["state"], "INDETERMINATE")

        row = self.conn.execute(
            "SELECT verification_event_id, state "
            "FROM pool_reconciliation_events"
        ).fetchone()

        self.assertIsNone(row["verification_event_id"])
        self.assertEqual(row["state"], "INDETERMINATE")

    def test_repeated_checks_append_events(self):
        self.record_verified()

        first = self.record()
        second = self.record()

        self.assertNotEqual(first["event_id"], second["event_id"])

        count = self.conn.execute(
            "SELECT COUNT(*) FROM pool_reconciliation_events"
        ).fetchone()[0]

        self.assertEqual(count, 2)

    def test_reconciliation_does_not_change_finances(self):
        self.record_verified()

        before = pool_ledger.journal_totals(
            self.conn,
            pool_id="test-pool",
        )

        self.record()

        after = pool_ledger.journal_totals(
            self.conn,
            pool_id="test-pool",
        )

        self.assertEqual(before, after)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM pool_deposits"
            ).fetchone()[0],
            0,
        )

    def test_stale_observation_cannot_be_recorded_as_current(self):
        from flowlab import pool_reconciliation_log

        self.record_verified()

        original = pool_reconciliation_log.reconcile_verification

        def reconcile_then_change(conn, rpc, *, observation_id):
            result = original(
                conn,
                rpc,
                observation_id=observation_id,
            )

            conn.execute(
                """
                UPDATE pool_deposit_observations
                SET blockhash=?, confirmations=1, state='CONFIRMED'
                WHERE id=?
                """,
                ("c" * 64, observation_id),
            )

            return result

        with patch.object(
            pool_reconciliation_log,
            "reconcile_verification",
            side_effect=reconcile_then_change,
        ):
            with self.assertRaises(ValueError):
                self.record()

        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM pool_reconciliation_events"
            ).fetchone()[0],
            0,
        )

    def test_invalid_observation_id_rejected(self):
        with self.assertRaises(ValueError):
            from flowlab.pool_reconciliation_log import record_reconciliation

            record_reconciliation(
                self.conn,
                self.rpc,
                observation_id=0,
            )


if __name__ == "__main__":
    unittest.main()
