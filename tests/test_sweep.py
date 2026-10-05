import copy
import json
import os
import tempfile
import unittest

from flowlab import ConfigError
from flowlab.cli import main
from flowlab.config_schema import validate
from flowlab.tx_builder import BuildError, TxBuilder
from tests.fake_chain import FEE, FakeChain
from tests.test_cli import cfg_repeat
from tests.test_executor import CFG, W


def cfg_sweep(**over):
    c = cfg_repeat(count=4, delay_seconds=0, **over)
    c["flows"][0]["repeat"].pop("step_down_sats", None)
    c["flows"][0]["repeat"]["sweep"] = True
    c["flows"][0]["allocation_sats"] = 300_000_000
    return c


class SweepConfig(unittest.TestCase):
    def test_repeat_sweep_expands_to_all_out_of_flow_wallets(self):
        ts = validate(cfg_sweep())["flows"][0]["transfers"]
        self.assertEqual([t["amount_sats"] for t in ts], [200_000_000, "all", "all", 200_000_000])
        self.assertEqual([t["from"] for t in ts], ["flab_source", "flab_a", "flab_b", "flab_source"])

    def test_sweep_cannot_combine_with_step_down(self):
        c = cfg_sweep()
        c["flows"][0]["repeat"]["step_down_sats"] = 1
        with self.assertRaises(ConfigError):
            validate(c)

    def test_all_is_refused_out_of_the_source(self):
        c = copy.deepcopy(CFG)
        c["flows"][0]["transfers"][0]["amount_sats"] = "all"
        with self.assertRaises(ConfigError):
            validate(c)

    def test_all_needs_something_to_send(self):
        c = copy.deepcopy(CFG)
        c["flows"][0]["transfers"] = [
            {"from": "flab_a", "to": "flab_b", "amount_sats": "all", "delay_seconds": 0}]
        with self.assertRaises(ConfigError):
            validate(c)

    def test_other_strings_are_refused(self):
        c = copy.deepcopy(CFG)
        c["flows"][0]["transfers"][1]["amount_sats"] = "everything"
        with self.assertRaises(ConfigError):
            validate(c)


class SweepBuild(unittest.TestCase):
    def setUp(self):
        self.chain = FakeChain(W)
        self.chain.fund("flab_a", 150_000_000)
        self.chain.fund("flab_a", 600_000)
        self.chain.mine(2)
        self.b = TxBuilder(self.chain, W, max_fee_sats=10_000_000)

    def test_sweep_sends_everything_minus_fee_with_no_change(self):
        addr = self.chain.get_new_address("flab_b")
        p = self.b.build("flab_a", addr, "all")
        self.assertEqual(p.amount_sats, 150_600_000 - FEE)
        self.assertEqual(len(p.outputs), 1)
        self.assertEqual(p.outputs[0][0], addr)
        self.assertEqual(len(p.inputs), 2)

    def test_empty_wallet_is_refused(self):
        with self.assertRaises(BuildError):
            self.b.build("flab_dest", self.chain.get_new_address("flab_b"), "all")

    def test_balance_below_the_fee_is_refused(self):
        self.chain.fund("flab_b", 100)
        self.chain.mine(2)
        with self.assertRaises(BuildError):
            self.b.build("flab_b", self.chain.get_new_address("flab_a"), "all")

    def test_only_our_own_wallets_can_receive_a_sweep(self):
        with self.assertRaises(BuildError):
            self.b.build("flab_a", "dgb1qnotours", "all")


class SweepRun(unittest.TestCase):
    def test_full_run_leaves_nothing_behind_in_flow_wallets(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        db, cf = os.path.join(d.name, "f.db"), os.path.join(d.name, "c.json")
        with open(cf, "w") as f:
            json.dump(cfg_sweep(), f)
        chain = FakeChain(W)
        chain.fund("flab_source", 3_500_000_000)
        chain.mine(3)
        lines = []

        def call(*argv, rpc=None):
            return main(list(argv), rpc=rpc, out=lines.append, sleep=lambda s: chain.mine(2), db=db)

        call("new", cf)
        l = [x for x in lines if str(x).startswith("\nTo approve")][0].split()
        exp = l[-2]
        self.assertTrue(any("ENTIRE BALANCE" in str(x) for x in lines))
        call("approve", exp, l[-1])
        self.assertEqual(call("run", exp, rpc=chain), 0, "\n".join(map(str, lines)))
        self.assertEqual(len([t for t in chain.txs.values() if t["inputs"]]), 4)
        self.assertEqual(chain.get_balances("flab_b")["mine"]["trusted"], 0)
        outs = sorted(len(t["outputs"]) for t in chain.txs.values() if t["inputs"])
        self.assertEqual(outs, [1, 1, 2, 2])
        self.assertTrue(any("COMPLETE" in str(x) for x in (call("status", exp), lines)[1]))


if __name__ == "__main__":
    unittest.main()
