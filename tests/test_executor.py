import copy
import json
import os
import tempfile
import unittest
from decimal import Decimal
from datetime import datetime, timedelta, timezone

from flowlab import Engine
from flowlab.executor import Executor
from flowlab.node_verifier import NodeVerifier
from flowlab.rpc import RpcError
from flowlab.tx_builder import BuildError, TxBuilder
from tests.fake_chain import FEE, FakeChain
from tests.test_engine import FakeVerifier

W = ["flab_source", "flab_a", "flab_b", "flab_dest"]
CFG = {
    "flows": [{"description": "explicit", "source_wallet": "flab_source",
               "flow_wallets": ["flab_a", "flab_b"], "destination_wallet": "flab_dest",
               "allocation_sats": 300_000_000,
               "transfers": [
                   {"from": "flab_source", "to": "flab_a", "amount_sats": 200_000_000, "delay_seconds": 30},
                   {"from": "flab_a", "to": "flab_b", "amount_sats": 150_000_000, "delay_seconds": 60},
                   {"from": "flab_b", "to": "flab_dest", "amount_sats": 100_000_000, "delay_seconds": 0}]}],
    "workload": {"mode": "count", "jobs": 3, "amount_sats_min": 100_000_000,
                 "amount_sats_max": 300_000_000, "delay_seconds_min": 0, "delay_seconds_max": 60},
    "confirmations_required": 2,
    "fee_policy": {"type": "minimum"},
    "address_policy": "new",
}


EXP_CFG = copy.deepcopy(CFG)
EXP_CFG["flows"][0].pop("transfers")
EXP_CFG["flows"][0]["experimental_topology"] = {
    "transitions": [
        {"from": "flab_source", "to": "flab_a"},
        {"from": "flab_source", "to": "flab_b"},
        {"from": "flab_a", "to": "flab_b"},
        {"from": "flab_b", "to": "flab_a"},
    ]
}
EXP_CFG["workload"] = {
    "mode": "count",
    "jobs": 4,
    "amount_sats_min": 50_000_000,
    "amount_sats_max": 100_000_000,
    "delay_seconds_min": 0,
    "delay_seconds_max": 5,
}
EXP_CFG["randomization"] = {
    "enabled": True,
    "model": "uniform",
    "seed": 2262026,
}
EXP_CFG["finalization"] = {
    "mode": "sweep_workers_to_destination",
}


class ExecBase(unittest.TestCase):
    use_node_verifier = False
    config = CFG

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.t = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)
        self.chain = FakeChain(W)
        self.chain.fund("flab_source", 3_500_000_000)
        self.chain.mine(3)
        self.v = NodeVerifier(self.chain) if self.use_node_verifier else FakeVerifier()
        self.e = Engine(os.path.join(self.dir.name, "x.db"), verifier=self.v, clock=lambda: self.t)
        self.b = TxBuilder(self.chain, W, max_fee_sats=10_000_000)
        self.x = Executor(self.e, self.chain, self.b)
        exp = self.e.create_experiment("exec")
        self.e.approve(exp, self.e.configure_experiment(
            exp, copy.deepcopy(self.config)
        ))
        self.e.start(exp)
        self.exp = exp

    def tearDown(self):
        self.e.conn.close()
        self.dir.cleanup()

    def run_all(self, limit=200):
        last = None
        for _ in range(limit):
            last = self.x.tick(self.exp)
            if last["done"] or last["blocked"]:
                return last
            self.t += timedelta(seconds=last["wait_s"] or 1)
            self.chain.mine(2)
        self.fail("did not finish")

    def sent(self):
        return [t for t in self.chain.txs.values() if t["inputs"]]


