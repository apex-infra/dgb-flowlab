import unittest
from datetime import datetime, timedelta, timezone

from flowlab import db
from flowlab import pool_recovery_persistence as recovery


class RecoveryCrashTests(unittest.TestCase):

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init(self.conn)

        self.conn.execute(
            "INSERT INTO pools "
            "(id,state,reserve_wallet,fee_wallet,created_at,updated_at) "
            "VALUES ('test-pool','PAUSED','reserve','fees','t','t')"
        )
        self.conn.execute(
            "INSERT INTO pool_sessions "
            "(id,pool_id,fee_basis_points,created_at) "
            "VALUES ('test-session','test-pool',500,'t')"
        )

        self.t0 = datetime(2026, 10, 25, tzinfo=timezone.utc)

    def tearDown(self):
        self.conn.close()

    def abandoned_attempt(self):
        old = recovery.acquire_lease(
            self.conn,
            lease_name="recovery",
            owner_token="worker-a",
            ttl_seconds=30,
            now=self.t0,
        )

        started_id = recovery.start_attempt(
            self.conn,
            lease=old,
            session_id="test-session",
            now=self.t0,
        )

        later = self.t0 + timedelta(seconds=31)

        new = recovery.acquire_lease(
            self.conn,
            lease_name="recovery",
            owner_token="worker-b",
            ttl_seconds=30,
            now=later,
        )

        return old, new, started_id, later

    def test_expired_worker_rejected(self):
        old, new, started_id, later = self.abandoned_attempt()

        self.assertGreater(
            new["fencing_token"],
            old["fencing_token"],
        )

        with self.assertRaises(PermissionError):
            recovery.complete_attempt(
                self.conn,
                lease=old,
                session_id="test-session",
                started_id=started_id,
                success=True,
                now=later,
            )

    def test_interruption_is_durable(self):
        _, new, started_id, later = self.abandoned_attempt()

        result = recovery.interrupt_abandoned_attempt(
            self.conn,
            lease=new,
            session_id="test-session",
            started_id=started_id,
            now=later,
        )

        events = [
            row[0]
            for row in self.conn.execute(
                "SELECT event FROM pool_recovery_runs ORDER BY id"
            )
        ]

        self.assertEqual(events, ["STARTED", "INTERRUPTED"])
        self.assertEqual(result["state"], "BACKOFF")
        self.assertEqual(result["failures"], 1)
        self.assertFalse(result["financial_actions_authorized"])

    def test_interruption_cannot_repeat(self):
        _, new, started_id, later = self.abandoned_attempt()

        recovery.interrupt_abandoned_attempt(
            self.conn,
            lease=new,
            session_id="test-session",
            started_id=started_id,
            now=later,
        )

        with self.assertRaises(RuntimeError):
            recovery.interrupt_abandoned_attempt(
                self.conn,
                lease=new,
                session_id="test-session",
                started_id=started_id,
                now=later,
            )

    def test_existing_failure_count_preserved(self):
        _, new, started_id, later = self.abandoned_attempt()

        self.conn.execute(
            "UPDATE pool_recovery_schedules "
            "SET consecutive_failures=3 WHERE session_id='test-session'"
        )

        result = recovery.interrupt_abandoned_attempt(
            self.conn,
            lease=new,
            session_id="test-session",
            started_id=started_id,
            now=later,
        )

        self.assertEqual(result["failures"], 4)


if __name__ == "__main__":
    unittest.main()
