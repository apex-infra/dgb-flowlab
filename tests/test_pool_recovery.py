"""Mixer Lab address recovery tests."""

import unittest

from flowlab import (
    db,
    pool_addresses,
    pool_ledger,
    pool_registration,
)


class FakeRecoveryRPC:
    def __init__(self):
        self.generated = 0
        self.candidates = {}
        self.owns = True

    def validate_address(self, address):
        return {"isvalid": True}

    def get_new_address(self, wallet, label=""):
        self.generated += 1
        raise RuntimeError("Simulated lost RPC response")

    def get_addresses_by_label(self, wallet, label):
        return self.candidates

    def get_address_info(self, wallet, address):
        return {"ismine": self.owns}


class RecoveryTests(unittest.TestCase):

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init(self.conn)
        self.rpc = FakeRecoveryRPC()

        pool_ledger.create_pool(
            self.conn,
            pool_id="recovery-pool",
            reserve_wallet="recovery-reserve",
            fee_wallet="recovery-fees",
        )

        pool_registration.register_participant(
            self.conn,
            self.rpc,
            pool_id="recovery-pool",
            session_id="recovery-session",
            payouts=[
                {"address": "payout-A", "allocation_bps": 10000}
            ],
        )

        with self.assertRaises(pool_addresses.AddressRecoveryRequired):
            pool_addresses.request_deposit_address(
                self.conn,
                self.rpc,
                pool_id="recovery-pool",
                session_id="recovery-session",
            )

    def tearDown(self):
        self.conn.close()

    def recover(self):
        return pool_addresses.recover_deposit_address(
            self.conn,
            self.rpc,
            pool_id="recovery-pool",
            session_id="recovery-session",
        )

    def test_one_verified_receiving_address_recovers(self):
        self.rpc.candidates = {
            "recovered-address": {"purpose": "receive"}
        }

        result = self.recover()

        self.assertEqual(result["state"], "READY")
        self.assertEqual(result["address"], "recovered-address")
        self.assertEqual(self.rpc.generated, 1)

    def test_no_address_requires_operator_recovery(self):
        with self.assertRaises(pool_addresses.AddressRecoveryRequired):
            self.recover()

        status = pool_addresses.address_status(
            self.conn,
            session_id="recovery-session",
        )

        self.assertEqual(status["state"], "RECOVERY_REQUIRED")

    def test_multiple_addresses_are_rejected(self):
        self.rpc.candidates = {
            "address-A": {"purpose": "receive"},
            "address-B": {"purpose": "receive"},
        }

        with self.assertRaises(pool_addresses.AddressRecoveryRequired):
            self.recover()

    def test_unowned_address_is_rejected(self):
        self.rpc.candidates = {
            "foreign-address": {"purpose": "receive"}
        }
        self.rpc.owns = False

        with self.assertRaises(pool_addresses.AddressRecoveryRequired):
            self.recover()

    def test_recovery_never_generates_second_address(self):
        self.rpc.candidates = {
            "recovered-address": {"purpose": "receive"}
        }

        self.recover()
        self.recover()

        self.assertEqual(self.rpc.generated, 1)


if __name__ == "__main__":
    unittest.main()