class RunTests(ExecBase):
    def test_full_run_moves_exactly_the_listed_amounts(self):
        r = self.run_all()
        self.assertTrue(r["done"], r)
        exp = self.e.get_experiment(self.exp)
        self.assertEqual(exp["state"], "COMPLETE")
        self.assertEqual(len(self.sent()), 3)
        stats = json.loads(exp["stats_json"])
        self.assertEqual((stats["jobs"], stats["total_fees_sats"], stats["total_moved_sats"]),
                         (3, 3 * FEE, 450_000_000))
        self.assertEqual(stats["issues"], [])
        bal = json.loads(exp["final_state_json"])["balances_sats"]
        self.assertEqual(bal["flab_dest"], 100_000_000)
        self.assertEqual(bal["flab_b"], 150_000_000 - 100_000_000 - FEE)
        self.assertEqual(bal["flab_a"], 200_000_000 - 150_000_000 - FEE)
        self.assertEqual(bal["flab_source"], 3_500_000_000 - 200_000_000 - FEE)

    def test_delays_are_respected(self):
        self.x.tick(self.exp)                       # START -> PLAN, jobs created
        r = self.x.tick(self.exp)
        self.assertEqual((r["actions"], r["wait_s"]), ([], 30))
        self.assertEqual(self.sent(), [])
        self.t += timedelta(seconds=29)
        self.assertEqual(self.x.tick(self.exp)["actions"], [])
        self.t += timedelta(seconds=1)
        self.assertEqual(len(self.x.tick(self.exp)["actions"]), 1)   # step 1 is due

    def test_waits_for_confirmations_before_next_step(self):
        for _ in range(3):
            self.x.tick(self.exp)
            self.t += timedelta(seconds=30)
        self.x.tick(self.exp)                       # PLAN -> EXECUTE
        self.x.tick(self.exp)                       # broadcast step 1
        self.assertEqual(len(self.sent()), 1)
        for _ in range(5):                          # no blocks mined: nothing else may happen
            r = self.x.tick(self.exp)
            self.t += timedelta(seconds=500)
        self.assertEqual(len(self.sent()), 1)
        self.chain.mine(2)
        r = self.x.tick(self.exp)
        self.assertTrue(any("confirmed" in a for a in r["actions"]), r)

    def test_build_refusal_pauses_and_sends_nothing(self):
        for u in self.chain.utxos.values():
            u["sats"] = 10_000_000                  # source can no longer fund step 1
        r = self.run_all()
        self.assertIn("insufficient", r["blocked"])
        self.assertEqual(self.e.get_experiment(self.exp)["state"], "PAUSED")
        self.assertEqual(self.sent(), [])

    def test_interrupted_broadcast_blocks_everything(self):
        self.chain.fail_send = RpcError("sendrawtransaction: timeout")
        r = self.run_all()
        self.assertEqual(self.e.get_experiment(self.exp)["state"], "PAUSED")
        status = [x[0] for x in self.e.conn.execute("SELECT status FROM action_journal")]
        self.assertEqual(status, ["intent"])
        self.assertEqual(self.sent(), [])
        self.chain.fail_send = None
        r = self.x.tick(self.exp)                   # PAUSED: nothing may run until a human resumes
        self.assertIn("PAUSED", r["blocked"])
        self.assertEqual(self.sent(), [])

    def test_emergency_stop_halts_the_run(self):
        for _ in range(4):
            self.x.tick(self.exp)
            self.t += timedelta(seconds=40)
        before = len(self.sent())
        self.e.emergency_stop("test")
        for _ in range(3):
            r = self.x.tick(self.exp)
            self.t += timedelta(seconds=500)
            self.chain.mine(2)
            self.assertIsNotNone(r["blocked"])
        self.assertEqual(len(self.sent()), before)

    def test_self_transfer_reconciliation_uses_exact_transaction_output(self):
        self.chain.fund("flab_a", 500_000_000)
        self.chain.mine(3)

        flow = self.e.list_flows(self.exp)[0]["id"]
        self.e.advance_flow(flow, "PLAN")

        jid = self.e.add_job(
            flow,
            {
                "from": "flab_a",
                "to": "flab_a",
                "amount_sats": 100_000_000,
                "step": 0,
            },
            planned_delay_s=0,
            generated_from={"source": "self-transfer reconciliation test"},
        )

        self.e.advance_flow(flow, "EXECUTE")
        self.x.tick(self.exp)          # build + broadcast

        self.chain.mine(2)
        self.x.tick(self.exp)          # confirmation

        job = self.e.get_job(jid)
        self.assertEqual(job["state"], "CONFIRMED")

        recorded = json.loads(job["result_json"])
        self.assertEqual(recorded["amount_sats"], 100_000_000)
        self.assertTrue(recorded["address"])

        ok, final_state, stats = self.x.reconcile(self.exp)

        self.assertTrue(ok, stats)
        self.assertEqual(stats["issues"], [])
        self.assertEqual(stats["total_moved_sats"], 100_000_000)
        self.assertEqual(stats["total_fees_sats"], FEE)
        self.assertIn("flab_a", final_state["balances_sats"])

    def test_self_transfer_reconciliation_does_not_require_getrawtransaction(self):
        self.chain.fund("flab_a", 500_000_000)
        self.chain.mine(3)

        flow = self.e.list_flows(self.exp)[0]["id"]
        self.e.advance_flow(flow, "PLAN")

        jid = self.e.add_job(
            flow,
            {
                "from": "flab_a",
                "to": "flab_a",
                "amount_sats": 100_000_000,
                "step": 0,
            },
            planned_delay_s=0,
            generated_from={"source": "self-transfer reconciliation test"},
        )

        self.e.advance_flow(flow, "EXECUTE")
        self.x.tick(self.exp)

        self.chain.mine(2)
        self.x.tick(self.exp)

        def unavailable(*args, **kwargs):
            raise RpcError("getrawtransaction unavailable without txindex", -5)

        self.chain.get_raw_transaction = unavailable

        ok, _, stats = self.x.reconcile(self.exp)

        self.assertTrue(ok, stats)
        self.assertEqual(stats["issues"], [])
        self.assertEqual(self.e.get_job(jid)["state"], "CONFIRMED")

    def test_self_transfer_reconciliation_catches_wrong_output_amount(self):
        self.chain.fund("flab_a", 500_000_000)
        self.chain.mine(3)

        flow = self.e.list_flows(self.exp)[0]["id"]
        self.e.advance_flow(flow, "PLAN")

        jid = self.e.add_job(
            flow,
            {
                "from": "flab_a",
                "to": "flab_a",
                "amount_sats": 100_000_000,
                "step": 0,
            },
            planned_delay_s=0,
            generated_from={"source": "self-transfer reconciliation test"},
        )

        self.e.advance_flow(flow, "EXECUTE")
        self.x.tick(self.exp)

        self.chain.mine(2)
        self.x.tick(self.exp)

        job = self.e.get_job(jid)
        recorded = json.loads(job["result_json"])
        target = recorded["address"]

        real = self.chain.decode_raw_transaction

        def liar(txhex):
            result = copy.deepcopy(real(txhex))
            for output in result["vout"]:
                if output["scriptPubKey"].get("address") == target:
                    output["value"] -= Decimal("0.00000001")
                    break
            return result

        self.chain.decode_raw_transaction = liar

        ok, _, stats = self.x.reconcile(self.exp)

        self.assertFalse(ok)
        self.assertTrue(
            any("self-transfer output was" in issue for issue in stats["issues"]),
            stats,
        )

    def test_reconciliation_failure_goes_to_error_not_complete(self):
        real = self.chain.get_transaction
        def liar(wallet, txid):
            res = real(wallet, txid)
            if wallet == "flab_dest":
                res["amount"] = res["amount"] - 1
            return res
        self.chain.get_transaction = liar
        r = self.run_all()
        self.assertTrue(r["done"])
        self.assertTrue(any("RECONCILIATION FAILED" in a for a in r["actions"]))
        self.assertEqual(self.e.get_experiment(self.exp)["state"], "ERROR")

    def test_reconciliation_catches_a_fee_mismatch(self):
        real = self.chain.get_transaction
        def liar(wallet, txid):
            res = real(wallet, txid)
            if "fee" in res:
                res["fee"] = res["fee"] * 2
            return res
        self.chain.get_transaction = liar
        r = self.run_all()
        self.assertTrue(any("RECONCILIATION FAILED" in a for a in r["actions"]))
        self.assertEqual(self.e.get_experiment(self.exp)["state"], "ERROR")


