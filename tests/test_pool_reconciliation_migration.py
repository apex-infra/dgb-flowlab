"""Regression tests for upgrading committed M4.1 schema v5 to v6."""

import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from flowlab import db, pool_ledger


M4_COMMIT = "0c8f997"


def make_v5_database(path):
    source = subprocess.check_output(
        ["git", "show", f"{M4_COMMIT}:flowlab/db.py"],
        text=True,
    )

    namespace = {"__name__": "flowlab_m4_db"}
    exec(compile(source, "m4_db.py", "exec"), namespace)

    conn = namespace["connect"](str(path))
    namespace["init"](conn)

    version = conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()[0]

    if version != "5":
        raise AssertionError(f"Expected historical v5, got {version}")

    return conn


class ReconciliationMigrationTests(unittest.TestCase):

    def test_existing_v5_verification_history_survives_upgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = make_v5_database(Path(tmp) / "upgrade.db")

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
                        address, state, created_at, updated_at
                    )
                    VALUES (
                        'migration-session', 'flab_pool_reserve',
                        'label', NULL, 'REQUESTING', 't', 't'
                    )
                    """
                )

                conn.execute(
                    """
                    UPDATE pool_deposit_addresses
                    SET address='deposit-address', state='READY'
                    WHERE session_id='migration-session'
                    """
                )

                conn.execute(
                    """
                    INSERT INTO pool_deposit_observations(
                        pool_id, session_id, txid, vout,
                        amount_sats, address, confirmations,
                        blockhash, state, first_seen_at, last_seen_at
                    )
                    VALUES (
                        'migration-pool', 'migration-session',
                        ?, 0, 125000000, 'deposit-address',
                        6, ?, 'CONFIRMED', 't', 't'
                    )
                    """,
                    ("a" * 64, "b" * 64),
                )

                observation_id = conn.execute(
                    "SELECT id FROM pool_deposit_observations"
                ).fetchone()[0]

                conn.execute(
                    """
                    INSERT INTO pool_verification_events(
                        observation_id, state, reason,
                        confirmations, blockhash, verified_at
                    )
                    VALUES (?, 'VERIFIED', 'Historical verification',
                            6, ?, 't')
                    """,
                    (observation_id, "b" * 64),
                )

                tables = (
                    "pools",
                    "pool_sessions",
                    "pool_deposit_observations",
                    "pool_verification_events",
                )

                def snapshot():
                    return {
                        name: [
                            tuple(row)
                            for row in conn.execute(
                                f"SELECT * FROM {name} ORDER BY rowid"
                            )
                        ]
                        for name in tables
                    }

                before = snapshot()

                db.init(conn)

                self.assertEqual(snapshot(), before)

                self.assertEqual(
                    conn.execute(
                        "SELECT value FROM meta "
                        "WHERE key='schema_version'"
                    ).fetchone()[0],
                    "6",
                )

                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM pool_reconciliation_events"
                    ).fetchone()[0],
                    0,
                )

                self.assertEqual(
                    conn.execute("PRAGMA integrity_check").fetchone()[0],
                    "ok",
                )

                self.assertEqual(
                    list(conn.execute("PRAGMA foreign_key_check")),
                    [],
                )

                db.init(conn)

            finally:
                conn.close()


    def test_v6_schema_integrity_and_immutability(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(str(Path(tmp) / "fresh.db"))

            try:
                db.init(conn)
                db.validate_pool_reconciliation_schema(conn)

                self.assertEqual(
                    conn.execute(
                        "SELECT value FROM meta "
                        "WHERE key='schema_version'"
                    ).fetchone()[0],
                    "6",
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

                triggers = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='trigger'"
                    )
                }

                self.assertIn(
                    "pool_reconciliation_events_no_update",
                    triggers,
                )
                self.assertIn(
                    "pool_reconciliation_events_no_delete",
                    triggers,
                )

                db.init(conn)

            finally:
                conn.close()

    def test_failed_v5_migration_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = make_v5_database(Path(tmp) / "rollback.db")

            try:
                # Deliberately occupy the new table's name.
                conn.execute(
                    "CREATE VIEW pool_reconciliation_events "
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
                    "5",
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
