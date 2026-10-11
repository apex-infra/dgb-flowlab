"""Read-only reconciliation of historical blockchain verification evidence."""

import unittest

from flowlab import pool_ledger
from flowlab.pool_verification_log import record_verification
from flowlab.pool_reconciliation import reconcile_verification
from flowlab.rpc import RpcError
from tests.test_pool_verifier import VerifierTests


class ReconciliationTests(unittest.TestCase):

    def setUp(self):
        VerifierTests.setUp(self)

    def tearDown(self):
        VerifierTests.tearDown(self)

    def record_verified(self):
        result = record_verification(
            self.conn,
            self.rpc,
            observation_id=self.observation_id,
        )
        self.assertEqual(result["state"], "VERIFIED")
        return result

    def reconcile(self):
        return reconcile_verification(
            self.conn,
            self.rpc,
            observation_id=self.observation_id,
        )

    def test_active_block_remains_current(self):
        self.record_verified()

        result = self.reconcile()

        self.assertEqual(result["state"], "CURRENT")

    def test_replaced_active_block_invalidates_evidence(self):
        self.record_verified()

        self.rpc.active_hash = "c" * 64

        result = self.reconcile()

        self.assertEqual(result["state"], "INVALIDATED")

    def test_orphaned_block_invalidates_evidence(self):
        self.record_verified()

        self.rpc.header["confirmations"] = -1

        result = self.reconcile()

        self.assertEqual(result["state"], "INVALIDATED")

    def test_rpc_failure_is_indeterminate(self):
        self.record_verified()

        def unavailable(height):
            raise RpcError("Node temporarily unavailable")

        self.rpc.get_block_hash = unavailable

        result = self.reconcile()

        self.assertEqual(result["state"], "INDETERMINATE")

    def test_missing_verified_history_is_indeterminate(self):
        result = self.reconcile()

        self.assertEqual(result["state"], "INDETERMINATE")

    def test_reconciliation_never_modifies_finances(self):
        self.record_verified()

        before = pool_ledger.journal_totals(
            self.conn,
            pool_id="test-pool",
        )

        result = self.reconcile()

        after = pool_ledger.journal_totals(
            self.conn,
            pool_id="test-pool",
        )

        self.assertEqual(result["state"], "CURRENT")
        self.assertEqual(before, after)

        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM pool_deposits"
            ).fetchone()[0],
            0,
        )


    def test_missing_transaction_membership(self):
        self.record_verified()

        self.rpc.raw["blockhash"] = "c" * 64

        result = self.reconcile()

        self.assertEqual(result["state"], "INDETERMINATE")

    def test_historical_output_amount_mismatch(self):
        self.record_verified()

        self.rpc.raw["vout"][0]["value"] = "9.00000000"

        result = self.reconcile()

        self.assertEqual(result["state"], "INDETERMINATE")

    def test_membership_not_established(self):
        self.record_verified()

        self.rpc.anchored_active = False

        result = self.reconcile()

        self.assertEqual(result["state"], "INDETERMINATE")


    def test_observation_changes_during_reconciliation(self):
        self.record_verified()

        original_lookup = self.rpc.get_block_hash
        changed = False

        def lookup_then_reorganize(height):
            nonlocal changed

            result = original_lookup(height)

            if not changed:
                changed = True
                self.conn.execute(
                    """
                    UPDATE pool_deposit_observations
                    SET blockhash = ?, confirmations = 1,
                        state = 'CONFIRMED'
                    WHERE id = ?
                    """,
                    ("c" * 64, self.observation_id),
                )

            return result

        self.rpc.get_block_hash = lookup_then_reorganize

        result = self.reconcile()

        self.assertEqual(result["state"], "INDETERMINATE")
        self.assertEqual(
            self.conn.execute(
                "SELECT blockhash FROM pool_deposit_observations"
            ).fetchone()[0],
            "c" * 64,
        )


    def test_observation_already_disagrees_with_history(self):
        self.record_verified()

        # The observation changed before reconciliation began.
        # Historical block evidence alone must not authorize CURRENT.
        self.conn.execute(
            """
            UPDATE pool_deposit_observations
            SET blockhash = ?, confirmations = 2,
                state = 'CONFIRMED'
            WHERE id = ?
            """,
            ("c" * 64, self.observation_id),
        )

        result = self.reconcile()

        self.assertEqual(result["state"], "INDETERMINATE")


    def test_reconciliation_events_are_immutable(self):
        import sqlite3

        verified = self.record_verified()

        cursor = self.conn.execute(
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
                "Historical evidence remains current",
                verified["blockhash"],
                "test-timestamp",
            ),
        )

        event_id = cursor.lastrowid

        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                UPDATE pool_reconciliation_events
                SET state='INVALIDATED'
                WHERE id=?
                """,
                (event_id,),
            )

        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                DELETE FROM pool_reconciliation_events
                WHERE id=?
                """,
                (event_id,),
            )

        event = self.conn.execute(
            """
            SELECT state, verification_event_id
            FROM pool_reconciliation_events
            WHERE id=?
            """,
            (event_id,),
        ).fetchone()

        self.assertEqual(event["state"], "CURRENT")
        self.assertEqual(
            event["verification_event_id"],
            verified["event_id"],
        )


    def test_reconciliation_rejects_cross_observation_reference(self):
        import sqlite3

        verified = self.record_verified()

        # Create a second valid observation belonging to this session.
        self.conn.execute(
            """
            INSERT INTO pool_deposit_observations(
                pool_id, session_id, txid, vout, amount_sats,
                address, confirmations, blockhash, state,
                first_seen_at, last_seen_at
            )
            VALUES (
                'test-pool', 'test-session', ?, 0, 125000000,
                'deposit-address', 3, ?, 'CONFIRMED', 't', 't'
            )
            """,
            ("d" * 64, "b" * 64),
        )

        other_id = self.conn.execute(
            """
            SELECT id FROM pool_deposit_observations
            WHERE txid = ?
            """,
            ("d" * 64,),
        ).fetchone()[0]

        # Attempt to attach observation A's verification evidence
        # to observation B's reconciliation history.
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO pool_reconciliation_events(
                    observation_id, verification_event_id,
                    state, reason, blockhash, reconciled_at
                )
                VALUES (?, ?, 'CURRENT', ?, ?, 't')
                """,
                (
                    other_id,
                    verified["event_id"],
                    "Incorrectly referenced historical evidence",
                    verified["blockhash"],
                ),
            )


    def test_current_reconciliation_rejects_wrong_blockhash(self):
        import sqlite3

        verified = self.record_verified()

        # CURRENT cannot reference block A while claiming block B.
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO pool_reconciliation_events(
                    observation_id, verification_event_id,
                    state, reason, blockhash, reconciled_at
                )
                VALUES (?, ?, 'CURRENT', ?, ?, 't')
                """,
                (
                    self.observation_id,
                    verified["event_id"],
                    "Incorrect historical block reference",
                    "c" * 64,
                ),
            )


if __name__ == "__main__":
    unittest.main()
