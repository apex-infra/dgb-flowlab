"""Recovery fencing tests at actual Mixer Lab evidence-write boundaries."""

import unittest
from datetime import datetime, timedelta, timezone

from flowlab import pool_observer
from flowlab.pool_verification_log import record_verification
from flowlab.pool_reconciliation_log import record_reconciliation
from flowlab.pool_recovery_persistence import acquire_lease

from tests.test_pool_observer import ObserverTests
from tests.test_pool_verifier import VerifierTests


def expired_and_current(conn):
    now = datetime.now(timezone.utc)

    expired = acquire_lease(
        conn,
        lease_name="recovery",
        owner_token="expired-worker",
        ttl_seconds=30,
        now=now - timedelta(minutes=5),
    )

    current = acquire_lease(
        conn,
        lease_name="recovery",
        owner_token="replacement-worker",
        ttl_seconds=300,
        now=now,
    )

    assert current["fencing_token"] > expired["fencing_token"]

    return expired, current


class RecoveryFencingTests(unittest.TestCase):

    def test_observer_rejects_expired_worker(self):
        fixture = ObserverTests("test_same_scan_is_idempotent")
        fixture.setUp()

        try:
            expired, current = expired_and_current(fixture.conn)

            fixture.rpc.entries = [
                fixture.entry(confirmations=3)
            ]

            with self.assertRaises(PermissionError):
                pool_observer.observe_session(
                    fixture.conn,
                    fixture.rpc,
                    pool_id="observer-pool",
                    session_id="observer-session",
                    lease=expired,
                )

            count = fixture.conn.execute(
                "SELECT COUNT(*) FROM pool_deposit_observations"
            ).fetchone()[0]

            self.assertEqual(count, 0)

            result = pool_observer.observe_session(
                fixture.conn,
                fixture.rpc,
                pool_id="observer-pool",
                session_id="observer-session",
                lease=current,
            )

            self.assertEqual(result["inserted"], 1)

        finally:
            fixture.tearDown()

    def test_verification_rejects_expired_worker(self):
        fixture = VerifierTests("runTest")
        fixture.setUp()

        try:
            expired, current = expired_and_current(fixture.conn)

            with self.assertRaises(PermissionError):
                record_verification(
                    fixture.conn,
                    fixture.rpc,
                    observation_id=fixture.observation_id,
                    lease=expired,
                )

            count = fixture.conn.execute(
                "SELECT COUNT(*) FROM pool_verification_events"
            ).fetchone()[0]

            self.assertEqual(count, 0)

            result = record_verification(
                fixture.conn,
                fixture.rpc,
                observation_id=fixture.observation_id,
                lease=current,
            )

            self.assertEqual(result["state"], "VERIFIED")

        finally:
            fixture.tearDown()

    def test_reconciliation_rejects_expired_worker(self):
        fixture = VerifierTests("runTest")
        fixture.setUp()

        try:
            record_verification(
                fixture.conn,
                fixture.rpc,
                observation_id=fixture.observation_id,
            )

            expired, current = expired_and_current(fixture.conn)

            with self.assertRaises(PermissionError):
                record_reconciliation(
                    fixture.conn,
                    fixture.rpc,
                    observation_id=fixture.observation_id,
                    lease=expired,
                )

            count = fixture.conn.execute(
                "SELECT COUNT(*) FROM pool_reconciliation_events"
            ).fetchone()[0]

            self.assertEqual(count, 0)

            result = record_reconciliation(
                fixture.conn,
                fixture.rpc,
                observation_id=fixture.observation_id,
                lease=current,
            )

            self.assertEqual(result["state"], "CURRENT")

        finally:
            fixture.tearDown()


    def test_lease_lost_during_verification_rpc(self):
        from unittest.mock import patch
        from flowlab import pool_verification_log

        fixture = VerifierTests("runTest")
        fixture.setUp()

        try:
            old, _ = expired_and_current(fixture.conn)

            # Build a fresh valid ownership generation.
            from flowlab.pool_recovery_persistence import release_lease

            # Existing replacement currently owns the lease.
            # Use a new lease name to isolate this scenario.
            active = acquire_lease(
                fixture.conn,
                lease_name="verification-rpc-test",
                owner_token="rpc-worker",
                ttl_seconds=300,
            )

            original_verify = pool_verification_log.verify_observation

            def lose_lease_during_rpc(*args, **kwargs):
                result = original_verify(*args, **kwargs)

                release_lease(
                    fixture.conn,
                    lease=active,
                )

                return result

            with patch.object(
                pool_verification_log,
                "verify_observation",
                side_effect=lose_lease_during_rpc,
            ):
                with self.assertRaises(PermissionError):
                    record_verification(
                        fixture.conn,
                        fixture.rpc,
                        observation_id=fixture.observation_id,
                        lease=active,
                    )

            count = fixture.conn.execute(
                "SELECT COUNT(*) FROM pool_verification_events"
            ).fetchone()[0]

            self.assertEqual(count, 0)

        finally:
            fixture.tearDown()


    def test_lease_lost_during_observation_rpc(self):
        from unittest.mock import patch
        from flowlab.pool_recovery_persistence import release_lease

        fixture = ObserverTests("runTest")
        fixture.setUp()

        try:
            fixture.rpc.entries = [
                fixture.entry(confirmations=3)
            ]

            active = acquire_lease(
                fixture.conn,
                lease_name="observation-rpc-test",
                owner_token="rpc-worker",
                ttl_seconds=300,
            )

            original = fixture.rpc.list_transactions
            released = [False]

            def lose_lease(*args, **kwargs):
                result = original(*args, **kwargs)

                if not released[0]:
                    released[0] = True
                    release_lease(fixture.conn, lease=active)

                return result

            with patch.object(
                fixture.rpc,
                "list_transactions",
                side_effect=lose_lease,
            ):
                with self.assertRaises(PermissionError):
                    pool_observer.observe_session(
                        fixture.conn,
                        fixture.rpc,
                        pool_id="observer-pool",
                        session_id="observer-session",
                        lease=active,
                    )

            count = fixture.conn.execute(
                "SELECT COUNT(*) FROM pool_deposit_observations"
            ).fetchone()[0]

            self.assertEqual(count, 0)

        finally:
            fixture.tearDown()

    def test_lease_lost_during_reconciliation_rpc(self):
        from unittest.mock import patch
        from flowlab import pool_reconciliation_log
        from flowlab.pool_recovery_persistence import release_lease

        fixture = VerifierTests("runTest")
        fixture.setUp()

        try:
            record_verification(
                fixture.conn,
                fixture.rpc,
                observation_id=fixture.observation_id,
            )

            active = acquire_lease(
                fixture.conn,
                lease_name="reconciliation-rpc-test",
                owner_token="rpc-worker",
                ttl_seconds=300,
            )

            original = pool_reconciliation_log.reconcile_verification

            def lose_lease(*args, **kwargs):
                result = original(*args, **kwargs)
                release_lease(fixture.conn, lease=active)
                return result

            with patch.object(
                pool_reconciliation_log,
                "reconcile_verification",
                side_effect=lose_lease,
            ):
                with self.assertRaises(PermissionError):
                    record_reconciliation(
                        fixture.conn,
                        fixture.rpc,
                        observation_id=fixture.observation_id,
                        lease=active,
                    )

            count = fixture.conn.execute(
                "SELECT COUNT(*) FROM pool_reconciliation_events"
            ).fetchone()[0]

            self.assertEqual(count, 0)

        finally:
            fixture.tearDown()


if __name__ == "__main__":
    unittest.main()
