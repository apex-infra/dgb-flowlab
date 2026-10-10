"""Mixer Lab deposit-address lifecycle tests."""

import sqlite3
import unittest

from flowlab import (
    db,
    pool_addresses,
    pool_ledger,
    pool_registration,
)


class FakeRPC:
    def __init__(self):
        self.generation_calls = []
        self.ownership_calls = []
        self.fail_generation = False
        self.owns_address = True

    def validate_address(self, address):
        return {"isvalid": True}

    def get_new_address(self, wallet, label=""):
        self.generation_calls.append((wallet, label))
        if self.fail_generation:
            raise RuntimeError("Simulated RPC interruption")
        return "generated-test-deposit-address"

    def get_address_info(self, wallet, address):
        self.ownership_calls.append((wallet, address))
        return {"ismine": self.owns_address}


class AddressLifecycleTests(unittest.TestCase):

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init(self.conn)
        self.rpc = FakeRPC()

        pool_ledger.create_pool(
            self.conn,
            pool_id="test-pool",
            reserve_wallet="test-reserve",
            fee_wallet="test-fees",
        )

        pool_registration.register_participant(
            self.conn,
            self.rpc,
            pool_id="test-pool",
            session_id="session-1",
            payouts=[
                {"address": "payout-A", "allocation_bps": 10000}
            ],
        )

    def tearDown(self):
        self.conn.close()

    def request(self, session_id="session-1"):
        return pool_addresses.request_deposit_address(
            self.conn,
            self.rpc,
            pool_id="test-pool",
            session_id=session_id,
        )

    def test_verified_address_becomes_ready(self):
        result = self.request()

        self.assertEqual(result["state"], "READY")
        self.assertEqual(
            result["address"],
            "generated-test-deposit-address",
        )
        self.assertEqual(result["wallet"], "test-reserve")
        self.assertEqual(len(self.rpc.generation_calls), 1)
        self.assertEqual(len(self.rpc.ownership_calls), 1)

    def test_ready_address_is_reused_without_new_rpc(self):
        first = self.request()
        second = self.request()

        self.assertEqual(first["address"], second["address"])
        self.assertEqual(len(self.rpc.generation_calls), 1)

    def test_rpc_failure_requires_recovery_and_no_retry(self):
        self.rpc.fail_generation = True

        with self.assertRaises(pool_addresses.AddressRecoveryRequired):
            self.request()

        status = pool_addresses.address_status(
            self.conn,
            session_id="session-1",
        )

        self.assertEqual(status["state"], "RECOVERY_REQUIRED")

        with self.assertRaises(pool_addresses.AddressRecoveryRequired):
            self.request()

        self.assertEqual(len(self.rpc.generation_calls), 1)

    def test_unverified_address_is_never_returned(self):
        self.rpc.owns_address = False

        with self.assertRaises(pool_addresses.AddressRecoveryRequired):
            self.request()

        status = pool_addresses.address_status(
            self.conn,
            session_id="session-1",
        )

        self.assertIsNone(status["address"])
        self.assertEqual(status["state"], "RECOVERY_REQUIRED")

    def test_interrupted_request_is_not_repeated(self):
        self.conn.execute(
            """
            INSERT INTO pool_deposit_addresses(
                session_id, wallet, request_label,
                state, created_at, updated_at
            )
            VALUES (?, ?, ?, 'REQUESTING', ?, ?)
            """,
            (
                "session-1",
                "test-reserve",
                "interrupted-request",
                "2026-10-10T00:00:00Z",
                "2026-10-10T00:00:00Z",
            ),
        )

        with self.assertRaises(pool_addresses.AddressRecoveryRequired):
            self.request()

        self.assertEqual(len(self.rpc.generation_calls), 0)

    def test_unregistered_session_cannot_get_address(self):
        with self.assertRaises(ValueError):
            self.request("unknown-session")

        self.assertEqual(len(self.rpc.generation_calls), 0)

    def test_ready_address_is_immutable(self):
        self.request()

        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                UPDATE pool_deposit_addresses
                SET address = 'different-address'
                WHERE session_id = 'session-1'
                """
            )


if __name__ == "__main__":
    unittest.main()
