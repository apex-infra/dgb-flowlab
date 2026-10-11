"""Regression tests for the committed M2 -> M3 database migration."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from flowlab import db


class PoolObservationMigrationTests(unittest.TestCase):

    def test_committed_m2_database_upgrades_to_v4(self):
        old_source = subprocess.check_output(
            ["git", "show", "61e869c:flowlab/db.py"],
            text=True,
        )

        namespace = {"__name__": "flowlab_m2_db"}
        exec(compile(old_source, "m2_db.py", "exec"), namespace)

        with tempfile.TemporaryDirectory() as tmp:
            conn = namespace["connect"](
                str(Path(tmp) / "upgrade.db")
            )

            try:
                namespace["init"](conn)

                conn.execute("""
                    INSERT INTO pools(
                        id, reserve_wallet, fee_wallet,
                        created_at, updated_at
                    )
                    VALUES (
                        'pool-1', 'flab_pool_reserve',
                        'flab_pool_fees', 't', 't'
                    )
                """)

                conn.execute("""
                    INSERT INTO pool_sessions(
                        id, pool_id, fee_basis_points, created_at
                    )
                    VALUES ('session-1', 'pool-1', 500, 't')
                """)

                conn.execute("""
                    INSERT INTO pool_payout_instructions(
                        session_id, position,
                        payout_address, allocation_bps
                    )
                    VALUES ('session-1', 1, 'test-payout', 10000)
                """)

                conn.execute("""
                    INSERT INTO pool_registration_seals(
                        session_id, completed_at
                    )
                    VALUES ('session-1', 't')
                """)

                conn.execute("""
                    INSERT INTO pool_deposit_addresses(
                        session_id, wallet, request_label,
                        state, created_at, updated_at
                    )
                    VALUES (
                        'session-1', 'flab_pool_reserve',
                        'test-label', 'REQUESTING', 't', 't'
                    )
                """)

                tables = (
                    "pools",
                    "pool_sessions",
                    "pool_payout_instructions",
                    "pool_registration_seals",
                    "pool_deposit_addresses",
                )

                before = {
                    name: [
                        tuple(row)
                        for row in conn.execute(
                            f"SELECT * FROM {name} ORDER BY rowid"
                        )
                    ]
                    for name in tables
                }

                self.assertEqual(
                    conn.execute(
                        "SELECT value FROM meta "
                        "WHERE key='schema_version'"
                    ).fetchone()[0],
                    "3",
                )

                db.init(conn)

                after = {
                    name: [
                        tuple(row)
                        for row in conn.execute(
                            f"SELECT * FROM {name} ORDER BY rowid"
                        )
                    ]
                    for name in tables
                }

                self.assertEqual(before, after)

                self.assertEqual(
                    conn.execute(
                        "SELECT value FROM meta "
                        "WHERE key='schema_version'"
                    ).fetchone()[0],
                    "5",
                )

                db.validate_pool_schema(conn)
                db.validate_pool_registration_schema(conn)
                db.validate_pool_observation_schema(conn)

                self.assertEqual(
                    conn.execute(
                        "PRAGMA integrity_check"
                    ).fetchone()[0],
                    "ok",
                )

                self.assertEqual(
                    list(conn.execute("PRAGMA foreign_key_check")),
                    [],
                )

                # Reopening must not recreate tables or triggers.
                db.init(conn)

                self.assertEqual(
                    conn.execute(
                        "SELECT value FROM meta "
                        "WHERE key='schema_version'"
                    ).fetchone()[0],
                    "5",
                )

            finally:
                conn.close()


    def test_financial_records_survive_migration(self):
        from flowlab import pool_ledger

        old_source = subprocess.check_output(
            ["git", "show", "61e869c:flowlab/db.py"],
            text=True,
        )
        namespace = {"__name__": "flowlab_m2_db"}
        exec(compile(old_source, "m2_db.py", "exec"), namespace)

        with tempfile.TemporaryDirectory() as tmp:
            conn = namespace["connect"](str(Path(tmp) / "financial.db"))

            try:
                namespace["init"](conn)

                pool_ledger.create_pool(
                    conn,
                    pool_id="financial-pool",
                    reserve_wallet="reserve",
                    fee_wallet="fees",
                )

                pool_ledger.create_session(
                    conn,
                    pool_id="financial-pool",
                    session_id="financial-session",
                    fee_basis_points=500,
                )

                for txid, amount in (
                    ("a" * 64, 100_000_000),
                    ("b" * 64, 250_000_000),
                ):
                    pool_ledger.record_deposit(
                        conn,
                        pool_id="financial-pool",
                        session_id="financial-session",
                        deposit_sats=amount,
                        fee_basis_points=500,
                        deposit_outpoint=(txid, 0),
                    )

                tables = (
                    "pool_deposits",
                    "pool_journal_transactions",
                    "pool_journal_entries",
                )

                def snapshot():
                    return {
                        table: [
                            tuple(row)
                            for row in conn.execute(
                                f"SELECT * FROM {table} ORDER BY id"
                            )
                        ]
                        for table in tables
                    }

                before = snapshot()

                balances_before = pool_ledger.session_balances(
                    conn,
                    pool_id="financial-pool",
                    session_id="financial-session",
                )

                self.assertEqual(
                    balances_before["deposit_sats"],
                    350_000_000,
                )
                self.assertEqual(
                    balances_before["fee_sats"],
                    17_500_000,
                )
                self.assertEqual(
                    balances_before["participant_liability_sats"],
                    332_500_000,
                )

                db.init(conn)

                self.assertEqual(before, snapshot())

                self.assertEqual(
                    balances_before,
                    pool_ledger.session_balances(
                        conn,
                        pool_id="financial-pool",
                        session_id="financial-session",
                    ),
                )

                self.assertEqual(
                    conn.execute(
                        "SELECT value FROM meta "
                        "WHERE key='schema_version'"
                    ).fetchone()[0],
                    "5",
                )

                self.assertEqual(
                    conn.execute(
                        "PRAGMA integrity_check"
                    ).fetchone()[0],
                    "ok",
                )

                self.assertEqual(
                    list(conn.execute("PRAGMA foreign_key_check")),
                    [],
                )

            finally:
                conn.close()

    def test_failed_migration_rolls_back_atomically(self):
        import sqlite3

        old_source = subprocess.check_output(
            ["git", "show", "61e869c:flowlab/db.py"],
            text=True,
        )
        namespace = {"__name__": "flowlab_m2_db"}
        exec(compile(old_source, "m2_db.py", "exec"), namespace)

        with tempfile.TemporaryDirectory() as tmp:
            conn = namespace["connect"](str(Path(tmp) / "rollback.db"))

            try:
                namespace["init"](conn)

                # Deliberately conflict with the new v4 table name.
                conn.execute(
                    "CREATE VIEW pool_deposit_observations "
                    "AS SELECT 1 AS id"
                )

                def schema_snapshot():
                    return [
                        tuple(row)
                        for row in conn.execute(
                            "SELECT type, name, sql FROM sqlite_master "
                            "ORDER BY type, name"
                        )
                    ]

                before = schema_snapshot()

                with self.assertRaises(sqlite3.OperationalError):
                    db.init(conn)

                self.assertEqual(before, schema_snapshot())

                self.assertEqual(
                    conn.execute(
                        "SELECT value FROM meta "
                        "WHERE key='schema_version'"
                    ).fetchone()[0],
                    "3",
                )

                self.assertIsNone(
                    conn.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='table' "
                        "AND name='pool_observation_history'"
                    ).fetchone()
                )

                self.assertEqual(
                    conn.execute(
                        "PRAGMA integrity_check"
                    ).fetchone()[0],
                    "ok",
                )

            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
