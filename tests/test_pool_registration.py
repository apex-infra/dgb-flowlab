"""Mixer Lab participant registration tests."""

import sqlite3
import unittest

from flowlab import db, pool_ledger, pool_registration


class FakeRPC:
    def __init__(self, invalid=None):
        self.invalid = invalid
        self.calls = []

    def validate_address(self, address):
        self.calls.append(address)
        return {"isvalid": address != self.invalid}


class RegistrationTests(unittest.TestCase):

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init(self.conn)

        pool_ledger.create_pool(
            self.conn,
            pool_id="test-pool",
            reserve_wallet="test-reserve",
            fee_wallet="test-fees",
        )

        self.rpc = FakeRPC()

    def tearDown(self):
        self.conn.close()

    def register(self, payouts, session_id="session-1", **kwargs):
        return pool_registration.register_participant(
            self.conn,
            self.rpc,
            pool_id="test-pool",
            session_id=session_id,
            payouts=payouts,
            **kwargs,
        )

    def test_valid_registration(self):
        result = self.register([
            {"address": "address-A", "allocation_bps": 6000},
            {"address": "address-B", "allocation_bps": 3000},
            {"address": "address-C", "allocation_bps": 1000},
        ])

        self.assertEqual(result["fee_basis_points"], 500)
        self.assertEqual(result["payout_count"], 3)

        rows = self.conn.execute(
            """
            SELECT payout_address, allocation_bps
            FROM pool_payout_instructions
            ORDER BY position
            """
        ).fetchall()

        self.assertEqual(
            [(r["payout_address"], r["allocation_bps"]) for r in rows],
            [
                ("address-A", 6000),
                ("address-B", 3000),
                ("address-C", 1000),
            ],
        )

        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM pool_registration_seals"
            ).fetchone()[0],
            1,
        )

    def test_one_address_at_100_percent(self):
        self.register([
            {"address": "address-A", "allocation_bps": 10000},
        ])
        self.assertEqual(len(self.rpc.calls), 1)

    def test_allocations_must_total_100_percent(self):
        with self.assertRaises(ValueError):
            self.register([
                {"address": "address-A", "allocation_bps": 6000},
                {"address": "address-B", "allocation_bps": 3000},
            ])

    def test_duplicate_addresses_rejected(self):
        with self.assertRaises(ValueError):
            self.register([
                {"address": "address-A", "allocation_bps": 5000},
                {"address": "address-A", "allocation_bps": 5000},
            ])

    def test_more_than_ten_addresses_rejected(self):
        with self.assertRaises(ValueError):
            self.register([
                {"address": f"address-{i}", "allocation_bps": 1000}
                for i in range(11)
            ])

    def test_invalid_address_leaves_no_registration(self):
        self.rpc.invalid = "address-B"

        with self.assertRaises(ValueError):
            self.register([
                {"address": "address-A", "allocation_bps": 5000},
                {"address": "address-B", "allocation_bps": 5000},
            ])

        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM pool_sessions"
            ).fetchone()[0],
            0,
        )

    def test_duplicate_session_does_not_overwrite(self):
        payouts = [
            {"address": "address-A", "allocation_bps": 10000},
        ]

        self.register(payouts)

        with self.assertRaises(sqlite3.IntegrityError):
            self.register(payouts)

        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM pool_registration_seals"
            ).fetchone()[0],
            1,
        )

    def test_payout_instructions_immutable(self):
        self.register([
            {"address": "address-A", "allocation_bps": 10000},
        ])

        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                UPDATE pool_payout_instructions
                SET allocation_bps = 5000
                """
            )

    def test_cannot_append_after_registration(self):
        self.register([
            {"address": "address-A", "allocation_bps": 10000},
        ])

        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO pool_payout_instructions(
                    session_id, position, payout_address, allocation_bps
                )
                VALUES ('session-1', 2, 'address-B', 1000)
                """
            )

    def test_no_expected_deposit_amount(self):
        result = self.register([
            {"address": "address-A", "allocation_bps": 10000},
        ])

        self.assertNotIn("deposit_sats", result)
        self.assertNotIn("expected_amount", result)


if __name__ == "__main__":
    unittest.main()
