import unittest
from decimal import Decimal

from flowlab.rpc import RpcClient, RpcError
from flowlab.tx_builder import BuildError, TxBuilder, broadcast
from tests import test_engine
from tests.fake_node import FakeNode
from tests.fake_chain import FakeChain, FEE

WALLETS = ["flab_source", "flab_a", "flab_b", "flab_dest"]
DEST = "dgb1qdest"
EXTERNAL = "dgb1qexternal"
CHANGE = "dgb1qchange"
TXID = "ab" * 32


class NodeCase:
    def make_node(self):
        self.n = FakeNode()
        self.sent = []
        h = self.n.handlers
        h["getnetworkinfo"] = lambda: {"relayfee": Decimal("0.001")}
        h["estimatesmartfee"] = lambda blocks: {
            "feerate": Decimal("0.00328494"),
            "blocks": blocks,
        }
        h["getaddressinfo"] = lambda a: {"ismine": a in (DEST, CHANGE)}
        h["validateaddress"] = lambda a: {"isvalid": a == EXTERNAL}
        h["getnewaddress"] = lambda label: CHANGE
        h["listunspent"] = lambda m: [
            {"txid": "11" * 32, "vout": 0, "amount": Decimal("5.0"), "spendable": True, "safe": True},
            {"txid": "22" * 32, "vout": 1, "amount": Decimal("0.5"), "spendable": True, "safe": True}]
        h["createrawtransaction"] = lambda i, o: "aa"
        self.fund_opts = []
        h["fundrawtransaction"] = lambda hx, o: (
            self.fund_opts.append(o),
            {"hex": "bb", "fee": Decimal("0.001")},
        )[1]
        h["signrawtransactionwithwallet"] = lambda hx: {"hex": "cc", "complete": True}
        self.vout = [{"value": Decimal("1.0"), "scriptPubKey": {"address": DEST}},
                     {"value": Decimal("3.999"), "scriptPubKey": {"address": CHANGE}}]
        self.vin = [{"txid": "11" * 32, "vout": 0}]
        h["decoderawtransaction"] = lambda hx: {"txid": TXID, "vin": self.vin, "vout": self.vout}
        h["testmempoolaccept"] = lambda l: [{"allowed": True}]
        h["sendrawtransaction"] = lambda hx: (self.sent.append(hx), TXID)[1]
        self.rpc = RpcClient("u", "p", self.n.port, allowed_wallets=WALLETS)
        self.b = TxBuilder(self.rpc, WALLETS, max_fee_sats=1_000_000)

    def names(self):
        return [c[1] for c in self.n.calls]


class BuildTests(NodeCase, unittest.TestCase):
    def setUp(self): self.make_node()

    def tearDown(self): self.n.close()

    def test_happy_path(self):
        p = self.b.build("flab_source", DEST, 100_000_000)
        self.assertEqual((p.txid, p.fee_sats, p.amount_sats), (TXID, 100_000, 100_000_000))
        self.assertIn("to address", p.summary())
        self.assertEqual(self.sent, [])  # build never broadcasts

    def test_planning_fee_reserve_uses_smart_fee_estimate(self):
        reserve = self.b.planning_fee_reserve_sats()

        self.assertEqual(reserve, 204_324)
        self.assertLess(reserve, self.b.max_fee_sats)


    def test_planning_fee_reserve_rejects_bad_input_counts(self):
        for n in (0, -1, 1.5, True):
            with self.assertRaises(BuildError):
                self.b.planning_fee_reserve_sats(n)

    def test_destination_must_be_ours(self):
        with self.assertRaises(BuildError):
            self.b.build("flab_source", "dgb1qstranger", 100_000_000)
        self.assertNotIn("createrawtransaction", self.names())

    def test_external_destination_requires_explicit_authorization(self):
        with self.assertRaises(BuildError):
            self.b.build(
                "flab_source",
                EXTERNAL,
                100_000_000,
            )
        self.assertNotIn("createrawtransaction", self.names())

    def test_explicitly_authorized_valid_external_destination_can_build(self):
        self.vout[0]["scriptPubKey"]["address"] = EXTERNAL

        p = self.b.build(
            "flab_source",
            EXTERNAL,
            100_000_000,
            allow_external=True,
        )

        self.assertEqual(p.address, EXTERNAL)
        self.assertIn("validateaddress", self.names())
        self.assertEqual(self.sent, [])

    def test_invalid_external_destination_is_refused_before_build(self):
        with self.assertRaisesRegex(
            BuildError,
            "not a valid DigiByte address",
        ):
            self.b.build(
                "flab_source",
                "not-valid",
                100_000_000,
                allow_external=True,
            )

        self.assertNotIn("createrawtransaction", self.names())

    def test_source_must_be_experiment_wallet(self):
        with self.assertRaises(BuildError):
            self.b.build("pool", DEST, 1)

    def test_bad_amounts(self):
        for amt in (0, -5, 1.5, True):
            with self.assertRaises(BuildError):
                self.b.build("flab_source", DEST, amt)

    def test_fee_over_cap(self):
        self.vout[1]["value"] = Decimal("3.5")
        with self.assertRaises(BuildError):
            self.b.build("flab_source", DEST, 100_000_000)

    def test_unexpected_output(self):
        self.vout[1]["scriptPubKey"]["address"] = "dgb1qthief"
        with self.assertRaises(BuildError):
            self.b.build("flab_source", DEST, 100_000_000)

    def test_wrong_amount_to_destination(self):
        self.vout[0]["value"] = Decimal("1.1")
        with self.assertRaises(BuildError):
            self.b.build("flab_source", DEST, 100_000_000)

    def test_unexpected_input(self):
        self.vin = [{"txid": "33" * 32, "vout": 0}]
        with self.assertRaises(BuildError):
            self.b.build("flab_source", DEST, 100_000_000)

    def test_insufficient_funds(self):
        with self.assertRaises(BuildError):
            self.b.build("flab_source", DEST, 10_000_000_000)

    def test_incomplete_signature(self):
        self.n.handlers["signrawtransactionwithwallet"] = lambda hx: {"hex": "cc", "complete": False}
        with self.assertRaises(BuildError):
            self.b.build("flab_source", DEST, 100_000_000)

    def test_mempool_rejection(self):
        self.n.handlers["testmempoolaccept"] = lambda l: [{"allowed": False, "reject-reason": "bad"}]
        with self.assertRaises(BuildError):
            self.b.build("flab_source", DEST, 100_000_000)