class ExperimentalRunTests(ExecBase):
    config = EXP_CFG

    def test_progressive_run_generates_exact_approved_count(self):
        r = self.run_all()

        self.assertTrue(r["done"], r)
        self.assertEqual(self.e.get_experiment(self.exp)["state"], "COMPLETE")

        flow = self.e.list_flows(self.exp)[0]
        jobs = self.e.list_jobs(flow["id"])

        self.assertTrue(all(j["state"] == "CONFIRMED" for j in jobs))

        generated = [
            (j, json.loads(j["generated_from_json"]))
            for j in jobs
        ]
        decisions = [
            (j, meta)
            for j, meta in generated
            if meta.get("source") == "seeded experimental generator"
        ]
        finalizations = [
            (j, meta)
            for j, meta in generated
            if meta.get("source") == "experimental finalization"
        ]

        self.assertEqual(len(decisions), 4)
        self.assertEqual(
            [meta["decision_index"] for _, meta in decisions],
            [0, 1, 2, 3],
        )
        self.assertGreaterEqual(len(finalizations), 1)
        self.assertLessEqual(len(finalizations), 2)
        self.assertEqual(len(self.sent()), len(jobs))

        flow_wallets = json.loads(flow["flow_wallets_json"])
        for job, meta in finalizations:
            plan = json.loads(job["planned_json"])
            self.assertEqual(meta["phase"], "finalization")
            self.assertIn(plan["from"], flow_wallets)
            self.assertEqual(plan["to"], flow["destination_wallet"])
            self.assertEqual(plan["amount_sats"], "all")

        for worker in flow_wallets:
            self.assertEqual(
                self.chain.get_balances(worker)["mine"]["trusted"],
                0,
            )

    def test_only_one_experimental_job_exists_before_first_confirmation(self):
        self.x.tick(self.exp)      # START -> PLAN
        self.x.tick(self.exp)      # generate step 1 / possibly PLAN -> EXECUTE

        flow = self.e.list_flows(self.exp)[0]
        self.assertEqual(len(self.e.list_jobs(flow["id"])), 1)

        # No second progressive job may appear while the first is unfinished.
        for _ in range(3):
            self.x.tick(self.exp)

        self.assertEqual(len(self.e.list_jobs(flow["id"])), 1)

    def test_generated_jobs_record_balance_snapshot_and_fee_reserve(self):
        self.x.tick(self.exp)
        self.x.tick(self.exp)

        flow = self.e.list_flows(self.exp)[0]
        job = self.e.list_jobs(flow["id"])[0]
        generated = json.loads(job["generated_from_json"])

        self.assertIn("balance_snapshot_sats", generated)
        self.assertEqual(
            generated["fee_reserve_sats"],
            self.b.planning_fee_reserve_sats(),
        )


