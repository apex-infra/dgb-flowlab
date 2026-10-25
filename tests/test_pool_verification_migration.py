"""Regression tests for the committed M3 schema-v4 to schema-v5 migration."""

import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from flowlab import db, pool_ledger


M3_COMMIT = "f9404ed"


def make_v4_database(path):
    source = subprocess.check_output(
        ["git", "show", f"{M3_COMMIT}:flowlab/db.py"],
        text=True,
    )
    namespace = {"__name__": "flowlab_m3_db"}
    exec(compile(source, "m3_db.py", "exec"), namespace)

    conn = namespace["connect"](str(path))
    namespace["init"](conn)

    assert conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()[0] == "4"

    return conn


def snapshot(conn, tables):
    return {
        table: [
            tuple(row)
            for row in conn.execute(
                f"SELECT * FROM {table} ORDER BY rowid"
            )
        ]
        for table in tables
    }


class VerificationMigrationTests(unittest.TestCase):

    def test_populated_v4_database_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = make_v4_database(Path(tmp) / "upgrade.db")

            try:
                pool_ledger.create_pool(
                    conn,
                    pool_id="migration-pool",
                    reserve_wallet="flab_pool_reserve",
                    fee_wallet="flab_pool_fees",
                )

                pool_ledger.create_session(
                    conn,
                    pool_id="migration-pool",
                    session_id="migration-session",
                    fee_basis_points=500,
                )

                conn.execute(
                    """
                    INSERT INTO pool_payout_instructions(
                        session_id, position,
                        payout_address, allocation_bps
                    )
                    VALUES ('migration-session', 1, 'payout', 10000)
                    """
                )

                conn.execute(
                    """
                    INSERT INTO pool_registration_seals(
                        session_id, completed_at
                    )
                    VALUES ('migration-session', 't')
                    """
                )

                conn.execute(
                    """
                    INSERT INTO pool_deposit_addresses(
                        session_id, wallet, request_label,
                        state, created_at, updated_at
                    )
                    VALUES (
                        'migration-session', 'flab_pool_reserve',
                        'migration-label', 'REQUESTING', 't', 't'
                    )
                    """
                )

                conn.execute(
                    """
                    UPDATE pool_deposit_addresses
                    SET address='migration-address',
                        state='READY'
                    WHERE session_id='migration-session'
                    """
                )

                txid = "a" * 64
                blockhash = "b" * 64

                conn.execute(
                    """
                    INSERT INTO pool_deposit_observations(
                        pool_id, session_id, txid, vout,
                        amount_sats, address, confirmations,
                        blockhash, state, first_seen_at, last_seen_at
                    )
                    VALUES (?, ?, ?, 0, 100000000, ?, 6, ?,
                            'CONFIRMED', 't', 't')
                    """,
                    (
                        "migration-pool",
                        "migration-session",
                        txid,
                        "migration-address",
                        blockhash,
                    ),
                )

                observation_id = conn.execute(
                    "SELECT id FROM pool_deposit_observations"
                ).fetchone()[0]

                conn.execute(
                    """
                    INSERT INTO pool_observation_history(
                        observation_id, confirmations,
                        blockhash, state, observed_at
                    )
                    VALUES (?, 6, ?, 'CONFIRMED', 't')
                    """,
                    (observation_id, blockhash),
                )

                pool_ledger.record_deposit(
                    conn,
                    pool_id="migration-pool",
                    session_id="migration-session",
                    deposit_sats=100_000_000,
                    fee_basis_points=500,
                    deposit_outpoint=(txid, 0),
                )

                tables = (
                    "pools",
                    "pool_sessions",
                    "pool_payout_instructions",
                    "pool_registration_seals",
                    "pool_deposit_addresses",
                    "pool_deposits",
                    "pool_journal_transactions",
                    "pool_journal_entries",
                    "pool_deposit_observations",
                    "pool_observation_history",
                )

                before = snapshot(conn, tables)

                balances_before = pool_ledger.session_balances(
                    conn,
                    pool_id="migration-pool",
                    session_id="migration-session",
                )

                journal_before = pool_ledger.journal_totals(
                    conn,
                    pool_id="migration-pool",
                )

                db.init(conn)

                self.assertEqual(snapshot(conn, tables), before)

                self.assertEqual(
                    pool_ledger.session_balances(
                        conn,
                        pool_id="migration-pool",
                        session_id="migration-session",
                    ),
                    balances_before,
                )

                self.assertEqual(
                    pool_ledger.journal_totals(
                        conn,
                        pool_id="migration-pool",
                    ),
                    journal_before,
                )

                self.assertEqual(
                    conn.execute(
                        "SELECT value FROM meta "
                        "WHERE key='schema_version'"
                    ).fetchone()[0],
                    "7",
                )

                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM pool_verification_events"
                    ).fetchone()[0],
                    0,
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

                db.init(conn)

            finally:
                conn.close()

    def test_failed_v4_migration_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = make_v4_database(Path(tmp) / "rollback.db")

            try:
                conn.execute(
                    "CREATE VIEW pool_verification_events "
                    "AS SELECT 1 AS id"
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
                        "SELECT value FROM meta "
                        "WHERE key='schema_version'"
                    ).fetchone()[0],
                    "4",
                )

                self.assertIsNone(
                    conn.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='index' AND "
                        "name='pool_verification_events_observation_idx'"
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
