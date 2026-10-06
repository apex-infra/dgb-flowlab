import unittest
from decimal import Decimal

from flowlab.rpc import RpcClient, RpcError
from flowlab.tx_builder import BuildError, TxBuilder, broadcast
from tests import test_engine
from tests.fake_node import FakeNode

WALLETS = ["flab_source", "flab_a", "flab_b", "flab_dest"]
DEST = "dgb1qdest"
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
