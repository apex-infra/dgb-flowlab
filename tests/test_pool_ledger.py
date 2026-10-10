"""Mixer Lab persistent pool and database migration tests."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from flowlab import db


class PoolMigrationTests(unittest.TestCase):

    def test_v1_database_migrates_without_losing_experiments(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "flowlab.db"
            conn = db.connect(str(path))

            try:
                # Establish a version-1 database using the existing schema.
                conn.executescript(db.SCHEMA)

                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', '1')"
                )

                conn.execute(
                    """
                    INSERT INTO experiments(id, description, state, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        "EXP-MIGRATION-TEST",
                        "Existing experiment",
                        "COMPLETE",
                        "2026-10-10T00:00:00Z",
                    ),
                )

                # Future implementation must upgrade the database.
                db.init(conn)

                version = conn.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()[0]

                self.assertEqual(version, "2")

                pool_table = conn.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type='table' AND name='pools'
                    """
                ).fetchone()

                self.assertIsNotNone(
                    pool_table,
                    "Schema v2 must create the persistent pools table",
                )


                existing = conn.execute(
                    "SELECT description, state FROM experiments WHERE id=?",
                    ("EXP-MIGRATION-TEST",),
                ).fetchone()

                self.assertIsNotNone(existing)
                self.assertEqual(existing["description"], "Existing experiment")
                self.assertEqual(existing["state"], "COMPLETE")

                # Reinitialization must be safe.
                db.init(conn)

                self.assertEqual(
                    conn.execute("PRAGMA integrity_check").fetchone()[0],
                    "ok",
                )

            finally:
                conn.close()


    def test_fresh_database_initializes_at_v2(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fresh.db"
            conn = db.connect(str(path))

            try:
                db.init(conn)

                version = conn.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()[0]

                self.assertEqual(version, "2")

                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }

                self.assertIn("experiments", tables)
                self.assertIn("pools", tables)

                db.init(conn)

                self.assertEqual(
                    conn.execute("PRAGMA integrity_check").fetchone()[0],
                    "ok",
                )
            finally:
                conn.close()


    def test_failed_migration_preserves_v1_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(str(Path(tmp) / "rollback.db"))
            try:
                conn.executescript(db.SCHEMA)
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', '1')"
                )
                conn.execute(
                    """
                    INSERT INTO experiments(id, description, state, created_at)
                    VALUES ('EXP-ROLLBACK', 'Preserve me', 'COMPLETE',
                            '2026-10-10T00:00:00Z')
                    """
                )

                # A conflicting object forces CREATE TABLE pools to fail.
                conn.execute("CREATE VIEW pools AS SELECT 1 AS id")

                with self.assertRaises(sqlite3.OperationalError):
                    db.init(conn)

                version = conn.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()[0]

                self.assertEqual(version, "1")

                experiment = conn.execute(
                    "SELECT description FROM experiments WHERE id='EXP-ROLLBACK'"
                ).fetchone()

                self.assertEqual(experiment["description"], "Preserve me")

                self.assertEqual(
                    conn.execute("PRAGMA integrity_check").fetchone()[0],
                    "ok",
                )
            finally:
                conn.close()


    def test_incomplete_v2_schema_is_rejected(self):
        """Schema version alone must not establish database integrity."""

        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(str(Path(tmp) / "incomplete.db"))

            try:
                db.init(conn)

                # Simulate an incomplete or damaged schema-v2 database.
                conn.execute("DROP TABLE pool_journal_entries")

                with self.assertRaises(RuntimeError):
                    db.init(conn)

            finally:
                conn.close()


    def test_fresh_initialization_failure_rolls_back_everything(self):
        """A failed first initialization must leave no partial schema."""

        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(str(Path(tmp) / "atomic-init.db"))

            try:
                # This name conflicts with the required pools table.
                conn.execute("CREATE VIEW pools AS SELECT 1 AS id")

                with self.assertRaises(sqlite3.OperationalError):
                    db.init(conn)

                objects = {
                    row["name"]
                    for row in conn.execute(
                        """
                        SELECT name FROM sqlite_master
                        WHERE type IN ('table', 'view')
                        """
                    )
                }

                # The pre-existing conflicting view remains, but
                # FlowLab initialization must not leave partial tables.
                self.assertIn("pools", objects)
                self.assertNotIn("experiments", objects)
                self.assertNotIn("meta", objects)
                self.assertNotIn("pool_sessions", objects)

            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