class NodeVerifierIntegration(ExecBase):
    use_node_verifier = True

    def test_full_run_with_the_real_verifier(self):
        self.assertTrue(self.run_all()["done"])
        self.assertEqual(self.e.get_experiment(self.exp)["state"], "COMPLETE")

    def test_extra_wallet_loaded_blocks_the_run(self):
        self.chain.wallets.append("pool")
        r = self.x.tick(self.exp)
        self.assertIn("verification failed", r["blocked"])
        self.assertEqual(self.e.get_experiment(self.exp)["state"], "PAUSED")
        self.assertEqual(self.sent(), [])


class FeeReserveTests(unittest.TestCase):
    def test_reserve_is_small_when_node_reports_relay_fee(self):
        c = FakeChain(W)
        c.fund("flab_source", 100_000_000)
        c.mine(3)
        b = TxBuilder(c, W, max_fee_sats=10_000_000)
        dest = c.get_new_address("flab_a")
        self.assertEqual(b.build("flab_source", dest, 95_000_000).amount_sats, 95_000_000)

    def test_reserve_falls_back_to_the_full_cap_if_node_will_not_say(self):
        c = FakeChain(W)
        c.fund("flab_source", 100_000_000)
        c.mine(3)
        def nope(): raise RpcError("no")
        c.get_network_info = nope
        b = TxBuilder(c, W, max_fee_sats=10_000_000)
        with self.assertRaises(BuildError):
            b.build("flab_source", c.get_new_address("flab_a"), 95_000_000)


if __name__ == "__main__":
    unittest.main()