class DistributionBuildTests(unittest.TestCase):
    def setUp(self):
        self.chain = FakeChain(WALLETS)
        self.chain.fund("flab_source", 1_000_000_000)
        self.chain.mine(3)
        self.b = TxBuilder(
            self.chain,
            WALLETS,
            max_fee_sats=10_000_000,
        )
        self.internal = self.chain.get_new_address(
            "flab_dest",
            "distribution-test",
        )
        self.external_a = "dgb1qexternalone"
        self.external_b = "dgb1qexternaltwo"

    def test_percentage_distribution_splits_net_budget_exactly(self):
        prepared = self.b.build_distribution(
            "flab_source",
            "all",
            "percentage",
            [
                {
                    "address": self.internal,
                    "percent_bps": 5000,
                },
                {
                    "address": self.external_a,
                    "percent_bps": 5000,
                },
            ],
            allowed_external_addresses={self.external_a},
        )

        net = 1_000_000_000 - FEE
        first = (net + 1) // 2
        second = net // 2

        self.assertEqual(prepared.budget_sats, 1_000_000_000)
        self.assertEqual(prepared.fee_sats, FEE)
        self.assertEqual(prepared.distributed_sats, net)
        self.assertEqual(
            prepared.destinations,
            (
                (self.internal, first),
                (self.external_a, second),
            ),
        )

    def test_fixed_distribution_preserves_amount_and_uses_remainder(self):
        prepared = self.b.build_distribution(
            "flab_source",
            800_000_000,
            "fixed",
            [
                {
                    "address": self.external_a,
                    "amount_sats": 300_000_000,
                },
                {
                    "address": self.internal,
                    "remainder": True,
                },
            ],
            allowed_external_addresses={self.external_a},
        )

        self.assertEqual(prepared.budget_sats, 800_000_000)
        self.assertEqual(prepared.fee_sats, FEE)
        self.assertEqual(
            prepared.destinations,
            (
                (self.external_a, 300_000_000),
                (self.internal, 500_000_000 - FEE),
            ),
        )

        self.assertIn(
            (prepared.change_address, 200_000_000),
            prepared.outputs,
        )

    def test_fixed_remainder_distribution_uses_successful_fee_probe_as_final_funding(self):
        original_fund = self.chain.fund_raw_transaction
        calls = []

        def fail_redundant_second_funding(*args, **kwargs):
            calls.append({
                "subtract_fee": kwargs.get("subtract_fee", False),
                "subtract_fee_indexes": kwargs.get(
                    "subtract_fee_indexes"
                ),
            })

            if len(calls) > 1:
                raise RpcError(
                    "The preselected coins total amount does not cover "
                    "the transaction target. Please allow other inputs "
                    "to be automatically selected or include more coins "
                    "manually",
                    -4,
                )

            return original_fund(*args, **kwargs)

        self.chain.fund_raw_transaction = (
            fail_redundant_second_funding
        )

        prepared = self.b.build_distribution(
            "flab_source",
            "all",
            "fixed",
            [
                {
                    "address": self.external_a,
                    "amount_sats": 300_000_000,
                },
                {
                    "address": self.internal,
                    "remainder": True,
                },
            ],
            allowed_external_addresses={self.external_a},
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0]["subtract_fee_indexes"],
            [1],
        )
        self.assertEqual(prepared.fee_sats, FEE)
        self.assertEqual(
            prepared.destinations,
            (
                (self.external_a, 300_000_000),
                (
                    self.internal,
                    700_000_000 - FEE,
                ),
            ),
        )

    def test_distribution_external_address_must_be_explicitly_approved(self):
        with self.assertRaisesRegex(
            BuildError,
            "explicitly approved external address",
        ):
            self.b.build_distribution(
                "flab_source",
                "all",
                "percentage",
                [
                    {
                        "address": self.external_a,
                        "percent_bps": 10_000,
                    },
                ],
            )

    def test_distribution_invalid_external_address_is_refused(self):
        bad = "not-valid"

        with self.assertRaisesRegex(
            BuildError,
            "not a valid DigiByte address",
        ):
            self.b.build_distribution(
                "flab_source",
                "all",
                "percentage",
                [
                    {
                        "address": bad,
                        "percent_bps": 10_000,
                    },
                ],
                allowed_external_addresses={bad},
            )

    def test_percentage_distribution_must_total_exactly_100_percent(self):
        with self.assertRaisesRegex(
            BuildError,
            "exactly 10000 basis points",
        ):
            self.b.build_distribution(
                "flab_source",
                "all",
                "percentage",
                [
                    {
                        "address": self.internal,
                        "percent_bps": 9000,
                    },
                ],
            )

    def test_fixed_distribution_requires_exactly_one_remainder(self):
        with self.assertRaisesRegex(
            BuildError,
            "exactly one remainder",
        ):
            self.b.build_distribution(
                "flab_source",
                "all",
                "fixed",
                [
                    {
                        "address": self.internal,
                        "amount_sats": 100_000_000,
                    },
                ],
            )

    def test_distribution_refuses_duplicate_destination_addresses(self):
        with self.assertRaisesRegex(
            BuildError,
            "must be unique",
        ):
            self.b.build_distribution(
                "flab_source",
                "all",
                "percentage",
                [
                    {
                        "address": self.internal,
                        "percent_bps": 5000,
                    },
                    {
                        "address": self.internal,
                        "percent_bps": 5000,
                    },
                ],
            )

    def test_distribution_refuses_more_than_ten_destinations(self):
        items = [
            {
                "address": f"dgb1qexternal{i}",
                "percent_bps": 1000 if i < 10 else 1,
            }
            for i in range(11)
        ]

        with self.assertRaisesRegex(
            BuildError,
            "between 1 and 10 destinations",
        ):
            self.b.build_distribution(
                "flab_source",
                "all",
                "percentage",
                items,
                allowed_external_addresses={
                    item["address"] for item in items
                },
            )


