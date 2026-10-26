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

    def test_run_once_keyboard_interrupt_returns_130(self):
        from unittest.mock import patch

        with patch(
            "flowlab.pool_recovery_runner.run_persistent_recovery_pass",
            side_effect=KeyboardInterrupt,
        ) as runner:
            result = main(
                ["recovery", "run-once", "--authorize"],
                db=self.path,
                rpc=ObservationRpc(),
                out=self.lines.append,
            )

        runner.assert_called_once()
        self.assertEqual(result, 130)
        self.assertIn(
            "Recovery interrupted by operator",
            "\n".join(self.lines),
        )
        self.assertNotIn(
            "Recovery result: PASS_FINISHED",
            "\n".join(self.lines),
        )


    def test_offline_preflight_read_only(self):
        from unittest.mock import patch

        before = Path(self.path).read_bytes()
        output = []

        with (
            patch("flowlab.cli._load") as config_mock,
            patch(
                "flowlab.pool_recovery_runner."
                "run_persistent_recovery_pass"
            ) as runner_mock,
        ):
            result = main(
                ["recovery", "preflight"],
                db=self.path,
                rpc=None,
                out=output.append,
            )

        self.assertEqual(result, 0)
        self.assertIn("Database schema: V7 VALID", "\n".join(output))
        self.assertIn("Financial actions authorized: NO", "\n".join(output))
        self.assertIn("Automatic recovery: DISABLED", "\n".join(output))

        config_mock.assert_not_called()
        runner_mock.assert_not_called()

        self.assertEqual(Path(self.path).read_bytes(), before)

    def test_offline_preflight_missing_database(self):
        missing = str(Path(self.tmp.name) / "missing-recovery.db")
        output = []

        result = main(
            ["recovery", "preflight"],
            db=missing,
            rpc=None,
            out=output.append,
        )

        self.assertEqual(result, 2)
        self.assertFalse(Path(missing).exists())
        self.assertIn("PREFLIGHT FAIL", "\n".join(output))

    def test_offline_preflight_invalid_schema(self):
        conn = db.connect(self.path)

        try:
            conn.execute(
                "UPDATE meta SET value='6' "
                "WHERE key='schema_version'"
            )
            conn.commit()
        finally:
            conn.close()

        output = []

        result = main(
            ["recovery", "preflight"],
            db=self.path,
            rpc=None,
            out=output.append,
        )

        self.assertEqual(result, 2)
        self.assertIn("PREFLIGHT FAIL", "\n".join(output))

    def test_offline_preflight_does_not_change_existing_lease(self):
        from flowlab.pool_recovery_persistence import acquire_lease
        from flowlab.pool_recovery_runner import LEASE_NAME

        conn = db.connect(self.path)

        try:
            acquire_lease(
                conn,
                lease_name=LEASE_NAME,
                owner_token="existing-test-owner",
                ttl_seconds=600,
            )

            before = tuple(
                conn.execute(
                    "SELECT owner_token,fencing_token,expires_at "
                    "FROM pool_recovery_leases WHERE lease_name=?",
                    (LEASE_NAME,),
                ).fetchone()
            )
        finally:
            conn.close()

        output = []

        result = main(
            ["recovery", "preflight"],
            db=self.path,
            rpc=None,
            out=output.append,
        )

        self.assertEqual(result, 0)
        self.assertIn("OWNED", "\n".join(output))

        conn = db.connect_readonly(self.path)

        try:
            after = tuple(
                conn.execute(
                    "SELECT owner_token,fencing_token,expires_at "
                    "FROM pool_recovery_leases WHERE lease_name=?",
                    (LEASE_NAME,),
                ).fetchone()
            )
        finally:
            conn.close()

        self.assertEqual(before, after)

    def test_preflight_check_node_success(self):
        from unittest.mock import patch

        class ReadOnlyNode:
            allowed_wallets = frozenset({"flab_pool_reserve"})

            def get_block_count(self):
                return 24400000

        output = []

        with (
            patch(
                "flowlab.cli._load",
                return_value={
                    "rpc_user": "test",
                    "rpc_password": "test-secret",
                    "rpc_host": "127.0.0.1",
                    "rpc_port": 14022,
                    "wallets": ["flab_pool_reserve"],
                },
            ),
            patch(
                "flowlab.cli.RpcClient",
                return_value=ReadOnlyNode(),
            ),
            patch(
                "flowlab.pool_recovery_runner.run_persistent_recovery_pass"
            ) as runner,
        ):
            result = main(
                ["recovery", "preflight", "--check-node"],
                db=self.path,
                rpc=None,
                out=output.append,
            )

        self.assertEqual(result, 0)
        self.assertIn(
            "RPC connectivity: OK",
            "\n".join(output),
        )
        self.assertIn(
            "24400000",
            "\n".join(output),
        )
        runner.assert_not_called()

        self.assertNotIn(
            "RPC connectivity: NOT CHECKED",
            "\n".join(output),
        )

    def test_preflight_check_node_failure(self):
        from unittest.mock import patch
        from flowlab.rpc import RpcError

        class FailedNode:
            allowed_wallets = frozenset({"flab_pool_reserve"})

            def get_block_count(self):
                raise RpcError("TEST_SECRET_DO_NOT_DISPLAY")

        output = []

        with (
            patch(
                "flowlab.cli._load",
                return_value={
                    "rpc_user": "test",
                    "rpc_password": "test-secret",
                    "rpc_host": "127.0.0.1",
                    "rpc_port": 14022,
                    "wallets": ["flab_pool_reserve"],
                },
            ),
            patch(
                "flowlab.cli.RpcClient",
                return_value=FailedNode(),
            ),
        ):
            result = main(
                ["recovery", "preflight", "--check-node"],
                db=self.path,
                rpc=None,
                out=output.append,
            )

        self.assertEqual(result, 2)
        self.assertIn(
            "RPC connectivity: FAILED",
            "\n".join(output),
        )
        self.assertNotIn(
            "TEST_SECRET_DO_NOT_DISPLAY",
            "\n".join(output),
        )

        self.assertNotIn(
            "RPC connectivity: NOT CHECKED",
            "\n".join(output),
        )

    def test_node_preflight_preserves_recovery_database(self):
        """Node connectivity must not change persistent recovery state."""
        from unittest.mock import patch
        from flowlab.pool_recovery_persistence import acquire_lease
        from flowlab.pool_recovery_runner import LEASE_NAME

        conn = db.connect(self.path)

        try:
            acquire_lease(
                conn,
                lease_name=LEASE_NAME,
                owner_token="existing-owner",
                ttl_seconds=600,
            )

            before = {}

            for table in (
                "pool_recovery_leases",
                "pool_recovery_schedules",
                "pool_recovery_runs",
            ):
                before[table] = [
                    tuple(row)
                    for row in conn.execute(
                        f"SELECT * FROM {table} ORDER BY rowid"
                    )
                ]
        finally:
            conn.close()

        class FakeNode:
            allowed_wallets = frozenset({"flab_pool_reserve"})

            def get_block_count(self):
                return 24400000

        config = {
            "rpc_user": "test",
            "rpc_password": "test-secret",
            "rpc_host": "127.0.0.1",
            "rpc_port": 14022,
            "wallets": ["flab_pool_reserve"],
        }

        output = []

        with (
            patch("flowlab.cli._load", return_value=config),
            patch("flowlab.cli.RpcClient", return_value=FakeNode()),
            patch(
                "flowlab.pool_recovery_runner."
                "run_persistent_recovery_pass"
            ) as runner_mock,
            patch(
                "flowlab.pool_recovery_persistence.acquire_lease"
            ) as acquire_mock,
        ):
            result = main(
                ["recovery", "preflight", "--check-node"],
                db=self.path,
                rpc=None,
                out=output.append,
            )

        self.assertEqual(result, 0)
        self.assertIn("RPC connectivity: OK", "\n".join(output))

        runner_mock.assert_not_called()
        acquire_mock.assert_not_called()

        conn = db.connect_readonly(self.path)

        try:
            after = {}

            for table in (
                "pool_recovery_leases",
                "pool_recovery_schedules",
                "pool_recovery_runs",
            ):
                after[table] = [
                    tuple(row)
                    for row in conn.execute(
                        f"SELECT * FROM {table} ORDER BY rowid"
                    )
                ]
        finally:
            conn.close()

        self.assertEqual(
            before,
            after,
            "Node connectivity preflight modified recovery records",
        )

    def test_node_preflight_unexpected_rpc_exception(self):
        """Unexpected RPC failures must produce a controlled refusal."""
        from flowlab.pool_recovery_cli import recovery_node_connectivity

        class BrokenNode:
            def get_block_count(self):
                raise RuntimeError("PRIVATE_RPC_DETAIL")

        output = []

        result = recovery_node_connectivity(
            BrokenNode(),
            out=output.append,
        )

        self.assertEqual(result, 2)
        self.assertIn("RPC connectivity: FAILED", output)
        self.assertNotIn(
            "PRIVATE_RPC_DETAIL",
            "\n".join(output),
        )

    def test_node_preflight_rejects_boolean_height(self):
        """Boolean values are not valid blockchain heights."""
        from flowlab.pool_recovery_cli import recovery_node_connectivity

        class InvalidNode:
            def get_block_count(self):
                return True

        output = []

        result = recovery_node_connectivity(
            InvalidNode(),
            out=output.append,
        )

        self.assertEqual(result, 2)
        self.assertIn("RPC connectivity: FAILED", output)

    def test_node_preflight_rejects_negative_height(self):
        """Negative blockchain heights must be rejected."""
        from flowlab.pool_recovery_cli import recovery_node_connectivity

        class InvalidNode:
            def get_block_count(self):
                return -1

        output = []

        result = recovery_node_connectivity(
            InvalidNode(),
            out=output.append,
        )

        self.assertEqual(result, 2)
        self.assertIn("RPC connectivity: FAILED", output)

    def test_node_preflight_rejects_string_height(self):
        """Malformed RPC responses must fail closed."""
        from flowlab.pool_recovery_cli import recovery_node_connectivity

        class InvalidNode:
            def get_block_count(self):
                return "24449287"

        output = []

        result = recovery_node_connectivity(
            InvalidNode(),
            out=output.append,
        )

        self.assertEqual(result, 2)
        self.assertIn("RPC connectivity: FAILED", output)

    def test_node_preflight_missing_rpc_credentials(self):
        """Missing credentials must fail before RPC construction."""
        from unittest.mock import patch

        output = []

        config = {
            "rpc_user": "test-user",
            "rpc_host": "127.0.0.1",
            "rpc_port": 14022,
            "wallets": ["flab_pool_reserve"],
        }

        with (
            patch("flowlab.cli._load", return_value=config),
            patch("flowlab.cli.RpcClient") as client,
        ):
            result = main(
                ["recovery", "preflight", "--check-node"],
                db=self.path,
                out=output.append,
            )

        self.assertEqual(result, 2)
        client.assert_not_called()
        self.assertNotIn("test-user", "\n".join(output))

    def test_node_preflight_rejects_remote_rpc_host(self):
        """Remote RPC endpoints must be rejected."""
        from unittest.mock import patch

        output = []

        config = {
            "rpc_user": "test-user",
            "rpc_password": "PRIVATE_PASSWORD",
            "rpc_host": "192.168.1.100",
            "rpc_port": 14022,
            "wallets": ["flab_pool_reserve"],
        }

        with patch(
            "flowlab.cli._load",
            return_value=config,
        ):
            result = main(
                ["recovery", "preflight", "--check-node"],
                db=self.path,
                out=output.append,
            )

        self.assertEqual(result, 2)
        self.assertNotIn(
            "PRIVATE_PASSWORD",
            "\n".join(output),
        )

    def test_node_preflight_invalid_database_skips_rpc(self):
        """Invalid schema must be rejected before loading credentials."""
        from unittest.mock import patch

        output = []

        with (
            patch(
                "flowlab.pool_recovery_cli.validate_recovery_database",
                return_value=False,
            ),
            patch("flowlab.cli._load") as loader,
            patch("flowlab.cli.RpcClient") as client,
        ):
            result = main(
                ["recovery", "preflight", "--check-node"],
                db=self.path,
                out=output.append,
            )

        self.assertEqual(result, 2)
        loader.assert_not_called()
        client.assert_not_called()

    def test_node_preflight_corrupt_database_fail_closed(self):
        """Corrupt SQLite must fail before credential loading or RPC."""
        from unittest.mock import patch
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "corrupt.db"
            path.write_bytes(b"NOT_A_SQLITE_DATABASE")

            before = path.read_bytes()
            output = []

            with (
                patch("flowlab.cli._load") as loader,
                patch("flowlab.cli.RpcClient") as client,
                patch(
                    "flowlab.pool_recovery_runner."
                    "run_persistent_recovery_pass"
                ) as runner,
            ):
                result = main(
                    ["recovery", "preflight", "--check-node"],
                    db=str(path),
                    out=output.append,
                )

            self.assertEqual(result, 2)
            self.assertEqual(path.read_bytes(), before)
            loader.assert_not_called()
            client.assert_not_called()
            runner.assert_not_called()

    def test_node_preflight_missing_database_fail_closed(self):
        """Missing databases must never be created by preflight."""
        from unittest.mock import patch
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.db"
            output = []

            with (
                patch("flowlab.cli._load") as loader,
                patch("flowlab.cli.RpcClient") as client,
            ):
                result = main(
                    ["recovery", "preflight", "--check-node"],
                    db=str(path),
                    out=output.append,
                )

            self.assertEqual(result, 2)
            self.assertFalse(path.exists())
            loader.assert_not_called()
            client.assert_not_called()

    def test_node_preflight_schema_v1_fail_closed(self):
        """Legacy schema V1 must not be migrated by preflight."""
        from unittest.mock import patch
        from flowlab import db

        conn = db.connect(self.path)

        try:
            conn.execute(
                "UPDATE meta SET value='1' "
                "WHERE key='schema_version'"
            )
        finally:
            conn.close()

        output = []

        with (
            patch("flowlab.cli._load") as loader,
            patch("flowlab.cli.RpcClient") as client,
        ):
            result = main(
                ["recovery", "preflight", "--check-node"],
                db=self.path,
                out=output.append,
            )

        self.assertEqual(result, 2)
        loader.assert_not_called()
        client.assert_not_called()

        conn = db.connect_readonly(self.path)

        try:
            version = conn.execute(
                "SELECT value FROM meta "
                "WHERE key='schema_version'"
            ).fetchone()

            self.assertEqual(version["value"], "1")
        finally:
            conn.close()

    def test_readiness_offline_validated(self):
        """Valid offline preflight reports OFFLINE_VALIDATED."""
        output = []

        result = main(
            ["recovery", "preflight"],
            db=self.path,
            out=output.append,
        )

        self.assertEqual(result, 0)
        self.assertIn(
            "Recovery readiness: OFFLINE_VALIDATED",
            output,
        )

    def test_readiness_node_connected(self):
        """Valid DB and successful node check report READY."""
        class FakeNode:
            def get_block_count(self):
                return 24449287

        output = []

        result = main(
            ["recovery", "preflight", "--check-node"],
            db=self.path,
            rpc=FakeNode(),
            out=output.append,
        )

        self.assertEqual(result, 0)
        self.assertIn("Recovery readiness: READY", output)
        self.assertIn("Financial actions authorized: NO", output)

    def test_readiness_node_failure(self):
        """RPC failure reports DEGRADED without authorizing recovery."""
        from flowlab.rpc import RpcError

        class FailedNode:
            def get_block_count(self):
                raise RpcError("SECRET_RPC_ERROR")

        output = []

        result = main(
            ["recovery", "preflight", "--check-node"],
            db=self.path,
            rpc=FailedNode(),
            out=output.append,
        )

        self.assertEqual(result, 2)
        self.assertIn("Recovery readiness: DEGRADED", output)
        self.assertNotIn("SECRET_RPC_ERROR", "\n".join(output))

    def test_readiness_invalid_database_blocked(self):
        """Invalid database reports BLOCKED without contacting RPC."""
        from unittest.mock import patch
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            missing = str(Path(directory) / "missing.db")
            output = []

            with (
                patch("flowlab.cli._load") as loader,
                patch("flowlab.cli.RpcClient") as client,
            ):
                result = main(
                    ["recovery", "preflight", "--check-node"],
                    db=missing,
                    out=output.append,
                )

            self.assertEqual(result, 2)
            self.assertIn("Recovery readiness: BLOCKED", output)
            loader.assert_not_called()
            client.assert_not_called()

    def test_readiness_exactly_one_offline(self):
        """Offline preflight must report exactly one readiness state."""
        output = []

        result = main(
            ["recovery", "preflight"],
            db=self.path,
            out=output.append,
        )

        readiness = [
            line for line in output
            if line.startswith("Recovery readiness:")
        ]

        self.assertEqual(result, 0)
        self.assertEqual(
            readiness,
            ["Recovery readiness: OFFLINE_VALIDATED"],
        )

    def test_readiness_exactly_one_connected(self):
        """Connected node reports READY without recovery authorization."""
        class FakeNode:
            def get_block_count(self):
                return 24449287

        output = []

        result = main(
            ["recovery", "preflight", "--check-node"],
            db=self.path,
            rpc=FakeNode(),
            out=output.append,
        )

        readiness = [
            line for line in output
            if line.startswith("Recovery readiness:")
        ]

        self.assertEqual(result, 0)
        self.assertEqual(readiness, ["Recovery readiness: READY"])
        self.assertIn("Financial actions authorized: NO", output)
        self.assertIn("Automatic recovery: DISABLED", output)

    def test_readiness_exactly_one_node_failure(self):
        """Failed connectivity reports DEGRADED exactly once."""
        from flowlab.rpc import RpcError

        class FailedNode:
            def get_block_count(self):
                raise RpcError("PRIVATE_NODE_ERROR")

        output = []

        result = main(
            ["recovery", "preflight", "--check-node"],
            db=self.path,
            rpc=FailedNode(),
            out=output.append,
        )

        readiness = [
            line for line in output
            if line.startswith("Recovery readiness:")
        ]

        self.assertEqual(result, 2)
        self.assertEqual(
            readiness,
            ["Recovery readiness: DEGRADED"],
        )
        self.assertNotIn(
            "PRIVATE_NODE_ERROR",
            "\n".join(output),
        )

    def test_readiness_reporting_preserves_recovery_records(self):
        """Readiness classification must never change recovery records."""
        from flowlab import db
        from unittest.mock import patch

        tables = (
            "pool_recovery_leases",
            "pool_recovery_schedules",
            "pool_recovery_runs",
        )

        def snapshot():
            conn = db.connect_readonly(self.path)

            try:
                return {
                    table: [
                        tuple(row)
                        for row in conn.execute(
                            f"SELECT * FROM {table} ORDER BY rowid"
                        )
                    ]
                    for table in tables
                }
            finally:
                conn.close()

        before = snapshot()

        class FakeNode:
            def get_block_count(self):
                return 24449287

        output = []

        with patch(
            "flowlab.pool_recovery_runner."
            "run_persistent_recovery_pass"
        ) as runner:
            result = main(
                ["recovery", "preflight", "--check-node"],
                db=self.path,
                rpc=FakeNode(),
                out=output.append,
            )

        self.assertEqual(result, 0)
        self.assertIn("Recovery readiness: READY", output)
        self.assertEqual(before, snapshot())
        runner.assert_not_called()
if __name__ == "__main__":
    unittest.main()
