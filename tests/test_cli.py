import contextlib
import copy
import io
import json
import os
import tempfile
import unittest

from flowlab import ConfigError, Engine
from flowlab.cli import main
from flowlab.config_schema import validate
from tests.fake_chain import FakeChain
from tests.test_executor import CFG, W
from tests.test_planner import PlannerBase


def cfg_repeat(**over):
    c = copy.deepcopy(CFG)
    fl = c["flows"][0]
    del fl["transfers"]
    fl["repeat"] = {"cycle": ["flab_source", "flab_a", "flab_b"], "count": 7,
                    "amount_sats": 200_000_000, "step_down_sats": 1_500_000, "delay_seconds": 5}
    fl["repeat"].update(over)
    return c


class RepeatTests(unittest.TestCase):
    def test_expands_to_plain_explicit_transfers(self):
        fl = validate(cfg_repeat())["flows"][0]
        self.assertNotIn("repeat", fl)
        ts = fl["transfers"]
        self.assertEqual(len(ts), 7)
        self.assertEqual([t["from"] for t in ts[:4]], ["flab_source", "flab_a", "flab_b", "flab_source"])
        self.assertEqual([t["to"] for t in ts[:4]], ["flab_a", "flab_b", "flab_source", "flab_a"])
        self.assertEqual([t["amount_sats"] for t in ts[:3]], [200_000_000, 198_500_000, 197_000_000])
        self.assertTrue(all(t["delay_seconds"] == 5 for t in ts))

    def test_same_input_gives_same_output_every_time(self):
        self.assertEqual(validate(cfg_repeat()), validate(cfg_repeat()))

    def test_rejections(self):
        for bad in (dict(count=0), dict(count=501), dict(amount_sats=0), dict(delay_seconds=-1),
                    dict(step_down_sats=-1), dict(cycle=["flab_source"]),
                    dict(cycle=["flab_a", "flab_b"]), dict(cycle=["flab_source", "flab_source"]),
                    dict(cycle=["flab_source", "w9_unknown"]), dict(step_down_sats=100_000_000),
                    dict(amount_sats=400_000_000)):
            with self.assertRaises(ConfigError, msg=str(bad)):
                validate(cfg_repeat(**bad))

    def test_count_limit_stands_alone(self):
        validate(cfg_repeat(count=500, amount_sats=1_000_000, step_down_sats=0))
        with self.assertRaises(ConfigError):
            validate(cfg_repeat(count=501, amount_sats=1_000_000, step_down_sats=0))

    def test_first_hop_must_leave_the_source(self):
        with self.assertRaises(ConfigError):
            validate(cfg_repeat(cycle=["flab_a", "flab_source", "flab_b"]))

    def test_cannot_mix_repeat_and_transfers(self):
        c = cfg_repeat()
        c["flows"][0]["transfers"] = copy.deepcopy(CFG["flows"][0]["transfers"])
        with self.assertRaises(ConfigError):
            validate(c)

    def test_randomization_requires_a_seed(self):
        with self.assertRaises(ConfigError):
            validate(dict(cfg_repeat(), randomization={"enabled": True, "model": "uniform"}))

    def test_seeded_randomization_validates(self):
        cfg = dict(
            cfg_repeat(),
            randomization={"enabled": True, "model": "uniform", "seed": 12345},
        )
        cfg["workload"] = {
            "mode": "count",
            "jobs": 7,
            "amount_sats_min": 10_000_000,
            "amount_sats_max": 100_000_000,
            "delay_seconds_min": 0,
            "delay_seconds_max": 30,
        }

        got = validate(cfg)
        self.assertEqual(
            got["randomization"],
            {"enabled": True, "model": "uniform", "seed": 12345},
        )


class ReviewTests(PlannerBase):
    def test_review_lists_every_transfer(self):
        exp = self.e.create_experiment("r")
        self.e.configure_experiment(exp, cfg_repeat())
        text = self.e.review(exp)["text"]
        self.assertIn("  7. ", text)
        self.assertIn("flab_source -> flab_a   200000000 sats   wait 5s", text)


class CliFlow(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.dir.name, "f.db")
        self.cfgfile = os.path.join(self.dir.name, "c.json")
        c = cfg_repeat(count=4, delay_seconds=0)
        c["flows"][0]["allocation_sats"] = 300_000_000
        with open(self.cfgfile, "w") as f:
            json.dump(c, f)
        self.chain = FakeChain(W)
        self.chain.fund("flab_source", 3_500_000_000)
        self.chain.mine(3)
        self.lines = []

    def tearDown(self):
        self.dir.cleanup()

    def call(self, *argv, rpc=None):
        def sleep(s):
            self.chain.mine(2)
        code = main(list(argv), rpc=rpc, out=self.lines.append, sleep=sleep, db=self.db)
        return code, "\n".join(map(str, self.lines))

    def new_exp(self):
        self.call("new", self.cfgfile)
        line = [l for l in self.lines if str(l).startswith("\nTo approve")][0].split()
        return line[-2], line[-1]

    def make_and_approve(self):
        exp, h = self.new_exp()
        self.call("approve", exp, h)
        return exp

    def test_new_review_approve_run_status(self):
        exp = self.make_and_approve()
        code, text = self.call("run", exp, rpc=self.chain)
        self.assertEqual(code, 0, text)
        self.assertIn("finished", text)
        self.call("status", exp)
        self.assertIn("COMPLETE", "\n".join(self.lines))
        self.assertEqual(len([t for t in self.chain.txs.values() if t["inputs"]]), 4)

    def test_wrong_hash_is_refused(self):
        exp, _ = self.new_exp()
        code, text = self.call("approve", exp, "0" * 64)
        self.assertEqual(code, 1)
        self.assertIn("refused", text)

    def test_running_an_unapproved_experiment_is_refused(self):
        exp, _ = self.new_exp()
        code, text = self.call("run", exp, rpc=self.chain)
        self.assertNotEqual(code, 0)
        self.assertEqual(len([t for t in self.chain.txs.values() if t["inputs"]]), 0)

    def test_stop_aborts_and_run_then_does_nothing(self):
        exp = self.make_and_approve()
        self.call("stop")
        code, text = self.call("run", exp, rpc=self.chain)
        self.assertNotEqual(code, 0)
        self.assertEqual(len([t for t in self.chain.txs.values() if t["inputs"]]), 0)

    def test_read_only_commands_do_not_hide_a_crash(self):
        exp = self.make_and_approve()
        e = Engine(self.db)
        e.recover()                        # a run starts: the clean flag goes to 0 ...
        e.conn.close()                     # ... and the process dies without closing cleanly
        for argv in (["status", exp], ["review", exp], ["list"]):
            self.call(*argv)
        e = Engine(self.db)
        flag = e.conn.execute("SELECT value FROM meta WHERE key='clean_shutdown'").fetchone()[0]
        e.conn.close()
        self.assertEqual(flag, "0")

    def test_ctrl_c_is_clean_and_run_can_continue(self):
        exp = self.make_and_approve()
        calls = []
        def sleep(s):
            calls.append(1)
            if len(calls) == 3:
                raise KeyboardInterrupt
            self.chain.mine(2)
        code = main(["run", exp], rpc=self.chain, out=self.lines.append, sleep=sleep, db=self.db)
        self.assertEqual(code, 0)
        code, text = self.call("run", exp, rpc=self.chain)
        self.assertEqual(code, 0, text)
        self.assertIn("finished", text)
        self.assertEqual(len([t for t in self.chain.txs.values() if t["inputs"]]), 4)


if __name__ == "__main__":
    unittest.main()
