"""M7.2 Mixer Lab recovery persistence migration tests."""

import unittest
from pathlib import Path

from flowlab import db


class RecoveryMigrationTests(unittest.TestCase):

    def test_fresh_database_has_recovery_schema(self):
        conn = db.connect(":memory:")

        try:
            db.init(conn)

            version = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()[0]

            self.assertEqual(version, "7")

            tables = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }

            self.assertIn("pool_recovery_leases", tables)
            self.assertIn("pool_recovery_schedules", tables)
            self.assertIn("pool_recovery_runs", tables)

            self.assertEqual(
                conn.execute(
                    "PRAGMA integrity_check"
                ).fetchone()[0],
                "ok",
            )

        finally:
            conn.close()


    def test_existing_v6_database_upgrades(self):
        import subprocess
        import tempfile

        from flowlab import pool_ledger

        source = subprocess.check_output(
            ["git", "show", "53a22c4:flowlab/db.py"],
            text=True,
        )

        namespace = {"__name__": "historical_flowlab_db"}
        exec(compile(source, "historical_db.py", "exec"), namespace)

        with tempfile.TemporaryDirectory() as tmp:
            database_path = str(Path(tmp) / "migration.db")

            conn = namespace["connect"](database_path)

            try:
                namespace["init"](conn)

                pool_ledger.create_pool(
                    conn,
                    pool_id="historical-pool",
                    reserve_wallet="historical-reserve",
                    fee_wallet="historical-fees",
                )

                self.assertEqual(
                    conn.execute(
                        "SELECT value FROM meta WHERE key='schema_version'"
                    ).fetchone()[0],
                    "6",
                )

                db.init(conn)

                self.assertEqual(
                    conn.execute(
                        "SELECT value FROM meta WHERE key='schema_version'"
                    ).fetchone()[0],
                    "7",
                )

                pool = conn.execute(
                    "SELECT reserve_wallet, fee_wallet FROM pools WHERE id = ?",
                    ("historical-pool",),
                ).fetchone()

                self.assertIsNotNone(pool)
                self.assertEqual(pool["reserve_wallet"], "historical-reserve")
                self.assertEqual(pool["fee_wallet"], "historical-fees")

                self.assertEqual(
                    conn.execute("PRAGMA integrity_check").fetchone()[0],
                    "ok",
                )

            finally:
                conn.close()


    def test_failed_v6_migration_rolls_back(self):
        import sqlite3
        import subprocess
        import tempfile

        source = subprocess.check_output(
            ["git", "show", "53a22c4:flowlab/db.py"],
            text=True,
        )

        namespace = {"__name__": "historical_flowlab_db"}
        exec(compile(source, "historical_db.py", "exec"), namespace)

        with tempfile.TemporaryDirectory() as tmp:
            conn = namespace["connect"](
                str(Path(tmp) / "rollback.db")
            )

            try:
                namespace["init"](conn)

                conn.execute(
                    "CREATE VIEW pool_recovery_runs AS SELECT 1 AS id"
                )

                before = [
                    tuple(row)
                    for row in conn.execute(
                        "SELECT type, name, sql FROM sqlite_master "
                        "ORDER BY type, name"
                    )
                ]

                with self.assertRaises(sqlite3.OperationalError):
                    db.init(conn)

                after = [
                    tuple(row)
                    for row in conn.execute(
                        "SELECT type, name, sql FROM sqlite_master "
                        "ORDER BY type, name"
                    )
                ]

                self.assertEqual(before, after)

                self.assertEqual(
                    conn.execute(
                        "SELECT value FROM meta WHERE key='schema_version'"
                    ).fetchone()[0],
                    "6",
                )

                self.assertEqual(
                    conn.execute("PRAGMA integrity_check").fetchone()[0],
                    "ok",
                )

            finally:
                conn.close()


    def test_recovery_history_is_immutable(self):
        import sqlite3

        conn = db.connect(":memory:")

        try:
            db.init(conn)

            conn.execute(
                """
                INSERT INTO pools(
                    id, state, reserve_wallet, fee_wallet,
                    created_at, updated_at
                )
                VALUES ('test-pool', 'PAUSED', 'reserve', 'fees', 't', 't')
                """
            )

            conn.execute(
                """
                INSERT INTO pool_sessions(
                    id, pool_id, fee_basis_points, created_at
                )
                VALUES ('test-session', 'test-pool', 500, 't')
                """
            )

            conn.execute(
                """
                INSERT INTO pool_recovery_runs(
                    session_id, lease_name, owner_token,
                    fencing_token, event, recorded_at
                )
                VALUES (
                    'test-session', 'test-lease', 'test-owner',
                    1, 'STARTED', 't'
                )
                """
            )

            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE pool_recovery_runs "
                    "SET event='COMPLETED' WHERE id=1"
                )

            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "DELETE FROM pool_recovery_runs WHERE id=1"
                )

            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM pool_recovery_runs"
                ).fetchone()[0],
                1,
            )

        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