class PartialDistributionBuildTests(unittest.TestCase):
    def setUp(self):
        self.chain = FakeChain(WALLETS)
        self.chain.fund("flab_source", 1_000_000_000)
        self.chain.mine(3)
        self.b = TxBuilder(
            self.chain,
            WALLETS,
            max_fee_sats=10_000_000,
        )
        self.internal = self.chain.get_new_address(
            "flab_dest",
            "partial-distribution-test",
        )
        self.external_a = "dgb1qexternalone"
        self.external_b = "dgb1qexternaltwo"

    def test_fixed_partial_distribution_retains_unassigned_value_as_change(self):
        prepared = self.b.build_partial_distribution(
            "flab_source",
            "all",
            "fixed",
            [{
                "address": self.external_a,
                "amount_sats": 300_000_000,
            }],
            allowed_external_addresses={self.external_a},
        )

        self.assertEqual(prepared.budget_sats, 1_000_000_000)
        self.assertEqual(prepared.fee_sats, FEE)
        self.assertEqual(prepared.distributed_sats, 300_000_000)
        self.assertEqual(
            prepared.destinations,
            ((self.external_a, 300_000_000),),
        )
        self.assertIn(
            (
                prepared.change_address,
                700_000_000 - FEE,
            ),
            prepared.outputs,
        )

    def test_percentage_partial_distribution_uses_gross_budget(self):
        prepared = self.b.build_partial_distribution(
            "flab_source",
            "all",
            "percentage",
            [
                {
                    "address": self.internal,
                    "percent_bps": 2000,
                },
                {
                    "address": self.external_a,
                    "percent_bps": 500,
                },
            ],
            allowed_external_addresses={self.external_a},
        )

        self.assertEqual(prepared.budget_sats, 1_000_000_000)
        self.assertEqual(prepared.fee_sats, FEE)
        self.assertEqual(
            prepared.destinations,
            (
                (self.internal, 200_000_000),
                (self.external_a, 50_000_000),
            ),
        )
        self.assertEqual(
            prepared.distributed_sats,
            250_000_000,
        )
        self.assertIn(
            (
                prepared.change_address,
                750_000_000 - FEE,
            ),
            prepared.outputs,
        )

    def test_partial_percentage_must_total_less_than_100_percent(self):
        with self.assertRaisesRegex(
            BuildError,
            "less than 10000 basis points",
        ):
            self.b.build_partial_distribution(
                "flab_source",
                "all",
                "percentage",
                [{
                    "address": self.internal,
                    "percent_bps": 10_000,
                }],
            )

    def test_partial_fixed_payouts_must_leave_retained_value_for_fee(self):
        with self.assertRaisesRegex(
            BuildError,
            "must leave retained value",
        ):
            self.b.build_partial_distribution(
                "flab_source",
                "all",
                "fixed",
                [{
                    "address": self.internal,
                    "amount_sats": 1_000_000_000,
                }],
            )

    def test_partial_distribution_external_address_requires_approval(self):
        with self.assertRaisesRegex(
            BuildError,
            "explicitly approved external address",
        ):
            self.b.build_partial_distribution(
                "flab_source",
                "all",
                "fixed",
                [{
                    "address": self.external_a,
                    "amount_sats": 100_000_000,
                }],
            )

    def test_partial_distribution_refuses_duplicate_addresses(self):
        with self.assertRaisesRegex(
            BuildError,
            "must be unique",
        ):
            self.b.build_partial_distribution(
                "flab_source",
                "all",
                "fixed",
                [
                    {
                        "address": self.internal,
                        "amount_sats": 100_000_000,
                    },
                    {
                        "address": self.internal,
                        "amount_sats": 50_000_000,
                    },
                ],
            )


