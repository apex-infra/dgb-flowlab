"""Mixer Lab recovery CLI tests."""

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from flowlab import db
from flowlab.cli import main



class ObservationRpc:
    def list_transactions(self, wallet, **kwargs):
        return []

    def get_block_hash(self, height):
        return "hash"

    def get_block_header(self, blockhash):
        return {}

    def get_raw_transaction(self, txid, verbose=True, blockhash=None):
        return {}


class RecoveryCliTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "recovery.db")
        conn = db.connect(self.path)
        db.init(conn)
        conn.close()
        self.lines = []

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, *args):
        return main(
            list(args),
            db=self.path,
            out=self.lines.append,
        )

    def test_status(self):
        self.assertEqual(self.call("recovery", "status"), 0)
        self.assertIn("MANUAL", "\n".join(self.lines))

    def test_empty_schedules(self):
        self.assertEqual(self.call("recovery", "schedules"), 0)
        self.assertIn("No recovery schedules", self.lines)

    def test_empty_history(self):
        self.assertEqual(self.call("recovery", "history"), 0)
        self.assertIn("No recovery history", self.lines)

    def test_invalid_history_limit(self):
        with self.assertRaises(ValueError):
            self.call("recovery", "history", "--limit", "0")

    def test_connection_rejects_writes(self):
        conn = db.connect_readonly(self.path)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute(
                    "INSERT INTO meta(key,value) "
                    "VALUES('readonly-test','blocked')"
                )
        finally:
            conn.close()

    def test_missing_database_not_created(self):
        missing = str(Path(self.tmp.name) / "missing.db")
        with self.assertRaises((OSError, sqlite3.OperationalError)):
            db.connect_readonly(missing)
        self.assertFalse(os.path.exists(missing))

    def test_inspection_does_not_modify_database(self):
        before = os.stat(self.path).st_size

        self.call("recovery", "status")
        self.call("recovery", "schedules")
        self.call("recovery", "history")

        self.assertEqual(os.stat(self.path).st_size, before)



    def test_run_once_requires_authorization(self):
        result = self.call("recovery", "run-once")
        self.assertEqual(result, 2)
        self.assertIn(
            "requires --authorize",
            "\\n".join(self.lines),
        )

    def test_run_once_requires_available_rpc_configuration(self):
        from unittest.mock import patch

        with patch(
            "flowlab.cli._load",
            side_effect=FileNotFoundError("Missing local config"),
        ):
            result = main(
                ["recovery", "run-once", "--authorize"],
                db=self.path,
                rpc=None,
                out=self.lines.append,
            )

        self.assertEqual(result, 2)
        self.assertTrue(
            any("REFUSED" in line for line in self.lines)
        )


    def test_recovery_without_authorization_does_not_load_config(self):
        from unittest.mock import patch

        with patch("flowlab.cli._load") as load_mock:
            result = main(
                ["recovery", "run-once"],
                db=self.path,
                rpc=None,
                out=self.lines.append,
            )

        self.assertEqual(result, 2)
        load_mock.assert_not_called()



    def test_run_once_valid_schema_executes(self):
        result = main(
            ["recovery", "run-once", "--authorize"],
            db=self.path,
            rpc=ObservationRpc(),
            out=self.lines.append,
        )

        self.assertEqual(result, 0)
        self.assertIn(
            "Recovery result: PASS_FINISHED",
            "\\n".join(self.lines),
        )

    def test_run_once_rejects_incomplete_schema(self):
        import sqlite3

        conn = sqlite3.connect(self.path)
        try:
            conn.execute("DROP TABLE pool_recovery_runs")
            conn.commit()
        finally:
            conn.close()

        result = main(
            ["recovery", "run-once", "--authorize"],
            db=self.path,
            rpc=ObservationRpc(),
            out=self.lines.append,
        )

        self.assertEqual(result, 2)
        self.assertIn(
            "schema validation failed",
            "\\n".join(self.lines),
        )


    def test_run_once_reports_busy_lease(self):
        from unittest.mock import patch

        response = {
            "state": "LEASE_BUSY",
            "results": [],
            "financial_actions_authorized": False,
        }

        with patch(
            "flowlab.pool_recovery_runner.run_persistent_recovery_pass",
            return_value=response,
        ) as mocked:
            result = main(
                ["recovery", "run-once", "--authorize"],
                db=self.path,
                rpc=ObservationRpc(),
                out=self.lines.append,
            )

        mocked.assert_called_once()
        self.assertEqual(result, 2)
        self.assertIn(
            "Recovery result: LEASE_BUSY",
            "\\n".join(self.lines),
        )

    def test_run_once_reports_processed_session(self):
        from unittest.mock import patch

        response = {
            "state": "PASS_FINISHED",
            "results": [{
                "session_id": "session-one",
                "state": "COMPLETED",
            }],
            "financial_actions_authorized": False,
        }

        with patch(
            "flowlab.pool_recovery_runner.run_persistent_recovery_pass",
            return_value=response,
        ) as mocked:
            result = main(
                ["recovery", "run-once", "--authorize"],
                db=self.path,
                rpc=ObservationRpc(),
                out=self.lines.append,
            )

        mocked.assert_called_once()
        self.assertEqual(result, 0)
        self.assertIn(
            "session-one | COMPLETED",
            "\\n".join(self.lines),
        )

    def test_run_once_rejects_financial_authorization(self):
        from unittest.mock import patch

        response = {
            "state": "PASS_FINISHED",
            "results": [],
            "financial_actions_authorized": True,
        }

        with patch(
            "flowlab.pool_recovery_runner.run_persistent_recovery_pass",
            return_value=response,
        ):
            with self.assertRaises(RuntimeError):
                main(
                    ["recovery", "run-once", "--authorize"],
                    db=self.path,
                    rpc=ObservationRpc(),
                    out=self.lines.append,
                )


    def test_run_once_executes_real_runner_for_eligible_session(self):
        from unittest.mock import patch
        from flowlab.pool_recovery_runner import LEASE_NAME

        conn = db.connect(self.path)
        try:
            conn.execute(
                "INSERT INTO pools "
                "(id,state,reserve_wallet,fee_wallet,created_at,updated_at) "
                "VALUES ('pool','ACTIVE','reserve','fees','t','t')"
            )
            conn.execute(
                "INSERT INTO pool_sessions "
                "(id,pool_id,fee_basis_points,created_at) "
                "VALUES ('session','pool',500,'t')"
            )
        finally:
            conn.close()

        session = {
            "pool_id": "pool",
            "session_id": "session",
        }

        with (
            patch(
                "flowlab.pool_recovery_runner.eligible_sessions",
                return_value=[session],
            ),
            patch(
                "flowlab.pool_recovery_runner.recover_session",
                return_value={"pass_state": "COMPLETED"},
            ) as recover_mock,
        ):
            result = main(
                ["recovery", "run-once", "--authorize"],
                db=self.path,
                rpc=ObservationRpc(),
                out=self.lines.append,
            )

        self.assertEqual(result, 0)
        recover_mock.assert_called_once()

        conn = db.connect_readonly(self.path)
        try:
            events = conn.execute(
                "SELECT event,attempt_id,id "
                "FROM pool_recovery_runs ORDER BY id"
            ).fetchall()

            self.assertEqual(
                [row["event"] for row in events],
                ["STARTED", "COMPLETED"],
            )
            self.assertEqual(events[1]["attempt_id"], events[0]["id"])

            schedule = conn.execute(
                "SELECT state FROM pool_recovery_schedules "
                "WHERE session_id='session'"
            ).fetchone()

            self.assertEqual(schedule["state"], "IDLE")

            lease = conn.execute(
                "SELECT owner_token FROM pool_recovery_leases "
                "WHERE lease_name=?",
                (LEASE_NAME,),
            ).fetchone()

            self.assertIsNotNone(lease)
            self.assertIsNone(lease["owner_token"])
        finally:
            conn.close()

    def test_run_once_uses_restricted_rpc(self):
        from unittest.mock import patch
        from flowlab.pool_recovery_rpc import RecoveryRpc

        class FullRpc:
            def list_transactions(self, wallet, **kwargs):
                return []

            def get_block_hash(self, height):
                return "hash"

            def get_block_header(self, blockhash):
                return {}

            def get_raw_transaction(
                self, txid, verbose=True, blockhash=None
            ):
                return {}

            def send_raw_transaction(self, tx):
                raise AssertionError("Broadcast must not be available")

        observed = []

        def inspect_runner(conn, rpc):
            observed.append(rpc)

            self.assertIsInstance(rpc, RecoveryRpc)
            self.assertFalse(hasattr(rpc, "send_raw_transaction"))
            self.assertFalse(hasattr(rpc, "create_raw_transaction"))

            return {
                "state": "PASS_FINISHED",
                "results": [],
                "financial_actions_authorized": False,
            }

        with patch(
            "flowlab.pool_recovery_runner.run_persistent_recovery_pass",
            side_effect=inspect_runner,
        ):
            result = main(
                ["recovery", "run-once", "--authorize"],
                db=self.path,
                rpc=FullRpc(),
                out=self.lines.append,
            )

        self.assertEqual(result, 0)
        self.assertEqual(len(observed), 1)

    def test_run_once_rejects_incomplete_rpc(self):
        from unittest.mock import patch

        with patch(
            "flowlab.pool_recovery_runner.run_persistent_recovery_pass"
        ) as runner_mock:
            with self.assertRaises(TypeError):
                main(
                    ["recovery", "run-once", "--authorize"],
                    db=self.path,
                    rpc=object(),
                    out=self.lines.append,
                )

        runner_mock.assert_not_called()


    def test_run_once_failed_session_returns_nonzero(self):
        from unittest.mock import patch

        response = {
            "state": "PASS_FINISHED",
            "results": [{
                "session_id": "failed-session",
                "state": "FAILED_BACKOFF",
                "error_type": "RpcError",
            }],
            "financial_actions_authorized": False,
        }

        with patch(
            "flowlab.pool_recovery_runner.run_persistent_recovery_pass",
            return_value=response,
        ):
            result = main(
                ["recovery", "run-once", "--authorize"],
                db=self.path,
                rpc=ObservationRpc(),
                out=self.lines.append,
            )

        self.assertEqual(
            result,
            2,
            "Recovery failures must produce a nonzero CLI exit code",
        )
        self.assertIn(
            "failed-session | FAILED_BACKOFF",
            "\n".join(self.lines),
        )


    def test_run_once_session_outcome_exit_codes(self):
        from unittest.mock import patch

        cases = [
            ("COMPLETED", 0),
            ("SKIPPED_PAUSED", 0),
            ("SKIPPED_NOT_DUE", 0),
            ("FAILED_BACKOFF", 2),
            ("INTERRUPTED_BACKOFF", 2),
            ("INTERRUPTION_REVIEW_REQUIRED", 2),
        ]

        for state, expected in cases:
            with self.subTest(state=state):
                response = {
                    "state": "PASS_FINISHED",
                    "results": [{
                        "session_id": "session-one",
                        "state": state,
                    }],
                    "financial_actions_authorized": False,
                }

                with patch(
                    "flowlab.pool_recovery_runner."
                    "run_persistent_recovery_pass",
                    return_value=response,
                ):
                    result = main(
                        ["recovery", "run-once", "--authorize"],
                        db=self.path,
                        rpc=ObservationRpc(),
                        out=self.lines.append,
                    )

                self.assertEqual(result, expected)

    def test_run_once_lease_lost_returns_nonzero(self):
        from unittest.mock import patch

        response = {
            "state": "LEASE_LOST",
            "results": [{
                "session_id": "session-one",
                "state": "INTERRUPTED_PENDING",
            }],
            "financial_actions_authorized": False,
        }

        with patch(
            "flowlab.pool_recovery_runner."
            "run_persistent_recovery_pass",
            return_value=response,
        ):
            result = main(
                ["recovery", "run-once", "--authorize"],
                db=self.path,
                rpc=ObservationRpc(),
                out=self.lines.append,
            )

        self.assertEqual(result, 2)
        self.assertIn(
            "Recovery result: LEASE_LOST",
            "\n".join(self.lines),
        )

    def test_run_once_mixed_results_return_nonzero(self):
        from unittest.mock import patch

        response = {
            "state": "PASS_FINISHED",
            "results": [
                {
                    "session_id": "session-one",
                    "state": "COMPLETED",
                },
                {
                    "session_id": "session-two",
                    "state": "FAILED_BACKOFF",
                },
            ],
            "financial_actions_authorized": False,
        }

        with patch(
            "flowlab.pool_recovery_runner."
            "run_persistent_recovery_pass",
            return_value=response,
        ):
            result = main(
                ["recovery", "run-once", "--authorize"],
                db=self.path,
                rpc=ObservationRpc(),
                out=self.lines.append,
            )

        self.assertEqual(result, 2)

    def test_run_once_revalidates_writable_connection(self):
        from unittest.mock import patch
        from flowlab import db as database

        original_validator = database.validate_pool_recovery_schema
        validations = []

        def validator(conn):
            validations.append(True)

            if len(validations) == 2:
                raise RuntimeError("Simulated writable schema failure")

            return original_validator(conn)

        with (
            patch(
                "flowlab.db.validate_pool_recovery_schema",
                side_effect=validator,
            ),
            patch(
                "flowlab.pool_recovery_runner."
                "run_persistent_recovery_pass"
            ) as runner_mock,
        ):
            result = main(
                ["recovery", "run-once", "--authorize"],
                db=self.path,
                rpc=ObservationRpc(),
                out=self.lines.append,
            )

        self.assertEqual(result, 2)
        self.assertEqual(len(validations), 2)
        runner_mock.assert_not_called()
        self.assertIn(
            "REFUSED: recovery schema validation failed",
            "\n".join(self.lines),
        )


    def test_recovery_invalid_database_never_loads_config(self):
        from unittest.mock import patch
        from flowlab import db as database

        conn = database.connect(self.path)
        try:
            conn.execute(
                "UPDATE meta SET value='6' WHERE key='schema_version'"
            )
            conn.commit()
        finally:
            conn.close()

        with (
            patch("flowlab.cli._load") as load_mock,
            patch(
                "flowlab.pool_recovery_rpc.create_recovery_rpc"
            ) as rpc_mock,
        ):
            result = main(
                ["recovery", "run-once", "--authorize"],
                db=self.path,
                rpc=None,
                out=self.lines.append,
            )

        self.assertEqual(result, 2)
        load_mock.assert_not_called()
        rpc_mock.assert_not_called()

    def test_recovery_missing_config_returns_refusal(self):
        from unittest.mock import patch

        with patch(
            "flowlab.cli._load",
            side_effect=FileNotFoundError("missing configuration"),
        ):
            result = main(
                ["recovery", "run-once", "--authorize"],
                db=self.path,
                rpc=None,
                out=self.lines.append,
            )

        self.assertEqual(result, 2)
        self.assertTrue(
            any("REFUSED" in line for line in self.lines)
        )


    def test_recovery_configuration_security(self):
        from unittest.mock import patch
        from flowlab.pool_recovery_rpc import RecoveryRpc

        secret = "PRIVATE_TEST_PASSWORD_DO_NOT_DISPLAY"

        cases = [
            {
                "rpc_user": "operator",
                "rpc_password": secret,
                "rpc_host": "192.168.1.100",
                "rpc_port": 14022,
                "wallets": ["flab_pool_reserve"],
            },
            {
                "rpc_user": "operator",
                "rpc_password": secret,
                "rpc_host": "127.0.0.1",
                "rpc_port": 14022,
                "wallets": [],
            },
            {
                "rpc_user": "operator",
                "rpc_password": secret,
                "rpc_host": "127.0.0.1",
                "rpc_port": 14022,
            },
        ]

        for config in cases:
            with self.subTest(config=config.get("rpc_host")):
                output = []

                with (
                    patch("flowlab.cli._load", return_value=config),
                    patch(
                        "flowlab.pool_recovery_runner."
                        "run_persistent_recovery_pass"
                    ) as runner,
                ):
                    result = main(
                        ["recovery", "run-once", "--authorize"],
                        db=self.path,
                        rpc=None,
                        out=output.append,
                    )

                self.assertEqual(result, 2)
                runner.assert_not_called()
                self.assertNotIn(secret, "\n".join(output))
                self.assertTrue(
                    any("REFUSED" in line for line in output)
                )

    def test_recovery_inspection_never_loads_rpc_configuration(self):
        from unittest.mock import patch

        for command in ("status", "schedules", "history"):
            with self.subTest(command=command):
                with patch("flowlab.cli._load") as load_mock:
                    result = main(
                        ["recovery", command],
                        db=self.path,
                        rpc=None,
                        out=self.lines.append,
                    )

                self.assertEqual(result, 0)
                load_mock.assert_not_called()

    def test_recovery_local_configuration_passes_restricted_rpc(self):
        from unittest.mock import patch
        from flowlab.pool_recovery_rpc import RecoveryRpc

        config = {
            "rpc_user": "operator",
            "rpc_password": "test-only",
            "rpc_host": "127.0.0.1",
            "rpc_port": 14022,
            "wallets": ["flab_pool_reserve"],
        }

        observed = []

        def runner(conn, rpc):
            observed.append(rpc)
            self.assertIsInstance(rpc, RecoveryRpc)
            self.assertFalse(hasattr(rpc, "send_raw_transaction"))

            return {
                "state": "PASS_FINISHED",
                "results": [],
                "financial_actions_authorized": False,
            }

        with (
            patch("flowlab.cli._load", return_value=config),
            patch(
                "flowlab.pool_recovery_runner."
                "run_persistent_recovery_pass",
                side_effect=runner,
            ),
        ):
            result = main(
                ["recovery", "run-once", "--authorize"],
                db=self.path,
                rpc=None,
                out=self.lines.append,
            )

        self.assertEqual(result, 0)
        self.assertEqual(len(observed), 1)
if __name__ == "__main__":
    unittest.main()
