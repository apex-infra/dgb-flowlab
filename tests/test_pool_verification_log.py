"""Tests for append-only Mixer Lab verification evidence."""

import sqlite3
import unittest

from flowlab import pool_ledger
from flowlab.pool_verification_log import (
    record_verification,
    verification_history,
)
from tests.test_pool_verifier import VerifierTests


class VerificationLogTests(unittest.TestCase):

    def setUp(self):
        VerifierTests.setUp(self)

    def tearDown(self):
        VerifierTests.tearDown(self)

    def record(self):
        return record_verification(
            self.conn,
            self.rpc,
            observation_id=self.observation_id,
        )

    def test_verified_event_is_persisted(self):
        result = self.record()

        self.assertEqual(result["state"], "VERIFIED")

        history = verification_history(
            self.conn,
            observation_id=self.observation_id,
        )

        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["state"], "VERIFIED")
        self.assertEqual(history[0]["confirmations"], 3)
        self.assertEqual(history[0]["blockhash"], "b" * 64)

    def test_repeated_verification_appends_events(self):
        first = self.record()
        second = self.record()

        self.assertNotEqual(first["event_id"], second["event_id"])

        history = verification_history(
            self.conn,
            observation_id=self.observation_id,
        )

        self.assertEqual(len(history), 2)

    def test_history_cannot_be_updated(self):
        result = self.record()

        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                UPDATE pool_verification_events
                SET state = 'REJECTED'
                WHERE id = ?
                """,
                (result["event_id"],),
            )

    def test_history_cannot_be_deleted(self):
        result = self.record()

        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "DELETE FROM pool_verification_events WHERE id = ?",
                (result["event_id"],),
            )

    def test_pending_evidence_is_recorded(self):
        self.rpc.active_hash = "c" * 64

        result = self.record()

        self.assertEqual(result["state"], "PENDING")

        history = verification_history(
            self.conn,
            observation_id=self.observation_id,
        )

        self.assertEqual(history[0]["state"], "PENDING")

    def test_rejected_evidence_is_recorded(self):
        self.rpc.raw["vout"][0]["value"] = "9.00000000"

        result = self.record()

        self.assertEqual(result["state"], "REJECTED")

        history = verification_history(
            self.conn,
            observation_id=self.observation_id,
        )

        self.assertEqual(history[0]["state"], "REJECTED")

    def test_verification_does_not_modify_finances(self):
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

    def test_invalid_observation_id_rejected(self):
        with self.assertRaises(ValueError):
            record_verification(
                self.conn,
                self.rpc,
                observation_id=0,
            )


    def test_observation_changes_during_verification(self):
        from unittest.mock import patch

        from flowlab import pool_verification_log

        original_verify = pool_verification_log.verify_observation

        def verify_then_reorganize(conn, rpc, *, observation_id):
            result = original_verify(
                conn,
                rpc,
                observation_id=observation_id,
            )

            # Simulate a reorganization arriving after RPC verification
            # but before the verification event is persisted.
            conn.execute(
                """
                UPDATE pool_deposit_observations
                SET blockhash = ?, confirmations = 1,
                    state = 'CONFIRMED'
                WHERE id = ?
                """,
                ("c" * 64, observation_id),
            )

            return result

        with patch.object(
            pool_verification_log,
            "verify_observation",
            side_effect=verify_then_reorganize,
        ):
            with self.assertRaises(ValueError):
                self.record()

        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM pool_verification_events"
            ).fetchone()[0],
            0,
        )

        self.assertEqual(
            self.conn.execute(
                "SELECT blockhash FROM pool_deposit_observations"
            ).fetchone()[0],
            "c" * 64,
        )


if __name__ == "__main__":
    unittest.main()
