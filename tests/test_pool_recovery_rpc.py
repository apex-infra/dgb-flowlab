"""Restricted Mixer Lab recovery RPC tests."""

import unittest

from flowlab.pool_recovery_rpc import RecoveryRpc


class FakeRpc:
    def list_transactions(self, wallet, **kwargs):
        return [wallet, kwargs]

    def get_block_hash(self, height):
        return f"hash-{height}"

    def get_block_header(self, blockhash):
        return {"hash": blockhash}

    def get_raw_transaction(self, txid, verbose=True, blockhash=None):
        return {
            "txid": txid,
            "verbose": verbose,
            "blockhash": blockhash,
        }

    def send_raw_transaction(self, tx):
        raise AssertionError("Financial RPC must not be available")

    def sign_raw_transaction_with_wallet(self, *args):
        raise AssertionError("Signing must not be available")


class RecoveryRpcTests(unittest.TestCase):

    def setUp(self):
        self.rpc = RecoveryRpc(FakeRpc())

    def test_observation_methods(self):
        self.assertEqual(
            self.rpc.list_transactions("flab_pool_reserve", count=10),
            ["flab_pool_reserve", {"count": 10}],
        )

        self.assertEqual(
            self.rpc.get_block_hash(100),
            "hash-100",
        )

        self.assertEqual(
            self.rpc.get_block_header("abc"),
            {"hash": "abc"},
        )

        self.assertEqual(
            self.rpc.get_raw_transaction("tx", blockhash="block"),
            {
                "txid": "tx",
                "verbose": True,
                "blockhash": "block",
            },
        )

    def test_financial_methods_not_exposed(self):
        for name in (
            "send_raw_transaction",
            "sign_raw_transaction_with_wallet",
            "create_raw_transaction",
            "fund_raw_transaction",
            "get_new_address",
        ):
            self.assertFalse(
                hasattr(self.rpc, name),
                f"Unexpected recovery method: {name}",
            )

    def test_rejects_incomplete_client(self):
        with self.assertRaises(TypeError):
            RecoveryRpc(object())



    def test_factory_rejects_unbounded_timeout(self):
        from flowlab.pool_recovery_rpc import create_recovery_rpc

        for timeout in (0, 31, -1, True, 3.5):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    create_recovery_rpc(
                        user="test",
                        password="test",
                        allowed_wallets=["flab_pool_reserve"],
                        timeout=timeout,
                    )

    def test_factory_requires_wallet_allowlist(self):
        from flowlab.pool_recovery_rpc import create_recovery_rpc

        with self.assertRaises(ValueError):
            create_recovery_rpc(
                user="test",
                password="test",
            )

    def test_factory_creates_restricted_client(self):
        from flowlab.pool_recovery_rpc import (
            RecoveryRpc,
            create_recovery_rpc,
        )

        rpc = create_recovery_rpc(
            user="test",
            password="test",
            allowed_wallets=["flab_pool_reserve"],
            timeout=15,
        )

        self.assertIsInstance(rpc, RecoveryRpc)
        self.assertFalse(hasattr(rpc, "send_raw_transaction"))
if __name__ == "__main__":
    unittest.main()
