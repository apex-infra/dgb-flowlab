"""M7.2 durable evidence-recovery persistence tests. No external RPC."""

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flowlab import db
from flowlab import pool_recovery_persistence as rp

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)


class RecoveryPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "research.db")
        self.conn = db.connect(self.path)
        db.init(self.conn)
        self.assertEqual(db.SCHEMA_VERSION, 7, "Requires V7 recovery schema")
        self.conn.execute(
            "INSERT INTO pools(id,state,reserve_wallet,fee_wallet,created_at,updated_at) "
            "VALUES ('p','ACTIVE','reserve','fees','t','t')"
        )
        self.conn.execute(
            "INSERT INTO pool_sessions(id,pool_id,fee_basis_points,created_at) "
            "VALUES ('s','p',500,'t')"
        )

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def lease(self, token="a", now=NOW, ttl=60):
        return rp.acquire_lease(self.conn, lease_name="mixer", owner_token=token,
                                ttl_seconds=ttl, now=now)

    def test_acquire_creates_fence(self):
        lease = self.lease()
        self.assertEqual(lease["fencing_token"], 1)
        self.assertEqual(lease["owner_token"], "a")

    def test_active_owner_blocks_competitor_and_same_token(self):
        self.lease()
        self.assertIsNone(self.lease("b", NOW + timedelta(seconds=5)))
        self.assertIsNone(self.lease("a", NOW + timedelta(seconds=5)))

    def test_expiry_reclaims_and_fence_increases(self):
        old = self.lease()
        new = self.lease("b", NOW + timedelta(seconds=60))
        self.assertEqual(new["fencing_token"], 2)
        with self.assertRaises(PermissionError):
            rp.release_lease(self.conn, lease=old, now=NOW + timedelta(seconds=61))

    def test_renewal_rejects_expired_token(self):
        old = self.lease()
        with self.assertRaises(PermissionError):
            rp.renew_lease(self.conn, lease=old, now=NOW + timedelta(seconds=60))

    def test_renewal_extends_exclusive_ownership(self):
        old = self.lease()
        renewed = rp.renew_lease(self.conn, lease=old, ttl_seconds=90,
                                 now=NOW + timedelta(seconds=30))
        self.assertEqual(renewed["fencing_token"], 1)
        self.assertIsNone(self.lease("b", NOW + timedelta(seconds=100)))

    def test_release_preserves_fencing_generation(self):
        old = self.lease()
        rp.release_lease(self.conn, lease=old, now=NOW)
        new = self.lease("b", NOW + timedelta(seconds=1))
        self.assertEqual(new["fencing_token"], 2)

    def test_two_connections_cannot_both_acquire(self):
        other = db.connect(self.path)
        try:
            first = self.lease()
            second = rp.acquire_lease(other, lease_name="mixer", owner_token="b", now=NOW)
            self.assertIsNone(second)
            self.assertEqual(first["fencing_token"], 1)
        finally:
            other.close()

    def test_start_and_complete_success_journal(self):
        lease = self.lease()
        started = rp.start_attempt(self.conn, lease=lease, session_id="s", now=NOW)
        report = rp.complete_attempt(self.conn, lease=lease, session_id="s",
                                     started_id=started, success=True, now=NOW)
        self.assertEqual(report["state"], "IDLE")
        self.assertFalse(report["financial_actions_authorized"])
        events = [r[0] for r in self.conn.execute(
            "SELECT event FROM pool_recovery_runs ORDER BY id")]
        self.assertEqual(events, ["STARTED", "COMPLETED"])

    def test_failure_backoff_increments_on_retry(self):
        first_lease = self.lease()
        first = rp.start_attempt(self.conn, lease=first_lease, session_id="s", now=NOW)
        rp.complete_attempt(self.conn, lease=first_lease, session_id="s",
                            started_id=first, success=False,
                            error_type="RPCUnavailable", now=NOW)
        rp.release_lease(self.conn, lease=first_lease, now=NOW)
        t2 = NOW + timedelta(seconds=60)
        lease = self.lease("b", t2)
        second = rp.start_attempt(self.conn, lease=lease, session_id="s", now=t2)
        report = rp.complete_attempt(self.conn, lease=lease, session_id="s",
                                     started_id=second, success=False,
                                     error_type="RPCUnavailable", now=t2)
        self.assertEqual(report["failures"], 2)
        self.assertEqual(report["next_attempt_at"],
                         (t2 + timedelta(seconds=120)).isoformat(timespec="microseconds"))

    def test_duplicate_completion_refused(self):
        lease = self.lease()
        started = rp.start_attempt(self.conn, lease=lease, session_id="s", now=NOW)
        rp.complete_attempt(self.conn, lease=lease, session_id="s",
                            started_id=started, success=True, now=NOW)
        with self.assertRaises(RuntimeError):
            rp.complete_attempt(self.conn, lease=lease, session_id="s",
                                started_id=started, success=True, now=NOW)

    def test_interrupted_attempt_is_not_silently_retried(self):
        lease = self.lease()
        rp.start_attempt(self.conn, lease=lease, session_id="s", now=NOW)
        with self.assertRaises(RuntimeError):
            rp.start_attempt(self.conn, lease=lease, session_id="s", now=NOW)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM pool_recovery_runs").fetchone()[0], 1)

    def test_stale_owner_cannot_write_attempt(self):
        old = self.lease()
        self.lease("new", NOW + timedelta(seconds=60))
        with self.assertRaises(PermissionError):
            rp.start_attempt(self.conn, lease=old, session_id="s",
                             now=NOW + timedelta(seconds=60))
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM pool_recovery_runs").fetchone()[0], 0)

    def test_unknown_session_cannot_create_attempt(self):
        lease = self.lease()
        with self.assertRaises(ValueError):
            rp.start_attempt(self.conn, lease=lease, session_id="missing", now=NOW)


if __name__ == "__main__":
    unittest.main()
