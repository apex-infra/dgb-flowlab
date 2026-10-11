"""M5.2 coordinated Mixer Lab observation and evidence tests."""

import unittest

from flowlab.pool_coordinator import coordinate_session
from tests.test_pool_observer import ObserverTests


class CoordinatorTests(unittest.TestCase):

    def setUp(self):
        ObserverTests.setUp(self)

    def tearDown(self):
        ObserverTests.tearDown(self)

    def coordinate(self):
        return coordinate_session(
            self.conn,
            self.rpc,
            pool_id="observer-pool",
            session_id="observer-session",
        )

    def test_empty_scan_returns_awaiting_deposit(self):
        result = self.coordinate()

        self.assertEqual(result["observation"]["inserted"], 0)
        self.assertEqual(result["observation"]["updated"], 0)
        self.assertEqual(result["lifecycle"]["state"], "AWAITING_DEPOSIT")
        self.assertEqual(result["lifecycle"]["observation_count"], 0)
        self.assertFalse(
            result["lifecycle"]["financial_actions_authorized"]
        )

    def test_unconfirmed_deposit_remains_under_observation(self):
        self.rpc.entries = [
            ObserverTests.entry(self, confirmations=0)
        ]

        result = self.coordinate()

        self.assertEqual(result["observation"]["inserted"], 1)
        self.assertEqual(result["lifecycle"]["state"], "OBSERVING")
        self.assertEqual(result["lifecycle"]["observation_count"], 1)
        self.assertFalse(
            result["lifecycle"]["financial_actions_authorized"]
        )

    def test_wrong_session_is_rejected(self):
        with self.assertRaises(ValueError):
            coordinate_session(
                self.conn,
                self.rpc,
                pool_id="observer-pool",
                session_id="unknown-session",
            )

        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM pool_deposit_observations"
            ).fetchone()[0],
            0,
        )


    def test_confirmed_deposit_completes_evidence_pass(self):
        from tests.test_pool_verifier import FakeRPC as VerificationRPC

        blockchain = VerificationRPC()

        class CombinedRPC:
            def __init__(self, observer, verifier):
                self.observer = observer
                self.verifier = verifier

            def list_transactions(self, *args, **kwargs):
                return self.observer.list_transactions(*args, **kwargs)

            def get_raw_transaction(self, *args, **kwargs):
                return self.verifier.get_raw_transaction(*args, **kwargs)

            def get_block_header(self, *args, **kwargs):
                return self.verifier.get_block_header(*args, **kwargs)

            def get_block_hash(self, *args, **kwargs):
                return self.verifier.get_block_hash(*args, **kwargs)

        self.rpc.entries = [{
            **ObserverTests.entry(self,
                txid="a" * 64,
                amount="1.25000000",
                confirmations=3,
            ),
            "blockhash": "b" * 64,
        }]

        self.rpc = CombinedRPC(self.rpc, blockchain)

        result = self.coordinate()

        self.assertEqual(result["processed_observations"], 1)
        self.assertEqual(result["results"][0]["verification"]["state"], "VERIFIED")
        self.assertEqual(result["results"][0]["reconciliation"]["state"], "CURRENT")
        self.assertEqual(result["lifecycle"]["state"], "READY_FOR_REVIEW")
        self.assertFalse(result["financial_actions_authorized"])

        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM pool_deposits"
            ).fetchone()[0],
            0,
        )

    def test_pending_verification_does_not_reconcile(self):
        from tests.test_pool_verifier import FakeRPC as VerificationRPC

        blockchain = VerificationRPC()
        blockchain.active_hash = "d" * 64

        class CombinedRPC:
            def __init__(self, observer, verifier):
                self.observer = observer
                self.verifier = verifier

            def list_transactions(self, *args, **kwargs):
                return self.observer.list_transactions(*args, **kwargs)

            def get_raw_transaction(self, *args, **kwargs):
                return self.verifier.get_raw_transaction(*args, **kwargs)

            def get_block_header(self, *args, **kwargs):
                return self.verifier.get_block_header(*args, **kwargs)

            def get_block_hash(self, *args, **kwargs):
                return self.verifier.get_block_hash(*args, **kwargs)

        self.rpc.entries = [{
            **ObserverTests.entry(self,
                txid="a" * 64,
                amount="1.25000000",
                confirmations=3,
            ),
            "blockhash": "b" * 64,
        }]

        self.rpc = CombinedRPC(self.rpc, blockchain)

        result = self.coordinate()

        self.assertEqual(result["processed_observations"], 1)
        self.assertNotEqual(
            result["results"][0]["verification"]["state"],
            "VERIFIED",
        )
        self.assertIsNone(result["results"][0]["reconciliation"])
        self.assertEqual(result["lifecycle"]["state"], "OBSERVING")
        self.assertFalse(result["financial_actions_authorized"])


    def test_verification_failure_propagates(self):
        self.rpc.entries = [{
            **ObserverTests.entry(
                self,
                amount="1.25000000",
                confirmations=3,
            ),
            "blockhash": "b" * 64,
        }]

        # Observation RPC is available, but blockchain verification
        # methods are absent. The pass must fail rather than succeed.
        with self.assertRaises(Exception):
            self.coordinate()

        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM pool_deposit_observations"
            ).fetchone()[0],
            1,
        )

        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM pool_verification_events"
            ).fetchone()[0],
            0,
        )

        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM pool_deposits"
            ).fetchone()[0],
            0,
        )

    def test_repeated_empty_pass_is_financially_idempotent(self):
        first = self.coordinate()
        second = self.coordinate()

        self.assertEqual(first["lifecycle"]["state"], "AWAITING_DEPOSIT")
        self.assertEqual(second["lifecycle"]["state"], "AWAITING_DEPOSIT")
        self.assertEqual(second["observation"]["inserted"], 0)
        self.assertEqual(second["observation"]["updated"], 0)

        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM pool_deposits"
            ).fetchone()[0],
            0,
        )

        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM pool_journal_entries"
            ).fetchone()[0],
            0,
        )


    def test_partial_failure_then_retry_preserves_evidence(self):
        from tests.test_pool_verifier import FakeRPC as VerificationRPC

        observer = self.rpc
        verifier = VerificationRPC()

        observer.entries = [
            {
                **ObserverTests.entry(
                    self,
                    txid="a" * 64,
                    amount="1.25000000",
                    confirmations=3,
                ),
                "blockhash": "b" * 64,
            },
            {
                **ObserverTests.entry(
                    self,
                    txid="c" * 64,
                    amount="1.25000000",
                    confirmations=3,
                ),
                "blockhash": "b" * 64,
            },
        ]

        class RecoverableRPC:
            def __init__(self):
                self.fail_second = True

            def list_transactions(self, *args, **kwargs):
                return observer.list_transactions(*args, **kwargs)

            def get_raw_transaction(self, txid, *args, **kwargs):
                if txid == "c" * 64 and self.fail_second:
                    raise RuntimeError("Simulated second deposit interruption")
                result = verifier.get_raw_transaction(
                    txid, *args, **kwargs
                )
                result["txid"] = txid
                return result

            def get_block_header(self, *args, **kwargs):
                return verifier.get_block_header(*args, **kwargs)

            def get_block_hash(self, *args, **kwargs):
                return verifier.get_block_hash(*args, **kwargs)

        self.rpc = RecoverableRPC()

        with self.assertRaises(RuntimeError):
            self.coordinate()

        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM pool_deposit_observations"
            ).fetchone()[0],
            2,
        )

        first_history = self.conn.execute(
            """
            SELECT COUNT(*)
            FROM pool_verification_events
            """
        ).fetchone()[0]

        self.assertGreaterEqual(first_history, 1)

        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM pool_deposits"
            ).fetchone()[0],
            0,
        )

        self.rpc.fail_second = False

        result = self.coordinate()

        self.assertEqual(result["pass_state"], "COMPLETED")
        self.assertEqual(result["review_state"], "READY_FOR_REVIEW")
        self.assertEqual(result["processed_observations"], 2)
        self.assertEqual(result["observation"]["inserted"], 0)
        self.assertEqual(result["observation"]["updated"], 0)
        self.assertEqual(result["lifecycle"]["state"], "READY_FOR_REVIEW")

        final_history = self.conn.execute(
            """
            SELECT COUNT(*)
            FROM pool_verification_events
            """
        ).fetchone()[0]

        self.assertGreater(final_history, first_history)

        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM pool_deposits"
            ).fetchone()[0],
            0,
        )

        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM pool_journal_entries"
            ).fetchone()[0],
            0,
        )

        from flowlab.pool_lifecycle import session_snapshot

        before_reads = self.conn.total_changes

        snapshot = session_snapshot(
            self.conn,
            pool_id="observer-pool",
            session_id="observer-session",
        )

        self.assertEqual(snapshot["state"], "READY_FOR_REVIEW")
        self.assertFalse(snapshot["financial_actions_authorized"])
        self.assertEqual(self.conn.total_changes, before_reads)


    def test_reorganization_revokes_ready_status(self):
        from tests.test_pool_verifier import FakeRPC as VerificationRPC

        observer = self.rpc
        verifier = VerificationRPC()

        observer.entries = [{
            **ObserverTests.entry(
                self,
                amount="1.25000000",
                confirmations=3,
            ),
            "blockhash": "b" * 64,
        }]

        class CombinedRPC:
            def list_transactions(self, *args, **kwargs):
                return observer.list_transactions(*args, **kwargs)

            def get_raw_transaction(self, *args, **kwargs):
                return verifier.get_raw_transaction(*args, **kwargs)

            def get_block_header(self, *args, **kwargs):
                return verifier.get_block_header(*args, **kwargs)

            def get_block_hash(self, *args, **kwargs):
                return verifier.get_block_hash(*args, **kwargs)

        self.rpc = CombinedRPC()

        first = self.coordinate()

        self.assertEqual(
            first["lifecycle"]["state"],
            "READY_FOR_REVIEW",
        )

        # Simulate a blockchain reorganization.
        verifier.active_hash = "d" * 64

        second = self.coordinate()

        self.assertNotEqual(
            second["lifecycle"]["state"],
            "READY_FOR_REVIEW",
        )

        self.assertNotEqual(
            second["results"][0]["verification"]["state"],
            "VERIFIED",
        )

        self.assertIsNone(
            second["results"][0]["reconciliation"]
        )

        self.assertFalse(
            second["financial_actions_authorized"]
        )

        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM pool_deposits"
            ).fetchone()[0],
            0,
        )


    def test_conflicted_deposit_requires_review(self):
        self.rpc.entries = [
            ObserverTests.entry(
                self,
                amount="1.25000000",
                confirmations=-2,
            )
        ]

        result = self.coordinate()

        self.assertEqual(
            result["lifecycle"]["state"],
            "EVIDENCE_REVIEW_REQUIRED",
        )
        self.assertEqual(
            result["lifecycle"]["observations"][0]["observation_state"],
            "CONFLICTED",
        )
        self.assertFalse(result["financial_actions_authorized"])

    def test_incomplete_registration_rejected_before_rpc(self):
        self.conn.execute(
            """
            INSERT INTO pool_sessions(
                id, pool_id, fee_basis_points, created_at
            )
            VALUES ('incomplete-session', 'observer-pool', 500, 't')
            """
        )

        before_calls = len(self.rpc.calls)

        with self.assertRaises(ValueError):
            coordinate_session(
                self.conn,
                self.rpc,
                pool_id="observer-pool",
                session_id="incomplete-session",
            )

        self.assertEqual(len(self.rpc.calls), before_calls)

    def test_requesting_address_rejected_before_rpc(self):
        self.conn.execute(
            """
            INSERT INTO pool_sessions(
                id, pool_id, fee_basis_points, created_at
            )
            VALUES ('pending-session', 'observer-pool', 500, 't')
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
                'pending-session', 'observer-reserve',
                'pending-label', NULL, 'REQUESTING', 't', 't'
            )
            """
        )

        before_calls = len(self.rpc.calls)

        with self.assertRaises(ValueError):
            coordinate_session(
                self.conn,
                self.rpc,
                pool_id="observer-pool",
                session_id="pending-session",
            )

        self.assertEqual(len(self.rpc.calls), before_calls)

    def test_coordinator_never_creates_financial_records(self):
        self.coordinate()

        for table in (
            "pool_deposits",
            "pool_journal_transactions",
            "pool_journal_entries",
        ):
            with self.subTest(table=table):
                count = self.conn.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                self.assertEqual(count, 0)


    def test_completed_pass_reports_review_state(self):
        result = self.coordinate()

        self.assertEqual(result["pass_state"], "COMPLETED")
        self.assertEqual(
            result["review_state"],
            "AWAITING_DEPOSIT",
        )
        self.assertEqual(result["processed_observations"], 0)
        self.assertFalse(result["financial_actions_authorized"])

    def test_unconfirmed_pass_reports_recheck_required(self):
        self.rpc.entries = [
            ObserverTests.entry(self, confirmations=0)
        ]

        result = self.coordinate()

        self.assertEqual(result["pass_state"], "COMPLETED")
        self.assertEqual(result["review_state"], "RECHECK_REQUIRED")
        self.assertEqual(result["lifecycle"]["state"], "OBSERVING")
        self.assertFalse(result["financial_actions_authorized"])

    def test_invalid_identifiers_rejected_without_rpc(self):
        for invalid in ("", "   ", None, 123):
            with self.subTest(session_id=invalid):
                before_calls = len(self.rpc.calls)

                with self.assertRaises(ValueError):
                    coordinate_session(
                        self.conn,
                        self.rpc,
                        pool_id="observer-pool",
                        session_id=invalid,
                    )

                self.assertEqual(len(self.rpc.calls), before_calls)


    def test_repeat_pass_updates_review_telemetry(self):
        from tests.test_pool_verifier import FakeRPC as VerificationRPC

        observer = self.rpc
        verifier = VerificationRPC()

        observer.entries = [{
            **ObserverTests.entry(
                self,
                amount="1.25000000",
                confirmations=3,
            ),
            "blockhash": "b" * 64,
        }]

        class CombinedRPC:
            def list_transactions(self, *args, **kwargs):
                return observer.list_transactions(*args, **kwargs)

            def get_raw_transaction(self, *args, **kwargs):
                return verifier.get_raw_transaction(*args, **kwargs)

            def get_block_header(self, *args, **kwargs):
                return verifier.get_block_header(*args, **kwargs)

            def get_block_hash(self, *args, **kwargs):
                return verifier.get_block_hash(*args, **kwargs)

        self.rpc = CombinedRPC()

        first = self.coordinate()

        self.assertEqual(first["pass_state"], "COMPLETED")
        self.assertEqual(first["review_state"], "READY_FOR_REVIEW")

        first_history = self.conn.execute(
            "SELECT COUNT(*) FROM pool_verification_events"
        ).fetchone()[0]

        # Historical evidence is no longer supported.
        verifier.active_hash = "d" * 64

        second = self.coordinate()

        self.assertEqual(second["pass_state"], "COMPLETED")
        self.assertEqual(second["review_state"], "RECHECK_REQUIRED")
        self.assertEqual(second["lifecycle"]["state"], "OBSERVING")

        second_history = self.conn.execute(
            "SELECT COUNT(*) FROM pool_verification_events"
        ).fetchone()[0]

        self.assertGreater(second_history, first_history)

        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM pool_deposits"
            ).fetchone()[0],
            0,
        )

        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM pool_journal_entries"
            ).fetchone()[0],
            0,
        )

        self.assertFalse(second["financial_actions_authorized"])


if __name__ == "__main__":
    unittest.main()