class BroadcastTests(NodeCase, test_engine.Base):
    def setUp(self):
        super().setUp()
        self.make_node()
        self.exp, self.flow, self.job = self.to_execute_with_job()
        self.p = self.b.build("flab_source", DEST, 100_000_000)

    def tearDown(self):
        self.n.close()
        super().tearDown()

    def journal(self):
        return [r[0] for r in self.e.conn.execute("SELECT status FROM action_journal")]

    def test_broadcast_records_everything(self):
        self.assertEqual(broadcast(self.e, self.rpc, self.job, self.p), TXID)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.journal(), ["done"])
        self.assertEqual(self.e.get_job(self.job)["state"], "BROADCAST")

    def test_second_broadcast_for_same_job_refused(self):
        broadcast(self.e, self.rpc, self.job, self.p)
        with self.assertRaises(Exception):
            broadcast(self.e, self.rpc, self.job, self.p)
        self.assertEqual(len(self.sent), 1)

    def test_definite_rejection_marks_failed(self):
        def boom(hx): raise RpcError("sendrawtransaction: bad-txns", -26)
        self.rpc.send_raw_transaction = boom
        with self.assertRaises(RpcError):
            broadcast(self.e, self.rpc, self.job, self.p)
        self.assertEqual(self.journal(), ["failed"])
        self.assertEqual(self.e.get_job(self.job)["state"], "PLANNED")

    def test_timeout_leaves_intent_and_blocks_retry(self):
        calls = []
        def boom(hx):
            calls.append(1); raise RpcError("sendrawtransaction: timeout")
        self.rpc.send_raw_transaction = boom
        with self.assertRaises(RpcError):
            broadcast(self.e, self.rpc, self.job, self.p)
        self.assertEqual(self.journal(), ["intent"])
        with self.assertRaises(Exception):
            broadcast(self.e, self.rpc, self.job, self.p)
        self.assertEqual(len(calls), 1)

    def test_already_in_mempool_is_not_a_clean_failure(self):
        def boom(hx): raise RpcError("sendrawtransaction: txn-already-in-mempool", -26)
        self.rpc.send_raw_transaction = boom
        with self.assertRaises(RpcError):
            broadcast(self.e, self.rpc, self.job, self.p)
        self.assertEqual(self.journal(), ["intent"])

    def test_emergency_stop_blocks_broadcast(self):
        self.e.emergency_stop("test")
        with self.assertRaises(Exception):
            broadcast(self.e, self.rpc, self.job, self.p)
        self.assertEqual(self.sent, [])

    def test_failed_verification_blocks_broadcast(self):
        self.v.fail = {"balance": "low"}
        with self.assertRaises(Exception):
            broadcast(self.e, self.rpc, self.job, self.p)
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
