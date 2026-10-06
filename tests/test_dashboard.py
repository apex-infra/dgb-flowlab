import hashlib
import json
import os
import tempfile
import unittest

from flowlab import Engine
from flowlab.cli import main
from flowlab.dashboard import dgb, snapshot
from tests.fake_chain import FakeChain
from tests.test_executor import EXP_CFG, W
from tests.test_sweep import cfg_sweep


class DashBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.db = os.path.join(self.dir.name, "f.db")
        self.cf = os.path.join(self.dir.name, "c.json")
        with open(self.cf, "w") as f:
            json.dump(cfg_sweep(), f)
        self.chain = FakeChain(W)
        self.chain.fund("flab_source", 3_500_000_000)
        self.chain.mine(3)
        self.lines = []

    def call(self, *argv, sleep=None):
        self.lines.clear()
        code = main(list(argv), rpc=self.chain, out=self.lines.append,
                    sleep=sleep or (lambda s: self.chain.mine(2)), db=self.db)
        return code, "\n".join(map(str, self.lines))

    def make(self):
        self.call("new", self.cf)
        line = [l for l in self.lines if str(l).startswith("\nTo approve")][0].split()
        self.call("approve", line[-2], line[-1])
        return line[-2]

    def digest(self):
        with open(self.db, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()


class Formatting(unittest.TestCase):
    def test_dgb_is_exact(self):
        self.assertEqual(dgb(200_000_000), "2.00000000")
        self.assertEqual(dgb(1_411_146), "0.01411146")
        self.assertEqual(dgb(0), "0.00000000")


class DashboardTests(DashBase):
    def test_finished_run_shows_hops_fees_balances_and_activity(self):
        exp = self.make()
        self.assertEqual(self.call("run", exp)[0], 0)
        code, text = self.call("watch", exp, "--once")
        self.assertEqual(code, 0)
        for want in (exp, "COMPLETE", "4/4 hops", "CONFIRMED", "flab_source", "flab_dest",
                     "WALLETS", "ACTIVITY", "TRANSACTION BROADCAST"):
            self.assertIn(want, text)
        self.assertNotIn("PLANNED", text)

    def test_mid_run_shows_planned_hops_and_countdown(self):
        exp = self.make()
        calls = []

        def stop_after_first_hop(s):
            calls.append(1)
            if len(calls) == 6:
                raise KeyboardInterrupt
            self.chain.mine(2)
        self.call("run", exp, sleep=stop_after_first_hop)
        text = self.call("watch", exp, "--once")[1]
        self.assertIn("CONFIRMED", text)
        self.assertIn("PLANNED", text)

    def test_experimental_snapshot_exposes_safe_progress_metadata(self):
        with open(self.cf, "w") as f:
            json.dump(EXP_CFG, f)

        exp = self.make()
        before = snapshot(self.db, exp)

        self.assertEqual(before["mode"], "experimental")
        self.assertEqual(before["target_jobs"], EXP_CFG["workload"]["jobs"])
        self.assertEqual(before["randomization"], {
            "model": EXP_CFG["randomization"]["model"],
            "seed": EXP_CFG["randomization"]["seed"],
        })
        self.assertEqual(before["flows"][0]["jobs"], [])

        self.assertEqual(self.call("run", exp)[0], 0)

        after = snapshot(self.db, exp)
        jobs = after["flows"][0]["jobs"]

        self.assertTrue(all(j["generated"] for j in jobs))

        decisions = [
            j for j in jobs
            if isinstance(j["generated"].get("decision_index"), int)
        ]
        finalizations = [
            j for j in jobs
            if j["generated"].get("phase") == "finalization"
        ]

        self.assertEqual(len(decisions), EXP_CFG["workload"]["jobs"])
        self.assertEqual(
            [j["generated"]["decision_index"] for j in decisions],
            list(range(EXP_CFG["workload"]["jobs"])),
        )
        self.assertGreaterEqual(len(finalizations), 1)

        for job in decisions:
            generated = job["generated"]
            self.assertIn("generator_version", generated)
            self.assertIn("model", generated)
            self.assertIn("seed", generated)
            self.assertIn("balance_snapshot_sats", generated)
            self.assertIn("fee_reserve_sats", generated)
            self.assertIn("delay_s", job)

        for job in finalizations:
            self.assertEqual(
                job["generated"]["source"],
                "experimental finalization",
            )
            self.assertIn(job["generated"]["worker"], ("flab_a", "flab_b"))

        for job in jobs:
            generated = job["generated"]

            # Snapshot exposure is explicitly whitelisted.
            self.assertLessEqual(set(generated), {
                "source",
                "generator_version",
                "model",
                "seed",
                "flow_index",
                "decision_index",
                "eligible_transition_count",
                "observed_balance_sats",
                "fee_reserve_sats",
                "balance_snapshot_sats",
                "source_budget_used_sats",
                "source_budget_remaining_sats",
                "phase",
                "worker",
            })

    def test_deterministic_snapshot_reports_deterministic_mode(self):
        exp = self.make()
        snap = snapshot(self.db, exp)

        self.assertEqual(snap["mode"], "deterministic")
        self.assertIsNone(snap["target_jobs"])
        self.assertIsNone(snap["randomization"])


    def test_watching_changes_nothing(self):
        exp = self.make()
        self.call("run", exp)
        before = self.digest()
        for _ in range(3):
            self.call("watch", exp, "--once")
            self.call("watch", "--once")
        self.assertEqual(self.digest(), before)

    def test_fresh_full_balance_run_does_not_crash_on_its_own_events(self):
        exp = self.make()
        self.call("run", exp, sleep=lambda s: (_ for _ in ()).throw(KeyboardInterrupt()))
        code, text = self.call("watch", exp, "--once")
        self.assertEqual(code, 0)
        self.assertIn("Job generated".upper(), text.upper())

    def test_no_colour_codes_leak_into_plain_text(self):
        from flowlab.dashboard import render, snapshot
        exp = self.make()
        self.call("run", exp)
        text = render(snapshot(self.db, exp), None, 100, color=False)
        self.assertNotIn("\x1b", text)

    def test_missing_database_is_a_clear_message_not_a_crash(self):
        lines = []
        code = main(["watch", "--once"], rpc=None, out=lines.append, sleep=lambda s: None,
                    db=os.path.join(self.dir.name, "nope.db"), local_path=os.path.join(self.dir.name, "none.json"))
        self.assertEqual(code, 1)
        self.assertIn("no database", "\n".join(lines))

    def test_empty_database_says_so(self):
        Engine(self.db).close()
        code, text = self.call("watch", "--once")
        self.assertEqual(code, 1)
        self.assertIn("no experiment", text)

    def test_ctrl_c_leaves_cleanly(self):
        exp = self.make()

        def boom(s):
            raise KeyboardInterrupt
        code, _ = self.call("watch", exp, sleep=boom)
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
