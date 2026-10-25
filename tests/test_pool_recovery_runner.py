"""Persistent recovery runner safety tests."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from flowlab import db
from flowlab import pool_recovery_runner as runner
from flowlab import pool_recovery_persistence as persistence


class RecoveryRunnerTests(unittest.TestCase):

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init(self.conn)

        self.conn.execute(
            "INSERT INTO pools "
            "(id,state,reserve_wallet,fee_wallet,created_at,updated_at) "
            "VALUES ('pool','ACTIVE','reserve','fees','t','t')"
        )
        self.conn.execute(
            "INSERT INTO pool_sessions "
            "(id,pool_id,fee_basis_points,created_at) "
            "VALUES ('session','pool',500,'t')"
        )

        self.session = {
            "pool_id": "pool",
            "session_id": "session",
        }

    def tearDown(self):
        self.conn.close()

    def execute_pass(self):
        with patch.object(
            runner,
            "eligible_sessions",
            return_value=[self.session],
        ):
            return runner.run_persistent_recovery_pass(
                self.conn,
                object(),
            )

    def test_success_records_completion(self):
        with patch.object(
            runner,
            "recover_session",
            return_value={"pass_state": "COMPLETED"},
        ):
            result = self.execute_pass()

        self.assertEqual(result["results"][0]["state"], "COMPLETED")

        events = [
            row[0] for row in self.conn.execute(
                "SELECT event FROM pool_recovery_runs ORDER BY id"
            )
        ]

        self.assertEqual(events, ["STARTED", "COMPLETED"])

    def test_failure_records_backoff(self):
        with patch.object(
            runner,
            "recover_session",
            side_effect=RuntimeError("RPC unavailable"),
        ):
            result = self.execute_pass()

        self.assertEqual(
            result["results"][0]["state"],
            "FAILED_BACKOFF",
        )

        schedule = self.conn.execute(
            "SELECT state,consecutive_failures "
            "FROM pool_recovery_schedules WHERE session_id='session'"
        ).fetchone()

        self.assertEqual(schedule["state"], "BACKOFF")
        self.assertEqual(schedule["consecutive_failures"], 1)

    def test_future_schedule_is_skipped(self):
        future = (
            datetime.now(timezone.utc) + timedelta(hours=1)
        ).isoformat()

        self.conn.execute(
            "INSERT INTO pool_recovery_schedules "
            "(session_id,state,next_attempt_at,updated_at) "
            "VALUES ('session','BACKOFF',?,'t')",
            (future,),
        )

        with patch.object(runner, "recover_session") as recover:
            result = self.execute_pass()

        recover.assert_not_called()

        self.assertEqual(
            result["results"][0]["state"],
            "SKIPPED_NOT_DUE",
        )

    def test_paused_schedule_is_skipped(self):
        self.conn.execute(
            "INSERT INTO pool_recovery_schedules "
            "(session_id,state,updated_at) "
            "VALUES ('session','PAUSED','t')"
        )

        with patch.object(runner, "recover_session") as recover:
            result = self.execute_pass()

        recover.assert_not_called()

        self.assertEqual(
            result["results"][0]["state"],
            "SKIPPED_PAUSED",
        )

    def test_competing_worker_is_blocked(self):
        lease = persistence.acquire_lease(
            self.conn,
            lease_name=runner.LEASE_NAME,
            ttl_seconds=300,
        )

        try:
            result = self.execute_pass()
            self.assertEqual(result["state"], "LEASE_BUSY")
        finally:
            persistence.release_lease(self.conn, lease=lease)

    def test_abandoned_attempt_requires_interruption(self):
        t0 = datetime.now(timezone.utc) - timedelta(minutes=20)

        old = persistence.acquire_lease(
            self.conn,
            lease_name=runner.LEASE_NAME,
            owner_token="crashed-worker",
            ttl_seconds=30,
            now=t0,
        )

        persistence.start_attempt(
            self.conn,
            lease=old,
            session_id="session",
            now=t0,
        )

        with patch.object(runner, "recover_session") as recover:
            result = self.execute_pass()

        recover.assert_not_called()

        self.assertEqual(
            result["results"][0]["state"],
            "INTERRUPTED_BACKOFF",
        )

        events = [
            row[0] for row in self.conn.execute(
                "SELECT event FROM pool_recovery_runs ORDER BY id"
            )
        ]

        self.assertEqual(events, ["STARTED", "INTERRUPTED"])

    def test_interrupted_attempt_not_hidden_by_future_schedule(self):
        t0 = datetime.now(timezone.utc) - timedelta(minutes=20)

        old = persistence.acquire_lease(
            self.conn,
            lease_name=runner.LEASE_NAME,
            owner_token="crashed-worker",
            ttl_seconds=30,
            now=t0,
        )

        persistence.start_attempt(
            self.conn,
            lease=old,
            session_id="session",
            now=t0,
        )

        future = (
            datetime.now(timezone.utc) + timedelta(hours=1)
        ).isoformat()

        self.conn.execute(
            "UPDATE pool_recovery_schedules "
            "SET state='BACKOFF',next_attempt_at=? "
            "WHERE session_id='session'",
            (future,),
        )

        with patch.object(runner, "recover_session") as recover:
            result = self.execute_pass()

        recover.assert_not_called()

        self.assertEqual(
            result["results"][0]["state"],
            "INTERRUPTED_BACKOFF",
        )


    def test_lease_loss_during_recovery_is_reported(self):
        from flowlab.pool_recovery_persistence import release_lease

        def lose_lease(conn, rpc, **kwargs):
            lease = kwargs["lease"]
            release_lease(conn, lease=lease)
            raise PermissionError("Recovery lease lost during RPC")

        with patch.object(
            runner,
            "recover_session",
            side_effect=lose_lease,
        ):
            result = self.execute_pass()

        self.assertEqual(result["state"], "LEASE_LOST")
        self.assertFalse(result["financial_actions_authorized"])

        events = [
            row[0] for row in self.conn.execute(
                "SELECT event FROM pool_recovery_runs ORDER BY id"
            )
        ]

        self.assertEqual(events, ["STARTED"])


    def test_lease_loss_after_successful_recovery(self):
        from flowlab.pool_recovery_persistence import release_lease

        def complete_rpc_then_lose_lease(conn, rpc, **kwargs):
            release_lease(conn, lease=kwargs["lease"])
            return {"pass_state": "COMPLETED"}

        with patch.object(
            runner,
            "recover_session",
            side_effect=complete_rpc_then_lose_lease,
        ):
            result = self.execute_pass()

        self.assertEqual(result["state"], "LEASE_LOST")
        self.assertFalse(result["financial_actions_authorized"])

        events = [
            row[0] for row in self.conn.execute(
                "SELECT event FROM pool_recovery_runs ORDER BY id"
            )
        ]

        self.assertEqual(events, ["STARTED"])


    def test_lease_renewed_for_each_session(self):
        self.conn.execute(
            "INSERT INTO pool_sessions "
            "(id,pool_id,fee_basis_points,created_at) "
            "VALUES ('session-two','pool',500,'t')"
        )

        sessions = [
            self.session,
            {"pool_id": "pool", "session_id": "session-two"},
        ]

        with (
            patch.object(
                runner,
                "eligible_sessions",
                return_value=sessions,
            ),
            patch.object(
                runner,
                "recover_session",
                return_value={"pass_state": "COMPLETED"},
            ),
            patch.object(
                runner,
                "renew_lease",
                wraps=persistence.renew_lease,
                create=True,
            ) as renew,
        ):
            result = runner.run_persistent_recovery_pass(
                self.conn,
                object(),
            )

        self.assertEqual(result["state"], "PASS_FINISHED")
        self.assertEqual(
            [item["state"] for item in result["results"]],
            ["COMPLETED", "COMPLETED"],
        )

        self.assertGreaterEqual(
            renew.call_count,
            2,
            "Recovery lease must be renewed for each session",
        )


    def test_lease_lost_between_sessions(self):
        from flowlab.pool_recovery_persistence import release_lease

        self.conn.execute(
            "INSERT INTO pool_sessions "
            "(id,pool_id,fee_basis_points,created_at) "
            "VALUES ('session-two','pool',500,'t')"
        )

        sessions = [
            self.session,
            {"pool_id": "pool", "session_id": "session-two"},
        ]

        processed = []

        def recover_then_release(conn, rpc, **kwargs):
            processed.append(kwargs["session_id"])

            if kwargs["session_id"] == "session":
                release_lease(conn, lease=kwargs["lease"])

            return {"pass_state": "COMPLETED"}

        with (
            patch.object(
                runner,
                "eligible_sessions",
                return_value=sessions,
            ),
            patch.object(
                runner,
                "recover_session",
                side_effect=recover_then_release,
            ),
        ):
            result = runner.run_persistent_recovery_pass(
                self.conn,
                object(),
            )

        self.assertEqual(result["state"], "LEASE_LOST")
        self.assertEqual(processed, ["session"])
        self.assertFalse(result["financial_actions_authorized"])

        events = [
            row["event"]
            for row in self.conn.execute(
                "SELECT event FROM pool_recovery_runs ORDER BY id"
            )
        ]

        self.assertEqual(events, ["STARTED"])


    def test_unresolved_attempt_uses_explicit_correlation(self):
        from flowlab.pool_recovery_persistence import (
            acquire_lease,
            start_attempt,
            complete_attempt,
        )

        lease = acquire_lease(
            self.conn,
            lease_name="correlation-test",
            owner_token="same-worker",
            ttl_seconds=600,
        )

        first = start_attempt(
            self.conn,
            lease=lease,
            session_id="session",
        )

        complete_attempt(
            self.conn,
            lease=lease,
            session_id="session",
            started_id=first,
            success=True,
        )

        second = start_attempt(
            self.conn,
            lease=lease,
            session_id="session",
        )

        unfinished = runner._unresolved(self.conn, "session")

        self.assertIsNotNone(unfinished)
        self.assertEqual(unfinished["id"], second)

        complete_attempt(
            self.conn,
            lease=lease,
            session_id="session",
            started_id=second,
            success=True,
        )

        self.assertIsNone(
            runner._unresolved(self.conn, "session")
        )

        terminal_events = self.conn.execute(
            "SELECT attempt_id FROM pool_recovery_runs "
            "WHERE event='COMPLETED' ORDER BY id"
        ).fetchall()

        self.assertEqual(
            [row["attempt_id"] for row in terminal_events],
            [first, second],
        )


    def test_lease_lost_during_recovery_rpc(self):
        from flowlab.pool_recovery_persistence import release_lease

        def lose_lease(conn, rpc, **kwargs):
            release_lease(conn, lease=kwargs["lease"])
            return {"pass_state": "COMPLETED"}

        with (
            patch.object(
                runner,
                "eligible_sessions",
                return_value=[self.session],
            ),
            patch.object(
                runner,
                "recover_session",
                side_effect=lose_lease,
            ) as recovery_mock,
        ):
            result = runner.run_persistent_recovery_pass(
                self.conn,
                object(),
            )

        recovery_mock.assert_called_once()

        self.assertEqual(result["state"], "LEASE_LOST")
        self.assertFalse(result["financial_actions_authorized"])

        events = [
            row["event"]
            for row in self.conn.execute(
                "SELECT event FROM pool_recovery_runs ORDER BY id"
            )
        ]

        self.assertEqual(events, ["STARTED"])

    def test_lease_lost_during_abandoned_interruption(self):
        from flowlab.pool_recovery_persistence import (
            acquire_lease,
            release_lease,
            start_attempt,
        )

        previous = acquire_lease(
            self.conn,
            lease_name=runner.LEASE_NAME,
            owner_token="previous-worker",
            ttl_seconds=600,
        )

        start_attempt(
            self.conn,
            lease=previous,
            session_id="session",
        )

        release_lease(self.conn, lease=previous)

        def lose_during_interruption(conn, **kwargs):
            release_lease(conn, lease=kwargs["lease"])
            raise PermissionError("Simulated ownership loss")

        with (
            patch.object(
                runner,
                "eligible_sessions",
                return_value=[self.session],
            ),
            patch.object(
                runner,
                "interrupt_abandoned_attempt",
                side_effect=lose_during_interruption,
            ) as interrupt_mock,
        ):
            result = runner.run_persistent_recovery_pass(
                self.conn,
                object(),
            )

        interrupt_mock.assert_called_once()

        self.assertEqual(result["state"], "LEASE_LOST")
        self.assertFalse(result["financial_actions_authorized"])

        events = [
            row["event"]
            for row in self.conn.execute(
                "SELECT event FROM pool_recovery_runs ORDER BY id"
            )
        ]

        self.assertEqual(events, ["STARTED"])


if __name__ == "__main__":
    unittest.main()
