import json, unittest
from decimal import Decimal
from flowlab.rpc import (RpcClient, RpcError, MethodNotAllowed, WalletNotAllowed,
                         to_sats, to_dgb)
from flowlab.node_verifier import NodeVerifier
from flowlab.verify import evaluate
from tests.fake_node import FakeNode


class RpcTests(unittest.TestCase):
    def setUp(self):
        self.n = FakeNode()
        self.n.handlers.update(
            getblockcount=lambda: 100, listwallets=lambda: list(self.n.wallets))
        self.c = RpcClient("flowlab", "s3cret-pw", self.n.port,
                           allowed_wallets=["flab_source", "flab_a"])

    def tearDown(self): self.n.close()

    def test_basic_call(self):
        self.assertEqual(self.c.get_block_count(), 100)

    def test_disallowed_method_never_sent(self):
        with self.assertRaises(MethodNotAllowed):
            self.c._call("sendtoaddress", ["x", 1])
        with self.assertRaises(MethodNotAllowed):
            self.c._call("dumpprivkey", ["x"])
        self.assertEqual(self.n.calls, [])

    def test_disallowed_wallet_never_sent(self):
        with self.assertRaises(WalletNotAllowed):
            self.c.get_balances("pool")
        with self.assertRaises(WalletNotAllowed):
            self.c.get_balances("")
        self.assertEqual(self.n.calls, [])

    def test_bad_allowlist_names_rejected(self):
        for bad in ("pool", "fees", "identity", "flab_", "flab_../x", "FLAB_a"):
            with self.assertRaises(WalletNotAllowed):
                RpcClient("u", "p", 1, allowed_wallets=[bad])

    def test_non_local_host_rejected(self):
        with self.assertRaises(RpcError):
            RpcClient("u", "p", 1, host="10.0.0.5")

    def test_wallet_url_used(self):
        self.n.handlers["getbalances"] = lambda: {"mine": {"trusted": 1}}
        self.c.get_balances("flab_a")
        self.assertEqual(self.n.calls[-1][0], "/wallet/flab_a")

    def test_credentials_not_in_errors_or_repr(self):
        try:
            self.c._call("getblockhash", [1])
        except RpcError as e:
            self.assertNotIn("s3cret", str(e))
        self.assertNotIn("s3cret", repr(self.c))
        n = FakeNode(); port = n.port; n.close()
        dead = RpcClient("flowlab", "s3cret-pw", port)
        try:
            dead.get_block_count()
        except RpcError as e:
            self.assertNotIn("s3cret", str(e))
        else:
            self.fail("expected error")

    def test_sats_conversion_exact(self):
        self.assertEqual(to_sats(Decimal("0.00000001")), 1)
        self.assertEqual(to_sats(Decimal("12.34567891")), 1234567891)
        self.assertEqual(format(to_dgb(1), "f"), "0.00000001")
        with self.assertRaises(RpcError):
            to_dgb(1.5)

    def test_raw_tx_amount_serialized_exactly(self):
        self.n.handlers["createrawtransaction"] = lambda i, o: "00"
        self.c.create_raw_transaction([], {"addr": 1})
        self.assertEqual(self.n.calls[-1][2][1], {"addr": Decimal("0.00000001")})


FLOW = {"source_wallet": "flab_source", "flow_wallets_json": json.dumps(["flab_a"]),
        "destination_wallet": "flab_source", "state": "START", "error_state": "NONE",
        "initial_alloc_sats": 1000}


class VerifierTests(unittest.TestCase):
    def setUp(self):
        self.n = FakeNode()
        h = self.n.handlers
        h["getblockchaininfo"] = lambda: {"blocks": 10, "headers": 10,
                                          "initialblockdownload": False}
        h["listwallets"] = lambda: ["flab_source", "flab_a"]
        h["getbalances"] = lambda: {"mine": {"trusted": Decimal("0.00001000")}}
        h["listunspent"] = lambda m: [{"amount": Decimal("0.00001")}]
        self.c = RpcClient("u", "p", self.n.port, allowed_wallets=["flab_source", "flab_a"])
        self.v = NodeVerifier(self.c)

    def tearDown(self): self.n.close()

    def test_all_clear(self):
        self.assertEqual(evaluate(self.v, {}, FLOW, "start"), [])

    def test_allowlisted_extra_wallet_loaded_is_allowed(self):
        self.c = RpcClient(
            "u", "p", self.n.port,
            allowed_wallets=["flab_source", "flab_a", "flab_b"],
        )
        self.v = NodeVerifier(self.c)
        self.n.handlers["listwallets"] = lambda: [
            "flab_source", "flab_a", "flab_b"
        ]
        self.assertEqual(evaluate(self.v, {}, FLOW, "start"), [])

    def test_non_allowlisted_extra_wallet_loaded_fails(self):
        self.n.handlers["listwallets"] = lambda: ["flab_source", "flab_a", "pool"]
        self.assertTrue(any("wallet" in f for f in evaluate(self.v, {}, FLOW, "start")))

    def test_missing_wallet_fails(self):
        self.n.handlers["listwallets"] = lambda: ["flab_source"]
        self.assertTrue(any(f.startswith("wallet") for f in evaluate(self.v, {}, FLOW, "x")))

    def test_low_balance_fails(self):
        self.n.handlers["getbalances"] = lambda: {"mine": {"trusted": Decimal("0.000001")}}
        self.assertTrue(any(f.startswith("balance") for f in evaluate(self.v, {}, FLOW, "x")))

    def test_ibd_fails(self):
        self.n.handlers["getblockchaininfo"] = lambda: {"blocks": 1, "headers": 10,
                                                        "initialblockdownload": True}
        self.assertTrue(any(f.startswith("height") for f in evaluate(self.v, {}, FLOW, "x")))

    def test_node_down_fails_closed(self):
        self.n.close()
        self.assertGreaterEqual(len(evaluate(self.v, {}, FLOW, "x")), 4)

    def test_unknown_txid_fails(self):
        v = NodeVerifier(self.c, known_txids=lambda f: ["ab" * 32])
        self.assertTrue(any(f.startswith("transaction") for f in evaluate(v, {}, FLOW, "x")))

    def test_flow_error_state_fails(self):
        f = dict(FLOW, error_state="ERROR")
        self.assertTrue(any(x.startswith("flow_state") for x in evaluate(self.v, {}, f, "x")))


if __name__ == "__main__":
    unittest.main()
