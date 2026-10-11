"""Mixer Lab provisional deposit discovery tests."""

import unittest

from flowlab import (
    db,
    pool_addresses,
    pool_ledger,
    pool_observer,
    pool_registration,
)


class FakeRPC:
    def __init__(self):
        self.entries = []
        self.calls = []

    def validate_address(self, address):
        return {"isvalid": True}

    def get_new_address(self, wallet, label=""):
        return "deposit-address"

    def get_address_info(self, wallet, address):
        return {"ismine": True}

    def list_transactions(
        self, wallet, label="*", count=100, skip=0,
        include_watchonly=False,
    ):
        self.calls.append((wallet, label, count, skip))
        return self.entries[skip:skip + count]


class ObserverTests(unittest.TestCase):

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init(self.conn)
        self.rpc = FakeRPC()

        pool_ledger.create_pool(
            self.conn,
            pool_id="observer-pool",
            reserve_wallet="observer-reserve",
            fee_wallet="observer-fees",
        )

        pool_registration.register_participant(
            self.conn,
            self.rpc,
            pool_id="observer-pool",
            session_id="observer-session",
            payouts=[
                {"address": "payout-address", "allocation_bps": 10000}
            ],
        )

        pool_addresses.request_deposit_address(
            self.conn,
            self.rpc,
            pool_id="observer-pool",
            session_id="observer-session",
        )

    def tearDown(self):
        self.conn.close()

    def entry(self, txid="a" * 64, vout=0, amount="175.0", confirmations=0):
        return {
            "category": "receive",
            "address": "deposit-address",
            "txid": txid,
            "vout": vout,
            "amount": amount,
            "confirmations": confirmations,
        }

    def scan(self, page_size=100):
        return pool_observer.observe_session(
            self.conn,
            self.rpc,
            pool_id="observer-pool",
            session_id="observer-session",
            page_size=page_size,
        )

    def test_unconfirmed_deposit_is_observed_not_credited(self):
        self.rpc.entries = [self.entry()]

        result = self.scan()

        self.assertEqual(result["inserted"], 1)

        row = self.conn.execute(
            "SELECT state, amount_sats FROM pool_deposit_observations"
        ).fetchone()

        self.assertEqual(row["state"], "UNCONFIRMED")
        self.assertEqual(row["amount_sats"], 17_500_000_000)

        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM pool_deposits"
            ).fetchone()[0],
            0,
        )

    def test_confirmation_updates_append_history(self):
        self.rpc.entries = [self.entry(confirmations=0)]
        self.scan()

        self.rpc.entries = [self.entry(confirmations=3)]
        result = self.scan()

        self.assertEqual(result["updated"], 1)

        history = list(self.conn.execute(
            "SELECT state FROM pool_observation_history ORDER BY id"
        ))

        self.assertEqual(
            [row["state"] for row in history],
            ["UNCONFIRMED", "CONFIRMED"],
        )

    def test_same_scan_is_idempotent(self):
        self.rpc.entries = [self.entry()]
        self.scan()
        result = self.scan()

        self.assertEqual(result["inserted"], 0)
        self.assertEqual(result["updated"], 0)

        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM pool_observation_history"
            ).fetchone()[0],
            1,
        )

    def test_multiple_deposits_and_pagination(self):
        self.rpc.entries = [
            self.entry(txid="a" * 64),
            self.entry(txid="b" * 64),
            self.entry(txid="c" * 64),
        ]

        result = self.scan(page_size=2)

        self.assertEqual(result["inserted"], 3)
        self.assertGreaterEqual(len(self.rpc.calls), 2)

    def test_wrong_address_is_ignored(self):
        entry = self.entry()
        entry["address"] = "unrelated-address"
        self.rpc.entries = [entry]

        self.assertEqual(self.scan()["inserted"], 0)

    def test_conflicting_amount_is_rejected(self):
        self.rpc.entries = [self.entry()]
        self.scan()

        self.rpc.entries = [self.entry(amount="200.0")]

        with self.assertRaises(ValueError):
            self.scan()

    def test_conflicted_transaction_is_recorded(self):
        self.rpc.entries = [self.entry(confirmations=-2)]
        self.scan()

        state = self.conn.execute(
            "SELECT state FROM pool_deposit_observations"
        ).fetchone()[0]

        self.assertEqual(state, "CONFLICTED")


    def test_transaction_arrives_during_pagination(self):
        a = self.entry(txid="a" * 64)
        b = self.entry(txid="b" * 64)
        c = self.entry(txid="c" * 64)
        x = self.entry(txid="d" * 64)

        class ShiftingRPC:
            def __init__(self):
                self.calls = 0

            def list_transactions(
                self, wallet, label="*", count=100, skip=0,
                include_watchonly=False,
            ):
                self.calls += 1

                # A new transaction arrives after the first page.
                if self.calls == 1:
                    history = [a, b]
                else:
                    history = [x, a, b, c]

                return history[skip:skip + count]

        self.rpc = ShiftingRPC()

        result = self.scan(page_size=2)

        self.assertEqual(result["inserted"], 4)

    def test_unstable_history_does_not_persist_partial_scan(self):
        a = self.entry(txid="a" * 64)
        b = self.entry(txid="b" * 64)

        class UnstableRPC:
            def __init__(self):
                self.calls = 0

            def list_transactions(
                self, wallet, label="*", count=100, skip=0,
                include_watchonly=False,
            ):
                self.calls += 1

                # Every scan starts with a different amount.
                if skip == 0:
                    self.amount = str(self.calls) + ".0"

                changed = dict(a)
                changed["amount"] = self.amount

                history = [changed, b]
                return history[skip:skip + count]

        self.rpc = UnstableRPC()

        with self.assertRaises(ValueError):
            self.scan(page_size=2)

        count = self.conn.execute(
            "SELECT COUNT(*) FROM pool_deposit_observations"
        ).fetchone()[0]

        self.assertEqual(count, 0)


    def test_database_rejects_wrong_session_deposit_address(self):
        import sqlite3

        # Even direct SQL writes must respect the registered
        # session's verified receiving address.
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO pool_deposit_observations(
                    pool_id, session_id, txid, vout,
                    amount_sats, address, confirmations,
                    blockhash, state, first_seen_at, last_seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "observer-pool",
                    "observer-session",
                    "f" * 64,
                    0,
                    100_000_000,
                    "wrong-deposit-address",
                    0,
                    None,
                    "UNCONFIRMED",
                    "t",
                    "t",
                ),
            )


    def test_rejects_fractional_satoshi(self):
        for amount in ("1.000000005", "0.000000001"):
            with self.subTest(amount=amount):
                self.rpc.entries = [self.entry(amount=amount)]

                with self.assertRaises(ValueError):
                    self.scan()

                self.assertEqual(
                    self.conn.execute(
                        "SELECT COUNT(*) FROM pool_deposit_observations"
                    ).fetchone()[0],
                    0,
                )

    def test_rejects_invalid_numeric_amounts(self):
        for amount in ("NaN", "Infinity", "not-a-number", True):
            with self.subTest(amount=amount):
                self.rpc.entries = [self.entry(amount=amount)]

                with self.assertRaises(ValueError):
                    self.scan()

                self.assertEqual(
                    self.conn.execute(
                        "SELECT COUNT(*) FROM pool_deposit_observations"
                    ).fetchone()[0],
                    0,
                )

    def test_rejects_amount_over_sqlite_limit(self):
        self.rpc.entries = [
            self.entry(amount="92233720368.54775808")
        ]

        with self.assertRaises(ValueError):
            self.scan()

        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM pool_deposit_observations"
            ).fetchone()[0],
            0,
        )

    def test_rejects_missing_amount(self):
        entry = self.entry()
        del entry["amount"]
        self.rpc.entries = [entry]

        with self.assertRaises(ValueError):
            self.scan()

    def test_invalid_amount_prevents_partial_persistence(self):
        self.rpc.entries = [
            self.entry(txid="a" * 64, amount="2.0"),
            self.entry(txid="b" * 64, amount="1.000000005"),
        ]

        with self.assertRaises(ValueError):
            self.scan()

        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM pool_deposit_observations"
            ).fetchone()[0],
            0,
        )


    def test_exact_satoshi_boundaries(self):
        from flowlab.pool_observer import _exact_deposit_sats

        cases = (
            ("0.00000001", 1),
            ("1.00000000", 100_000_000),
            ("1.12345678", 112_345_678),
            ("92233720368.54775807", 9223372036854775807),
        )

        for amount, expected in cases:
            with self.subTest(amount=amount):
                self.assertEqual(
                    _exact_deposit_sats(amount),
                    expected,
                )

    def test_rejects_above_maximum_satoshi(self):
        from flowlab.pool_observer import _exact_deposit_sats

        with self.assertRaises(ValueError):
            _exact_deposit_sats("92233720368.54775808")

    def test_rejects_more_than_eight_decimal_places(self):
        from flowlab.pool_observer import _exact_deposit_sats

        for amount in ("0.000000001", "1.123456789"):
            with self.subTest(amount=amount):
                with self.assertRaises(ValueError):
                    _exact_deposit_sats(amount)


if __name__ == "__main__":
    unittest.main()
