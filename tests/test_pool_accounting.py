"""Mixer Lab balanced financial accounting tests."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from flowlab import db
from flowlab import pool_ledger


class PoolAccountingTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(str(Path(self.tmp.name) / "ledger.db"))
        db.init(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_confirmed_deposit_creates_balanced_obligations(self):
        """175 DGB at 5% produces 166.25 DGB liability and 8.75 DGB revenue."""

        deposit_sats = 17_500_000_000
        fee_sats = 875_000_000
        entitlement_sats = 16_625_000_000

        pool_ledger.create_pool(
            self.conn,
            pool_id="POOL-TEST",
            reserve_wallet="flab_pool_reserve",
            fee_wallet="flab_pool_fees",
        )

        pool_ledger.create_session(
            self.conn,
            pool_id="POOL-TEST",
            session_id="SESSION-001",
            fee_basis_points=500,
        )

        pool_ledger.record_deposit(
            self.conn,
            pool_id="POOL-TEST",
            session_id="SESSION-001",
            deposit_sats=deposit_sats,
            fee_basis_points=500,
            deposit_outpoint=("a" * 64, 0),
        )

        balances = pool_ledger.session_balances(
            self.conn,
            pool_id="POOL-TEST",
            session_id="SESSION-001",
        )

        self.assertEqual(balances["deposit_sats"], deposit_sats)
        self.assertEqual(balances["fee_sats"], fee_sats)
        self.assertEqual(
            balances["participant_liability_sats"],
            entitlement_sats,
        )

        self.assertEqual(
            balances["deposit_sats"],
            balances["fee_sats"]
            + balances["participant_liability_sats"],
        )

        # The financial journal must balance exactly.
        journal = pool_ledger.journal_totals(
            self.conn,
            pool_id="POOL-TEST",
        )

        self.assertEqual(
            journal["total_debits_sats"],
            journal["total_credits_sats"],
        )

    def test_duplicate_deposit_cannot_create_duplicate_credit(self):
        """One blockchain output cannot be credited twice."""

        pool_ledger.create_pool(
            self.conn,
            pool_id="POOL-TEST",
            reserve_wallet="flab_pool_reserve",
            fee_wallet="flab_pool_fees",
        )

        pool_ledger.create_session(
            self.conn,
            pool_id="POOL-TEST",
            session_id="SESSION-001",
            fee_basis_points=500,
        )

        arguments = dict(
            pool_id="POOL-TEST",
            session_id="SESSION-001",
            deposit_sats=17_500_000_000,
            fee_basis_points=500,
            deposit_outpoint=("b" * 64, 0),
        )

        pool_ledger.record_deposit(self.conn, **arguments)

        with self.assertRaises(pool_ledger.DuplicateDepositError):
            pool_ledger.record_deposit(self.conn, **arguments)

        balances = pool_ledger.session_balances(
            self.conn,
            pool_id="POOL-TEST",
            session_id="SESSION-001",
        )

        self.assertEqual(
            balances["deposit_sats"],
            17_500_000_000,
        )


    def test_posted_journal_is_immutable(self):
        pool_ledger.create_pool(
            self.conn,
            pool_id="POOL-TEST",
            reserve_wallet="flab_pool_reserve",
            fee_wallet="flab_pool_fees",
        )

        pool_ledger.create_session(
            self.conn,
            pool_id="POOL-TEST",
            session_id="SESSION-001",
            fee_basis_points=500,
        )

        pool_ledger.record_deposit(
            self.conn,
            pool_id="POOL-TEST",
            session_id="SESSION-001",
            deposit_sats=17_500_000_000,
            fee_basis_points=500,
            deposit_outpoint=("c" * 64, 0),
        )

        journal_id = self.conn.execute(
            "SELECT id FROM pool_journal_transactions"
        ).fetchone()["id"]

        entry_id = self.conn.execute(
            "SELECT id FROM pool_journal_entries LIMIT 1"
        ).fetchone()["id"]

        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE pool_journal_entries SET debit_sats=1 WHERE id=?",
                (entry_id,),
            )

        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "DELETE FROM pool_journal_entries WHERE id=?",
                (entry_id,),
            )

        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "DELETE FROM pool_journal_transactions WHERE id=?",
                (journal_id,),
            )

        self.assertEqual(
            self.conn.execute(
                "SELECT state FROM pool_journal_transactions WHERE id=?",
                (journal_id,),
            ).fetchone()["state"],
            "POSTED",
        )


    def test_unbalanced_journal_cannot_be_posted(self):
        pool_ledger.create_pool(
            self.conn,
            pool_id="POOL-TEST",
            reserve_wallet="flab_pool_reserve",
            fee_wallet="flab_pool_fees",
        )

        journal_id = self.conn.execute(
            """
            INSERT INTO pool_journal_transactions(
                pool_id, created_at
            )
            VALUES (?, ?)
            """,
            ("POOL-TEST", "2026-10-10T00:00:00Z"),
        ).lastrowid

        self.conn.execute(
            """
            INSERT INTO pool_journal_entries(
                transaction_id, account, debit_sats, credit_sats
            )
            VALUES (?, ?, ?, ?)
            """,
            (journal_id, "pool_assets", 17_500_000_000, 0),
        )

        self.conn.execute(
            """
            INSERT INTO pool_journal_entries(
                transaction_id, account, debit_sats, credit_sats
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                journal_id,
                "participant_liability:SESSION-001",
                0,
                16_625_000_000,
            ),
        )

        # Missing the 8.75 DGB fee credit means the journal
        # does not balance.
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                UPDATE pool_journal_transactions
                SET state = 'POSTED'
                WHERE id = ?
                """,
                (journal_id,),
            )

        state = self.conn.execute(
            """
            SELECT state
            FROM pool_journal_transactions
            WHERE id = ?
            """,
            (journal_id,),
        ).fetchone()["state"]

        self.assertEqual(state, "DRAFT")


    def test_deposit_failure_rolls_back_all_financial_records(self):
        pool_ledger.create_pool(
            self.conn,
            pool_id="POOL-TEST",
            reserve_wallet="flab_pool_reserve",
            fee_wallet="flab_pool_fees",
        )

        pool_ledger.create_session(
            self.conn,
            pool_id="POOL-TEST",
            session_id="SESSION-001",
            fee_basis_points=500,
        )

        # Simulate a database failure while inserting journal entries.
        self.conn.execute("""
            CREATE TRIGGER simulate_journal_failure
            BEFORE INSERT ON pool_journal_entries
            BEGIN
                SELECT RAISE(ABORT, 'simulated journal failure');
            END
        """)

        with self.assertRaises(sqlite3.IntegrityError):
            pool_ledger.record_deposit(
                self.conn,
                pool_id="POOL-TEST",
                session_id="SESSION-001",
                deposit_sats=17_500_000_000,
                fee_basis_points=500,
                deposit_outpoint=("d" * 64, 0),
            )

        for table in (
            "pool_deposits",
            "pool_journal_transactions",
            "pool_journal_entries",
        ):
            count = self.conn.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]

            self.assertEqual(
                count, 0,
                f"{table} retained records after failed transaction",
            )

        self.assertEqual(
            self.conn.execute("PRAGMA integrity_check").fetchone()[0],
            "ok",
        )


    def test_multiple_deposits_use_cumulative_session_fee(self):
        """Fee rounding uses the total accepted session deposit."""

        pool_ledger.create_pool(
            self.conn,
            pool_id="POOL-TEST",
            reserve_wallet="flab_pool_reserve",
            fee_wallet="flab_pool_fees",
        )

        pool_ledger.create_session(
            self.conn,
            pool_id="POOL-TEST",
            session_id="SESSION-001",
            fee_basis_points=500,
        )

        # Each deposit rounds to one satoshi individually,
        # but 5% of the combined 20 satoshis is one satoshi.
        for txid in ("e" * 64, "f" * 64):
            pool_ledger.record_deposit(
                self.conn,
                pool_id="POOL-TEST",
                session_id="SESSION-001",
                deposit_sats=10,
                fee_basis_points=500,
                deposit_outpoint=(txid, 0),
            )

        balances = pool_ledger.session_balances(
            self.conn,
            pool_id="POOL-TEST",
            session_id="SESSION-001",
        )

        self.assertEqual(balances["deposit_sats"], 20)
        self.assertEqual(balances["fee_sats"], 1)
        self.assertEqual(
            balances["participant_liability_sats"], 19
        )

        journal = pool_ledger.journal_totals(
            self.conn,
            pool_id="POOL-TEST",
        )

        self.assertEqual(
            journal["total_debits_sats"],
            journal["total_credits_sats"],
        )


    def test_credited_deposit_is_immutable(self):
        """A credited deposit cannot change after journal posting."""

        pool_ledger.create_pool(
            self.conn,
            pool_id="POOL-TEST",
            reserve_wallet="flab_pool_reserve",
            fee_wallet="flab_pool_fees",
        )

        pool_ledger.create_session(
            self.conn,
            pool_id="POOL-TEST",
            session_id="SESSION-001",
            fee_basis_points=500,
        )

        deposit_id = pool_ledger.record_deposit(
            self.conn,
            pool_id="POOL-TEST",
            session_id="SESSION-001",
            deposit_sats=17_500_000_000,
            fee_basis_points=500,
            deposit_outpoint=("1" * 64, 0),
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                UPDATE pool_deposits
                SET amount_sats = ?
                WHERE id = ?
                """,
                (20_000_000_000, deposit_id),
            )

        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "DELETE FROM pool_deposits WHERE id = ?",
                (deposit_id,),
            )

        balances = pool_ledger.session_balances(
            self.conn,
            pool_id="POOL-TEST",
            session_id="SESSION-001",
        )

        self.assertEqual(
            balances["deposit_sats"],
            17_500_000_000,
        )


    def test_registered_session_fee_terms_are_immutable(self):
        """A participant's registered fee cannot be changed."""

        conn = db.connect(":memory:")

        try:
            db.init(conn)

            pool_ledger.create_pool(
                conn,
                pool_id="fee-lock-pool",
                reserve_wallet="fee_lock_reserve",
                fee_wallet="fee_lock_fees",
            )

            pool_ledger.create_session(
                conn,
                pool_id="fee-lock-pool",
                session_id="fee-lock-session",
                fee_basis_points=500,
            )

            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    UPDATE pool_sessions
                    SET fee_basis_points = 1000
                    WHERE id = ?
                    """,
                    ("fee-lock-session",),
                )

            row = conn.execute(
                """
                SELECT fee_basis_points
                FROM pool_sessions
                WHERE id = ?
                """,
                ("fee-lock-session",),
            ).fetchone()

            self.assertEqual(row["fee_basis_points"], 500)

        finally:
            conn.close()


    def test_journal_cannot_be_inserted_already_posted(self):
        """Every posted journal must pass the balancing transition."""

        conn = db.connect(":memory:")

        try:
            db.init(conn)

            pool_ledger.create_pool(
                conn,
                pool_id="posting-guard-pool",
                reserve_wallet="posting_guard_reserve",
                fee_wallet="posting_guard_fees",
            )

            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO pool_journal_transactions(
                        pool_id, state, created_at
                    )
                    VALUES (?, 'POSTED', ?)
                    """,
                    ("posting-guard-pool", "2026-10-10T00:00:00+00:00"),
                )

            count = conn.execute(
                """
                SELECT COUNT(*) AS n
                FROM pool_journal_transactions
                WHERE pool_id = ?
                """,
                ("posting-guard-pool",),
            ).fetchone()["n"]

            self.assertEqual(count, 0)

        finally:
            conn.close()


    def test_deposit_rejects_amount_above_sqlite_integer_limit(self):
        """Amounts exceeding signed 64-bit storage must be refused."""

        conn = db.connect(":memory:")
        try:
            db.init(conn)
            pool_ledger.create_pool(
                conn,
                pool_id="overflow-pool",
                reserve_wallet="overflow_reserve",
                fee_wallet="overflow_fees",
            )
            pool_ledger.create_session(
                conn,
                pool_id="overflow-pool",
                session_id="overflow-session",
                fee_basis_points=500,
            )

            with self.assertRaises(ValueError):
                pool_ledger.record_deposit(
                    conn,
                    pool_id="overflow-pool",
                    session_id="overflow-session",
                    deposit_sats=2**63,
                    fee_basis_points=500,
                    deposit_outpoint=("a" * 64, 0),
                )
        finally:
            conn.close()

    def test_cumulative_session_deposit_cannot_overflow(self):
        """Multiple valid deposits must not overflow session totals."""

        maximum = 2**63 - 1

        conn = db.connect(":memory:")
        try:
            db.init(conn)
            pool_ledger.create_pool(
                conn,
                pool_id="cumulative-overflow-pool",
                reserve_wallet="cumulative_reserve",
                fee_wallet="cumulative_fees",
            )
            pool_ledger.create_session(
                conn,
                pool_id="cumulative-overflow-pool",
                session_id="cumulative-session",
                fee_basis_points=500,
            )

            pool_ledger.record_deposit(
                conn,
                pool_id="cumulative-overflow-pool",
                session_id="cumulative-session",
                deposit_sats=maximum,
                fee_basis_points=500,
                deposit_outpoint=("b" * 64, 0),
            )

            with self.assertRaises(ValueError):
                pool_ledger.record_deposit(
                    conn,
                    pool_id="cumulative-overflow-pool",
                    session_id="cumulative-session",
                    deposit_sats=1,
                    fee_basis_points=500,
                    deposit_outpoint=("c" * 64, 0),
                )

            balances = pool_ledger.session_balances(
                conn,
                pool_id="cumulative-overflow-pool",
                session_id="cumulative-session",
            )
            self.assertEqual(balances["deposit_sats"], maximum)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
