import json, unittest
from decimal import Decimal
from flowlab.rpc import (RpcClient, RpcError, MethodNotAllowed, WalletNotAllowed,
                         to_sats, to_dgb)
from flowlab.node_verifier import NodeVerifier
from flowlab.plays import compile_play
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

    def test_fund_raw_transaction_accepts_explicit_fee_output_indexes(self):
        seen = {}

        def handler(hexstr, options):
            seen.update(options)
            return {"hex": "funded", "fee": Decimal("0.001")}

        self.n.handlers["fundrawtransaction"] = handler

        result = self.c.fund_raw_transaction(
            "flab_source",
            "raw",
            "dgb1qchange",
            subtract_fee_indexes=[0, 2],
        )

        self.assertEqual(result["hex"], "funded")
        self.assertEqual(seen["subtractFeeFromOutputs"], [0, 2])
        self.assertFalse(seen["add_inputs"])
        self.assertEqual(seen["changeAddress"], "dgb1qchange")

    def test_fund_raw_transaction_refuses_two_fee_subtraction_modes(self):
        with self.assertRaises(RpcError):
            self.c.fund_raw_transaction(
                "flab_source",
                "raw",
                "dgb1qchange",
                subtract_fee=True,
                subtract_fee_indexes=[0],
            )

    def test_get_new_address_can_select_address_type(self):
        seen = {}

        def handler(label, address_type):
            seen["label"] = label
            seen["address_type"] = address_type
            return "dgb1qtype"

        self.n.handlers["getnewaddress"] = handler

        result = self.c.get_new_address(
            "flab_a",
            "deposit one",
            "bech32",
        )

        self.assertEqual(result, "dgb1qtype")
        self.assertEqual(
            seen,
            {
                "label": "deposit one",
                "address_type": "bech32",
            },
        )
        self.assertEqual(self.n.calls[-1][0], "/wallet/flab_a")
        self.assertEqual(self.n.calls[-1][1], "getnewaddress")
        self.assertEqual(
            self.n.calls[-1][2],
            ["deposit one", "bech32"],
        )

    def test_set_label_uses_wallet_rpc(self):
        seen = {}

        def handler(address, label):
            seen["address"] = address
            seen["label"] = label
            return None

        self.n.handlers["setlabel"] = handler

        result = self.c.set_label(
            "flab_a",
            "dgb1qabc",
            "reserve one",
        )

        self.assertIsNone(result)
        self.assertEqual(
            seen,
            {
                "address": "dgb1qabc",
                "label": "reserve one",
            },
        )
        self.assertEqual(
            self.n.calls[-1][0],
            "/wallet/flab_a",
        )
        self.assertEqual(
            self.n.calls[-1][1],
            "setlabel",
        )
        self.assertEqual(
            self.n.calls[-1][2],
            ["dgb1qabc", "reserve one"],
        )

    def test_validate_address_uses_non_wallet_rpc(self):
        self.n.handlers["validateaddress"] = lambda a: {
            "isvalid": a == "dgb1qexternal",
        }

        self.assertEqual(
            self.c.validate_address("dgb1qexternal"),
            {"isvalid": True},
        )
        self.assertEqual(self.n.calls[-1][0], "/")
        self.assertEqual(self.n.calls[-1][1], "validateaddress")

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

    def test_settlement_cycle_wallet_selection_uses_real_wallets_not_identity_marker(self):
        cfg = compile_play(
            "settlement_cycle",
            {
                "source_wallet": "flab_source",
                "allocation_wallet": "flab_stage",
                "workers": ["flab_a", "flab_b"],
                "hubs": ["flab_hub_a"],
                "allocation_sats": 500_000_000,
                "outbound_decisions": 10,
                "return_decisions": 6,
                "amount_sats_min": 10_000_000,
                "amount_sats_max": 50_000_000,
                "delay_seconds_min": 5,
                "delay_seconds_max": 60,
                "settlement_delay_seconds_min": 30,
                "settlement_delay_seconds_max": 180,
                "reserve_return_delay_seconds_min": 0,
                "reserve_return_delay_seconds_max": 60,
                "confirmations_required": 2,
                "seed": 401,
                "max_total_transactions": 100,
                "settlement": {
                    "mode": "fixed",
                    "items": [{
                        "type": "address",
                        "address": "DExternalSettlement",
                        "amount_sats": 50_000_000,
                    }],
                },
            },
        )

        approved_flow = cfg["flows"][0]

        experiment = {
            "config_json": json.dumps(cfg),
        }

        flow = {
            "source_wallet": approved_flow["source_wallet"],
            "flow_wallets_json": json.dumps(
                approved_flow["flow_wallets"]
            ),
            "destination_wallet":
                approved_flow["flow_identity"],
            "state": "START",
            "error_state": "NONE",
            "initial_alloc_sats":
                approved_flow["allocation_sats"],
        }

        wallets = self.v._wallets(experiment, flow)

        self.assertEqual(
            wallets,
            [
                "flab_source",
                "flab_stage",
                "flab_hub_a",
                "flab_a",
                "flab_b",
            ],
        )

        self.assertNotIn(
            approved_flow["flow_identity"],
            wallets,
        )

    def test_settlement_cycle_includes_internal_payout_wallet(self):
        cfg = compile_play(
            "settlement_cycle",
            {
                "source_wallet": "flab_source",
                "allocation_wallet": "flab_stage",
                "workers": ["flab_a", "flab_b"],
                "hubs": [],
                "allocation_sats": 500_000_000,
                "outbound_decisions": 4,
                "return_decisions": 3,
                "amount_sats_min": 10_000_000,
                "amount_sats_max": 50_000_000,
                "delay_seconds_min": 0,
                "delay_seconds_max": 5,
                "settlement_delay_seconds_min": 0,
                "settlement_delay_seconds_max": 5,
                "reserve_return_delay_seconds_min": 0,
                "reserve_return_delay_seconds_max": 5,
                "confirmations_required": 2,
                "seed": 402,
                "max_total_transactions": 50,
                "settlement": {
                    "mode": "fixed",
                    "items": [
                        {
                            "type": "wallet",
                            "wallet": "flab_dest",
                            "amount_sats": 25_000_000,
                        },
                        {
                            "type": "address",
                            "address": "DExternalSettlement",
                            "amount_sats": 10_000_000,
                        },
                    ],
                },
            },
        )

        approved_flow = cfg["flows"][0]

        experiment = {
            "config_json": json.dumps(cfg),
        }

        flow = {
            "source_wallet": approved_flow["source_wallet"],
            "flow_wallets_json": json.dumps(
                approved_flow["flow_wallets"]
            ),
            "destination_wallet":
                approved_flow["flow_identity"],
            "state": "START",
            "error_state": "NONE",
            "initial_alloc_sats":
                approved_flow["allocation_sats"],
        }

        wallets = self.v._wallets(
            experiment,
            flow,
        )

        self.assertEqual(
            wallets,
            [
                "flab_source",
                "flab_stage",
                "flab_a",
                "flab_b",
                "flab_dest",
            ],
        )

        self.assertNotIn(
            approved_flow["flow_identity"],
            wallets,
        )

    def test_settlement_cycle_missing_internal_payout_wallet_fails(self):
        cfg = compile_play(
            "settlement_cycle",
            {
                "source_wallet": "flab_source",
                "allocation_wallet": "flab_stage",
                "workers": ["flab_a", "flab_b"],
                "hubs": [],
                "allocation_sats": 500_000_000,
                "outbound_decisions": 4,
                "return_decisions": 3,
                "amount_sats_min": 10_000_000,
                "amount_sats_max": 50_000_000,
                "delay_seconds_min": 0,
                "delay_seconds_max": 5,
                "settlement_delay_seconds_min": 0,
                "settlement_delay_seconds_max": 5,
                "reserve_return_delay_seconds_min": 0,
                "reserve_return_delay_seconds_max": 5,
                "confirmations_required": 2,
                "seed": 403,
                "max_total_transactions": 50,
                "settlement": {
                    "mode": "fixed",
                    "items": [{
                        "type": "wallet",
                        "wallet": "flab_dest",
                        "amount_sats": 25_000_000,
                    }],
                },
            },
        )

        approved_flow = cfg["flows"][0]

        experiment = {
            "config_json": json.dumps(cfg),
        }

        flow = {
            "source_wallet": approved_flow["source_wallet"],
            "flow_wallets_json": json.dumps(
                approved_flow["flow_wallets"]
            ),
            "destination_wallet":
                approved_flow["flow_identity"],
            "state": "START",
            "error_state": "NONE",
            "initial_alloc_sats":
                approved_flow["allocation_sats"],
        }

        self.n.handlers["listwallets"] = lambda: [
            "flab_source",
            "flab_stage",
            "flab_a",
            "flab_b",
        ]

        self.c = RpcClient(
            "u",
            "p",
            self.n.port,
            allowed_wallets=[
                "flab_source",
                "flab_stage",
                "flab_a",
                "flab_b",
                "flab_dest",
            ],
        )
        self.v = NodeVerifier(self.c)

        ok, detail = self.v._wallet(
            experiment,
            flow,
        )

        self.assertFalse(ok)
        self.assertIn(
            "flab_dest",
            detail,
        )


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
