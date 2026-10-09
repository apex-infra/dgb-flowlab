import copy
import json
import os
import tempfile
import unittest
from decimal import Decimal
from datetime import datetime, timedelta, timezone

from flowlab import Engine
from flowlab.executor import POLL_SECONDS, Executor
from flowlab.node_verifier import NodeVerifier
from flowlab.plays import compile_play
from flowlab.rpc import RpcError, to_sats
from flowlab.tx_builder import BuildError, FeeLimitExceeded, TxBuilder
from tests.fake_chain import FEE, FakeChain
from tests.test_engine import FakeVerifier

W = ["flab_source", "flab_stage", "flab_a", "flab_b", "flab_dest"]
EXTERNAL = "dgb1qexternaldestination"

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


STAGED_EXP_CFG = copy.deepcopy(EXP_CFG)
STAGED_EXP_CFG["flows"][0]["allocation_wallet"] = "flab_stage"
STAGED_EXP_CFG["flows"][0]["flow_wallets"] = [
    "flab_stage",
    "flab_a",
    "flab_b",
]
STAGED_EXP_CFG["flows"][0]["experimental_topology"] = {
    "transitions": [
        {"from": "flab_stage", "to": "flab_a"},
        {"from": "flab_stage", "to": "flab_b"},
        {"from": "flab_a", "to": "flab_b"},
        {"from": "flab_b", "to": "flab_a"},
    ]
}


EXTERNAL_CFG = copy.deepcopy(CFG)
EXTERNAL_CFG["flows"][0].pop("destination_wallet")
EXTERNAL_CFG["flows"][0]["destination_address"] = EXTERNAL
EXTERNAL_CFG["flows"][0]["transfers"][-1]["to"] = EXTERNAL


EXTERNAL_EXP_CFG = copy.deepcopy(EXP_CFG)
EXTERNAL_EXP_CFG["flows"][0].pop("destination_wallet")
EXTERNAL_EXP_CFG["flows"][0]["destination_address"] = EXTERNAL


MULTI_EXP_CFG = copy.deepcopy(STAGED_EXP_CFG)
MULTI_EXP_FLOW = MULTI_EXP_CFG["flows"][0]

MULTI_EXP_FLOW.pop("destination_wallet")
MULTI_EXP_FLOW["finalization_wallet"] = "flab_stage"
MULTI_EXP_FLOW["destinations"] = {
    "mode": "percentage",
    "items": [
        {
            "type": "wallet",
            "wallet": "flab_dest",
            "percent_bps": 5000,
        },
        {
            "type": "address",
            "address": EXTERNAL,
            "percent_bps": 5000,
        },
    ],
}

MULTI_EXP_CFG["finalization"] = {
    "mode": "consolidate_then_distribute",
}


SETTLEMENT_CYCLE_MIXED_CFG = compile_play(
    "settlement_cycle",
    {
        "source_wallet": "flab_source",
        "allocation_wallet": "flab_stage",
        "workers": ["flab_a", "flab_b"],
        "hubs": [],
        "allocation_sats": 500_000_000,
        "outbound_decisions": 2,
        "return_decisions": 2,
        "amount_sats_min": 25_000_000,
        "amount_sats_max": 75_000_000,
        "delay_seconds_min": 0,
        "delay_seconds_max": 0,
        "settlement_delay_seconds_min": 0,
        "settlement_delay_seconds_max": 0,
        "reserve_return_delay_seconds_min": 0,
        "reserve_return_delay_seconds_max": 0,
        "confirmations_required": 2,
        "seed": 105,
        "max_total_transactions": 30,
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
                    "address": EXTERNAL,
                    "amount_sats": 20_000_000,
                },
            ],
        },
    },
)


SETTLEMENT_CYCLE_CFG = compile_play(
    "settlement_cycle",
    {
        "source_wallet": "flab_source",
        "allocation_wallet": "flab_stage",
        "workers": ["flab_a", "flab_b"],
        "hubs": [],
        "allocation_sats": 500_000_000,
        "outbound_decisions": 2,
        "return_decisions": 2,
        "amount_sats_min": 25_000_000,
        "amount_sats_max": 75_000_000,
        "delay_seconds_min": 0,
        "delay_seconds_max": 0,
        "settlement_delay_seconds_min": 0,
        "settlement_delay_seconds_max": 0,
        "reserve_return_delay_seconds_min": 0,
        "reserve_return_delay_seconds_max": 0,
        "confirmations_required": 2,
        "seed": 104,
        "max_total_transactions": 30,
        "settlement": {
            "mode": "fixed",
            "items": [{
                "type": "wallet",
                "wallet": "flab_dest",
                "amount_sats": 50_000_000,
            }],
        },
    },
)


SETTLEMENT_CYCLE_SNAPSHOT_CFG = copy.deepcopy(
    SETTLEMENT_CYCLE_CFG
)
SETTLEMENT_CYCLE_SNAPSHOT_CFG[
    "utxo_policy"
]["default"]["scope"] = "snapshot_at_start"


FAN_OUT_FAN_IN_CFG = compile_play(
    "fan_out_fan_in",
    {
        "source_wallet": "flab_source",
        "allocation_wallet": "flab_stage",
        "workers": ["flab_a", "flab_b"],
        "destination_wallet": "flab_dest",
        "allocation_sats": 500_000_000,
        "amount_sats_min": 50_000_000,
        "amount_sats_max": 100_000_000,
        "delay_seconds_min": 0,
        "delay_seconds_max": 5,
        "confirmations_required": 2,
        "seed": 104,
    },
)


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


class SettlementCycleSnapshotIsolationTests(ExecBase):
    config = SETTLEMENT_CYCLE_SNAPSHOT_CFG

    def test_dirty_worker_refuses_before_start_cohort_capture(self):
        self.chain.fund(
            "flab_a",
            12_345_678,
        )
        self.chain.mine(3)

        flow = self.e.list_flows(self.exp)[0]

        self.assertIsNone(
            self.e.get_utxo_cohort(
                flow["id"],
                "snapshot_at_start",
            )
        )

        r = self.x.tick(self.exp)

        self.assertIsNotNone(r["blocked"], r)
        self.assertIn(
            "Settlement Cycle requires empty worker and hub wallets",
            r["blocked"],
        )
        self.assertIn(
            "flab_a",
            r["blocked"],
        )

        self.assertEqual(
            self.e.get_experiment(self.exp)["state"],
            "PAUSED",
        )

        self.assertEqual(
            self.e.list_jobs(flow["id"]),
            [],
        )

        self.assertEqual(
            self.sent(),
            [],
        )

        self.assertIsNone(
            self.e.get_utxo_cohort(
                flow["id"],
                "snapshot_at_start",
            ),
            "dirty Settlement Cycle must not persist an immutable "
            "experiment-start cohort",
        )


class SettlementCycleLifecycleDispatchTests(ExecBase):
    config = SETTLEMENT_CYCLE_CFG

    def test_start_enters_settlement_progressive_planning(self):
        r = self.x.tick(self.exp)

        self.assertIsNone(r["blocked"], r)

        flow = self.e.list_flows(self.exp)[0]

        self.assertEqual(
            self.e.get_flow(flow["id"])["state"],
            "PLAN",
        )

        self.assertEqual(
            self.e.list_jobs(flow["id"]),
            [],
        )

    def test_dirty_worker_refuses_before_settlement_allocation(self):
        self.chain.fund(
            "flab_a",
            12_345_678,
        )
        self.chain.mine(3)

        # START -> PLAN is still harmless; no transaction or job exists yet.
        r = self.x.tick(self.exp)
        self.assertIsNone(r["blocked"], r)

        # The first PLAN tick must refuse before creating the allocation job.
        r = self.x.tick(self.exp)

        self.assertIsNotNone(r["blocked"], r)
        self.assertIn(
            "Settlement Cycle requires empty worker and hub wallets",
            r["blocked"],
        )
        self.assertIn(
            "flab_a",
            r["blocked"],
        )

        self.assertEqual(
            self.e.get_experiment(self.exp)["state"],
            "PAUSED",
        )
        self.assertEqual(
            self.sent(),
            [],
        )

        flow = self.e.list_flows(self.exp)[0]

        self.assertEqual(
            self.e.list_jobs(flow["id"]),
            [],
        )

    def test_dirty_hub_refuses_before_settlement_allocation(self):
        cfg = compile_play(
            "settlement_cycle",
            {
                "source_wallet": "flab_source",
                "allocation_wallet": "flab_stage",
                "workers": ["flab_a", "flab_b"],
                "hubs": ["flab_hub_a"],
                "allocation_sats": 500_000_000,
                "outbound_decisions": 2,
                "return_decisions": 2,
                "amount_sats_min": 25_000_000,
                "amount_sats_max": 75_000_000,
                "delay_seconds_min": 0,
                "delay_seconds_max": 0,
                "settlement_delay_seconds_min": 0,
                "settlement_delay_seconds_max": 0,
                "reserve_return_delay_seconds_min": 0,
                "reserve_return_delay_seconds_max": 0,
                "confirmations_required": 2,
                "seed": 106,
                "max_total_transactions": 30,
                "settlement": {
                    "mode": "fixed",
                    "items": [{
                        "type": "wallet",
                        "wallet": "flab_dest",
                        "amount_sats": 50_000_000,
                    }],
                },
            },
        )

        self.e.conn.close()
        self.dir.cleanup()

        self.dir = tempfile.TemporaryDirectory()
        self.t = datetime(
            2026, 10, 5, 12, 0, 0,
            tzinfo=timezone.utc,
        )
        self.chain = FakeChain(W)
        self.chain.fund(
            "flab_source",
            3_500_000_000,
        )
        self.chain.fund(
            "flab_hub_a",
            12_345_678,
        )
        self.chain.mine(3)

        self.v = FakeVerifier()
        self.e = Engine(
            os.path.join(
                self.dir.name,
                "x.db",
            ),
            verifier=self.v,
            clock=lambda: self.t,
        )
        self.b = TxBuilder(
            self.chain,
            W,
            max_fee_sats=10_000_000,
        )
        self.x = Executor(
            self.e,
            self.chain,
            self.b,
        )

        exp = self.e.create_experiment("exec")

        self.e.approve(
            exp,
            self.e.configure_experiment(
                exp,
                cfg,
            ),
        )
        self.e.start(exp)
        self.exp = exp

        # START -> PLAN.
        r = self.x.tick(self.exp)
        self.assertIsNone(r["blocked"], r)

        # First PLAN tick must refuse before allocation.
        r = self.x.tick(self.exp)

        self.assertIsNotNone(r["blocked"], r)
        self.assertIn(
            "Settlement Cycle requires empty worker and hub wallets",
            r["blocked"],
        )
        self.assertIn(
            "flab_hub_a",
            r["blocked"],
        )

        self.assertEqual(
            self.e.get_experiment(self.exp)["state"],
            "PAUSED",
        )
        self.assertEqual(
            self.sent(),
            [],
        )

        flow = self.e.list_flows(self.exp)[0]

        self.assertEqual(
            self.e.list_jobs(flow["id"]),
            [],
        )

    def test_pending_worker_refuses_before_settlement_allocation(self):
        original = self.chain.get_balances

        def contaminated(wallet):
            result = original(wallet)

            if wallet == "flab_a":
                result = copy.deepcopy(result)
                result["mine"]["untrusted_pending"] = Decimal(
                    "0.12345678"
                )

            return result

        self.chain.get_balances = contaminated

        self.x.tick(self.exp)
        r = self.x.tick(self.exp)

        self.assertIsNotNone(r["blocked"], r)
        self.assertIn(
            "Settlement Cycle requires empty worker and hub wallets",
            r["blocked"],
        )
        self.assertIn(
            "untrusted_pending=12345678 sats",
            r["blocked"],
        )
        self.assertEqual(
            self.sent(),
            [],
        )

    def test_immature_worker_refuses_before_settlement_allocation(self):
        original = self.chain.get_balances

        def contaminated(wallet):
            result = original(wallet)

            if wallet == "flab_a":
                result = copy.deepcopy(result)
                result["mine"]["immature"] = Decimal(
                    "0.12345678"
                )

            return result

        self.chain.get_balances = contaminated

        self.x.tick(self.exp)
        r = self.x.tick(self.exp)

        self.assertIsNotNone(r["blocked"], r)
        self.assertIn(
            "Settlement Cycle requires empty worker and hub wallets",
            r["blocked"],
        )
        self.assertIn(
            "immature=12345678 sats",
            r["blocked"],
        )
        self.assertEqual(
            self.sent(),
            [],
        )

    def test_plan_generates_allocation_then_settlement_workload(self):
        flow = self.e.list_flows(self.exp)[0]

        # START -> PLAN
        r = self.x.tick(self.exp)
        self.assertIsNone(r["blocked"], r)

        # PLAN -> allocation job / EXECUTE
        r = self.x.tick(self.exp)
        self.assertIsNone(r["blocked"], r)

        jobs = self.e.list_jobs(flow["id"])
        self.assertEqual(len(jobs), 1)

        allocation = jobs[0]
        allocation_meta = json.loads(
            allocation["generated_from_json"] or "{}"
        )

        self.assertEqual(
            allocation_meta.get("phase"),
            "allocation",
        )
        self.assertEqual(
            json.loads(allocation["planned_json"])["to"],
            "flab_stage",
        )

        # Execute + confirm allocation.
        self.x.tick(self.exp)
        self.chain.mine(2)
        self.x.tick(self.exp)

        # NEXT_STATE must return to PLAN, not COMPLETE.
        r = self.x.tick(self.exp)
        self.assertIsNone(r["blocked"], r)

        self.assertEqual(
            self.e.get_flow(flow["id"])["state"],
            "PLAN",
        )

        # PLAN generates first outbound Settlement Cycle decision.
        r = self.x.tick(self.exp)
        self.assertIsNone(r["blocked"], r)

        jobs = self.e.list_jobs(flow["id"])
        self.assertEqual(len(jobs), 2)

        workload = jobs[1]
        workload_meta = json.loads(
            workload["generated_from_json"] or "{}"
        )

        self.assertEqual(
            workload_meta.get("phase"),
            "workload",
        )
        self.assertEqual(
            workload_meta.get("source"),
            "settlement cycle outbound",
        )


class SettlementCycleMixedExecutionTests(ExecBase):
    config = SETTLEMENT_CYCLE_MIXED_CFG

    def test_mixed_wallet_and_external_settlement_executes(self):
        settlement = None

        for _ in range(200):
            r = self.x.tick(self.exp)

            self.assertIsNone(
                r["blocked"],
                r,
            )

            flow = self.e.list_flows(self.exp)[0]
            matches = [
                job
                for job in self.e.list_jobs(flow["id"])
                if json.loads(
                    job["generated_from_json"] or "{}"
                ).get("phase") == "settlement"
            ]

            if matches:
                settlement = matches[0]

                if (
                    settlement["state"] == "PLANNED"
                    and self.e.get_flow(flow["id"])["state"]
                    == "EXECUTE"
                ):
                    break

            self.t += timedelta(
                seconds=r["wait_s"] or 1
            )
            self.chain.mine(2)
        else:
            self.fail(
                "mixed Settlement Cycle did not reach settlement EXECUTE"
            )

        stage_before = to_sats(
            self.chain.get_balances(
                "flab_stage"
            )["mine"]["trusted"]
        )

        r = self.x.tick(self.exp)

        self.assertIsNone(r["blocked"], r)

        settlement = self.e.get_job(settlement["id"])
        self.assertEqual(
            settlement["state"],
            "BROADCAST",
        )

        result = json.loads(
            settlement["result_json"]
        )

        self.assertEqual(
            len(result["destinations"]),
            2,
        )

        internal = next(
            item
            for item in result["destinations"]
            if item["type"] == "wallet"
        )
        external = next(
            item
            for item in result["destinations"]
            if item["type"] == "address"
        )

        self.assertEqual(
            internal["wallet"],
            "flab_dest",
        )
        self.assertEqual(
            internal["amount_sats"],
            25_000_000,
        )
        self.assertTrue(
            self.chain.get_address_info(
                "flab_dest",
                internal["resolved_address"],
            ).get("ismine")
        )

        self.assertEqual(
            external["address"],
            EXTERNAL,
        )
        self.assertEqual(
            external["resolved_address"],
            EXTERNAL,
        )
        self.assertEqual(
            external["amount_sats"],
            20_000_000,
        )

        tx = self.chain.txs[
            settlement["txid"]
        ]
        outputs = dict(tx["outputs"])

        self.assertEqual(
            outputs[internal["resolved_address"]],
            25_000_000,
        )
        self.assertEqual(
            outputs[EXTERNAL],
            20_000_000,
        )

        retained = [
            sats
            for address, sats in tx["outputs"]
            if self.chain.get_address_info(
                "flab_stage",
                address,
            ).get("ismine")
        ]

        self.assertEqual(len(retained), 1)
        self.assertEqual(
            retained[0],
            stage_before
            - 45_000_000
            - FEE,
        )


class SettlementCycleExecutionTests(ExecBase):
    config = SETTLEMENT_CYCLE_CFG

    def test_consolidation_uses_sender_actual_utxo_count_for_fee_reserve(self):
        # Drive through allocation + both outbound decisions, stopping at the
        # PLAN tick immediately before pre-settlement consolidation.
        flow = self.e.list_flows(self.exp)[0]

        for _ in range(200):
            jobs = self.e.list_jobs(flow["id"])
            workload = [
                job
                for job in jobs
                if json.loads(
                    job["generated_from_json"] or "{}"
                ).get("phase") == "workload"
            ]

            if (
                len(workload) == 2
                and all(job["state"] == "CONFIRMED" for job in workload)
                and self.e.get_flow(flow["id"])["state"] == "PLAN"
            ):
                break

            r = self.x.tick(self.exp)
            self.assertIsNone(r["blocked"], r)

            self.t += timedelta(seconds=r["wait_s"] or 1)
            self.chain.mine(2)
        else:
            self.fail(
                "Settlement Cycle did not reach pre-settlement "
                "consolidation PLAN"
            )

        # Reproduce the live failure deterministically:
        #
        #   sender balance = 500 sats
        #   actual eligible UTXOs = 1
        #   generic 8-input reserve = 1,000 sats
        #   actual 1-input reserve = 100 sats
        #
        # The consolidation must use the sender's actual eligible input count,
        # not the generic workload-planning reserve.
        def balances(_flow):
            return {
                "flab_source": 1_000_000_000,
                "flab_stage": 400_000_000,
                "flab_a": 500,
                "flab_b": 0,
            }

        self.x._confirmed_balances = balances

        original_list_unspent = self.chain.list_unspent

        def one_input(wallet, minconf=0):
            if wallet == "flab_a":
                return [{
                    "txid": "11" * 32,
                    "vout": 0,
                    "amount": Decimal("0.00000500"),
                    "confirmations": 100,
                    "spendable": True,
                    "safe": True,
                }]

            return original_list_unspent(wallet, minconf)

        self.chain.list_unspent = one_input

        calls = []

        def reserve_for_inputs(n_inputs=8):
            calls.append(n_inputs)
            return 100 if n_inputs == 1 else 1_000

        self.b.planning_fee_reserve_sats = reserve_for_inputs

        r = self.x.tick(self.exp)

        self.assertIsNone(r["blocked"], r)

        jobs = self.e.list_jobs(flow["id"])
        consolidation = [
            job
            for job in jobs
            if json.loads(
                job["generated_from_json"] or "{}"
            ).get("phase") == "consolidation"
        ]

        self.assertEqual(len(consolidation), 1)

        plan = json.loads(consolidation[0]["planned_json"])

        self.assertEqual(plan["from"], "flab_a")
        self.assertEqual(plan["to"], "flab_stage")
        self.assertEqual(plan["amount_sats"], "all")

        generated = json.loads(
            consolidation[0]["generated_from_json"] or "{}"
        )

        self.assertEqual(generated["fee_reserve_sats"], 100)
        self.assertIn(1, calls)

    def _drive_to_settlement_execute(self):
        settlement = None

        for _ in range(200):
            r = self.x.tick(self.exp)

            self.assertIsNone(
                r["blocked"],
                r,
            )

            flow = self.e.list_flows(self.exp)[0]
            jobs = self.e.list_jobs(flow["id"])

            matches = [
                job
                for job in jobs
                if json.loads(
                    job["generated_from_json"] or "{}"
                ).get("phase") == "settlement"
            ]

            if matches:
                settlement = matches[0]

                if (
                    settlement["state"] == "PLANNED"
                    and self.e.get_flow(flow["id"])["state"]
                    == "EXECUTE"
                ):
                    return flow, settlement

            self.t += timedelta(
                seconds=r["wait_s"] or 1
            )
            self.chain.mine(2)

        self.fail(
            "Settlement Cycle did not reach settlement EXECUTE"
        )

    def test_intermediate_settlement_builds_partial_distribution(self):
        flow, settlement = self._drive_to_settlement_execute()

        plan = json.loads(settlement["planned_json"])

        self.assertEqual(
            plan["settlement"],
            SETTLEMENT_CYCLE_CFG["settlement_cycle"]["settlement"],
        )
        self.assertEqual(plan["from"], "flab_stage")
        self.assertEqual(plan["amount_sats"], "all")
        self.assertNotIn("to", plan)

        stage_before = to_sats(
            self.chain.get_balances(
                "flab_stage"
            )["mine"]["trusted"]
        )

        r = self.x.tick(self.exp)

        self.assertIsNone(r["blocked"], r)

        settlement = self.e.get_job(settlement["id"])

        self.assertEqual(
            settlement["state"],
            "BROADCAST",
        )

        result = json.loads(
            settlement["result_json"]
        )

        self.assertEqual(
            len(result["destinations"]),
            1,
        )

        payout = result["destinations"][0]

        self.assertEqual(
            payout["type"],
            "wallet",
        )
        self.assertEqual(
            payout["wallet"],
            "flab_dest",
        )
        self.assertTrue(
            payout["resolved_address"],
        )
        self.assertEqual(
            payout["amount_sats"],
            50_000_000,
        )

        self.assertTrue(
            self.chain.get_address_info(
                "flab_dest",
                payout["resolved_address"],
            ).get("ismine")
        )

        tx = self.chain.txs[settlement["txid"]]
        outputs = dict(tx["outputs"])

        self.assertEqual(
            outputs[payout["resolved_address"]],
            50_000_000,
        )

        retained = [
            sats
            for address, sats in tx["outputs"]
            if self.chain.get_address_info(
                "flab_stage",
                address,
            ).get("ismine")
        ]

        self.assertEqual(len(retained), 1)

        self.assertEqual(
            retained[0],
            stage_before
            - 50_000_000
            - FEE,
        )

    def test_full_settlement_cycle_completes_and_reconciles(self):
        r = self.run_all(limit=400)

        self.assertTrue(r["done"], r)
        self.assertIsNone(r["blocked"], r)

        exp = self.e.get_experiment(self.exp)

        self.assertEqual(
            exp["state"],
            "COMPLETE",
        )

        flow = self.e.list_flows(self.exp)[0]
        jobs = self.e.list_jobs(flow["id"])

        self.assertTrue(jobs)
        self.assertTrue(
            all(job["state"] == "CONFIRMED" for job in jobs),
            jobs,
        )

        phases = [
            json.loads(
                job["generated_from_json"] or "{}"
            ).get("phase")
            for job in jobs
        ]

        self.assertEqual(phases[0], "allocation")
        self.assertEqual(phases[-1], "reserve_return")

        self.assertEqual(
            phases.count("workload"),
            SETTLEMENT_CYCLE_CFG[
                "settlement_cycle"
            ]["outbound"]["decisions"],
        )
        self.assertEqual(
            phases.count("settlement"),
            1,
        )
        self.assertEqual(
            phases.count("return_workload"),
            SETTLEMENT_CYCLE_CFG[
                "settlement_cycle"
            ]["return"]["decisions"],
        )
        self.assertEqual(
            phases.count("reserve_return"),
            1,
        )

        stats = json.loads(exp["stats_json"])

        self.assertEqual(
            stats["issues"],
            [],
        )

        final = json.loads(
            exp["final_state_json"]
        )["balances_sats"]

        self.assertEqual(
            final["flab_stage"],
            0,
        )
        self.assertEqual(
            final["flab_a"],
            0,
        )
        self.assertEqual(
            final["flab_b"],
            0,
        )

        # Fixed intermediate settlement payout.
        self.assertEqual(
            final["flab_dest"],
            50_000_000,
        )

    def test_settlement_reconciliation_catches_wrong_payout_output(self):
        r = self.run_all(limit=400)

        self.assertTrue(r["done"], r)
        self.assertIsNone(r["blocked"], r)

        flow = self.e.list_flows(self.exp)[0]

        settlement = next(
            job
            for job in self.e.list_jobs(flow["id"])
            if json.loads(
                job["generated_from_json"] or "{}"
            ).get("phase") == "settlement"
        )

        recorded = json.loads(
            settlement["result_json"]
        )

        payout_address = recorded[
            "destinations"
        ][0]["resolved_address"]

        real = self.chain.decode_raw_transaction

        def liar(txhex):
            result = copy.deepcopy(real(txhex))

            for output in result["vout"]:
                if (
                    output["scriptPubKey"].get("address")
                    == payout_address
                ):
                    output["value"] -= Decimal(
                        "0.00000001"
                    )
                    break

            return result

        self.chain.decode_raw_transaction = liar

        ok, _, stats = self.x.reconcile(
            self.exp
        )

        self.assertFalse(ok)
        self.assertTrue(
            any(
                "settlement payout" in issue
                for issue in stats["issues"]
            ),
            stats,
        )

    def test_settlement_reconciliation_catches_wrong_retained_output(self):
        r = self.run_all(limit=400)

        self.assertTrue(r["done"], r)
        self.assertIsNone(r["blocked"], r)

        flow = self.e.list_flows(self.exp)[0]

        settlement = next(
            job
            for job in self.e.list_jobs(flow["id"])
            if json.loads(
                job["generated_from_json"] or "{}"
            ).get("phase") == "settlement"
        )

        recorded = json.loads(
            settlement["result_json"]
        )

        payout_addresses = {
            item["resolved_address"]
            for item in recorded["destinations"]
        }

        real = self.chain.decode_raw_transaction

        def liar(txhex):
            result = copy.deepcopy(real(txhex))

            for output in result["vout"]:
                address = output[
                    "scriptPubKey"
                ].get("address")

                if address not in payout_addresses:
                    output["value"] -= Decimal(
                        "0.00000001"
                    )
                    break

            return result

        self.chain.decode_raw_transaction = liar

        ok, _, stats = self.x.reconcile(
            self.exp
        )

        self.assertFalse(ok)
        self.assertTrue(
            any(
                "retained Stage output" in issue
                for issue in stats["issues"]
            ),
            stats,
        )

    def test_settlement_execution_refuses_mutated_persisted_contract(self):
        flow, settlement = self._drive_to_settlement_execute()

        plan = json.loads(settlement["planned_json"])
        plan["settlement"]["items"][0]["amount_sats"] += 1

        self.e.conn.execute(
            "UPDATE jobs SET planned_json=? WHERE id=?",
            (
                json.dumps(
                    plan,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                settlement["id"],
            ),
        )
        self.e.conn.commit()

        r = self.x.tick(self.exp)

        self.assertIsNotNone(
            r["blocked"],
            r,
        )
        self.assertIn(
            "settlement",
            r["blocked"].lower(),
        )
        self.assertIn(
            "approved",
            r["blocked"].lower(),
        )


class FanOutFanInRunTests(ExecBase):
    config = FAN_OUT_FAN_IN_CFG

    def test_nonempty_stage_is_preserved_and_only_experiment_value_settles(self):
        stage_baseline = 275_000_000

        self.chain.fund(
            "flab_stage",
            stage_baseline,
        )
        self.chain.mine(3)

        r = self.run_all(limit=300)

        self.assertTrue(r["done"], r)
        self.assertIsNone(r["blocked"], r)

        exp = self.e.get_experiment(self.exp)
        self.assertEqual(exp["state"], "COMPLETE")

        flow = self.e.list_flows(self.exp)[0]
        jobs = self.e.list_jobs(flow["id"])

        # Two-worker lifecycle:
        # allocation + 2 fan-out + 2 fan-in + finalization.
        self.assertEqual(len(jobs), 6)
        self.assertEqual(len(self.sent()), 6)

        phases = [
            json.loads(
                job["generated_from_json"] or "{}"
            ).get("phase")
            for job in jobs
        ]

        self.assertEqual(
            phases,
            [
                "allocation",
                "workload",
                "workload",
                "consolidation",
                "consolidation",
                "finalization",
            ],
        )

        final = json.loads(
            exp["final_state_json"]
        )["balances_sats"]

        # Pre-existing Stage value is not part of the Play principal and must
        # remain in Stage after terminal settlement.
        self.assertEqual(
            final["flab_stage"],
            stage_baseline,
        )

        # Branch wallets are isolated at the beginning and swept back to Stage.
        self.assertEqual(final["flab_a"], 0)
        self.assertEqual(final["flab_b"], 0)

        # The reserve pays the commitment fee outside experiment principal.
        self.assertEqual(
            final["flab_source"],
            3_500_000_000
            - FAN_OUT_FAN_IN_CFG["flows"][0]["allocation_sats"]
            - FEE,
        )

        # Principal pays:
        #   2 fan-out fees
        #   2 fan-in fees
        #   1 terminal finalization fee
        allocation = FAN_OUT_FAN_IN_CFG["flows"][0][
            "allocation_sats"
        ]

        self.assertEqual(
            final["flab_dest"],
            allocation - (5 * FEE),
        )

        terminal = jobs[-1]
        terminal_meta = json.loads(
            terminal["generated_from_json"] or "{}"
        )
        terminal_result = json.loads(
            terminal["result_json"] or "{}"
        )

        self.assertEqual(
            terminal_meta["stage_baseline_sats"],
            stage_baseline,
        )
        self.assertEqual(
            terminal_meta["stage_wallet"],
            "flab_stage",
        )
        self.assertEqual(
            terminal_result["amount_sats"],
            allocation - (5 * FEE),
        )

    def test_dirty_worker_refuses_before_first_transaction(self):
        self.chain.fund(
            "flab_a",
            12_345_678,
        )
        self.chain.mine(3)

        r = self.x.tick(self.exp)

        self.assertIsNotNone(r["blocked"], r)
        self.assertIn(
            "Fan-Out/Fan-In requires empty worker wallets",
            r["blocked"],
        )
        self.assertIn(
            "flab_a",
            r["blocked"],
        )

        self.assertEqual(
            self.e.get_experiment(self.exp)["state"],
            "PAUSED",
        )
        self.assertEqual(self.sent(), [])

        flow = self.e.list_flows(self.exp)[0]
        self.assertEqual(
            self.e.list_jobs(flow["id"]),
            [],
        )


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

    def test_fee_limit_build_refusal_backs_off_and_retries(self):
        # START -> PLAN and make the first scheduled job due.
        self.x.tick(self.exp)
        self.t += timedelta(seconds=30)
        self.x.tick(self.exp)

        flow = self.e.list_flows(self.exp)[0]
        self.assertEqual(
            self.e.get_flow(flow["id"])["state"],
            "EXECUTE",
        )

        original_build = self.b.build
        calls = []

        def high_fee_once(*args, **kwargs):
            calls.append(1)

            if len(calls) == 1:
                raise FeeLimitExceeded(
                    "fee 105833520 sats exceeds the configured maximum "
                    "10000000 sats"
                )

            return original_build(*args, **kwargs)

        self.b.build = high_fee_once

        # First build sees an excessive fee. It must NOT pause and must
        # NOT broadcast. The same PLANNED job remains available to retry.
        r = self.x.tick(self.exp)

        self.assertIsNone(r["blocked"], r)
        self.assertEqual(r["wait_s"], POLL_SECONDS)
        self.assertEqual(
            self.e.get_experiment(self.exp)["state"],
            "RUNNING",
        )

        jobs = self.e.list_jobs(flow["id"])

        self.assertEqual(jobs[0]["state"], "PLANNED")
        self.assertEqual(self.sent(), [])

        # After the retry interval, rebuild from scratch. When the builder
        # reports an acceptable fee, normal execution continues.
        self.t += timedelta(seconds=POLL_SECONDS)

        r = self.x.tick(self.exp)

        self.assertIsNone(r["blocked"], r)
        self.assertEqual(
            self.e.get_job(jobs[0]["id"])["state"],
            "BROADCAST",
        )
        self.assertEqual(len(self.sent()), 1)
        self.assertEqual(len(calls), 2)

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


class ExternalDestinationRunTests(ExecBase):
    config = EXTERNAL_CFG

    def test_deterministic_run_completes_to_external_address(self):
        r = self.run_all()

        self.assertTrue(r["done"], r)
        self.assertEqual(
            self.e.get_experiment(self.exp)["state"],
            "COMPLETE",
        )

        flow = self.e.list_flows(self.exp)[0]
        self.assertEqual(flow["destination_wallet"], EXTERNAL)

        jobs = self.e.list_jobs(flow["id"])
        final = jobs[-1]
        plan = json.loads(final["planned_json"])
        recorded = json.loads(final["result_json"])

        self.assertEqual(plan["to"], EXTERNAL)
        self.assertEqual(recorded["address"], EXTERNAL)

        tx = self.chain.txs[final["txid"]]
        external_outputs = [
            sats
            for address, sats in tx["outputs"]
            if address == EXTERNAL
        ]

        self.assertEqual(external_outputs, [100_000_000])

        final_state = json.loads(
            self.e.get_experiment(self.exp)["final_state_json"]
        )["balances_sats"]

        self.assertNotIn(EXTERNAL, final_state)
        self.assertIn("flab_source", final_state)
        self.assertIn("flab_a", final_state)
        self.assertIn("flab_b", final_state)

    def test_external_reconciliation_catches_wrong_output_amount(self):
        # First prove the transaction can be built, broadcast, confirmed, and
        # reconciled normally. Then corrupt only the reconciliation view.
        r = self.run_all()

        self.assertTrue(r["done"], r)
        self.assertEqual(
            self.e.get_experiment(self.exp)["state"],
            "COMPLETE",
        )

        real = self.chain.decode_raw_transaction

        def liar(txhex):
            result = copy.deepcopy(real(txhex))
            for output in result["vout"]:
                if output["scriptPubKey"].get("address") == EXTERNAL:
                    output["value"] -= Decimal("0.00000001")
                    break
            return result

        self.chain.decode_raw_transaction = liar

        ok, _, stats = self.x.reconcile(self.exp)

        self.assertFalse(ok)
        self.assertTrue(
            any(
                "external output was" in issue
                for issue in stats["issues"]
            ),
            stats,
        )


class ExternalExperimentalRunTests(ExecBase):
    config = EXTERNAL_EXP_CFG

    def test_experimental_finalization_sweeps_to_external_address(self):
        r = self.run_all()

        self.assertTrue(r["done"], r)
        self.assertEqual(
            self.e.get_experiment(self.exp)["state"],
            "COMPLETE",
        )

        flow = self.e.list_flows(self.exp)[0]
        jobs = self.e.list_jobs(flow["id"])

        workload = []
        finalizations = []

        for job in jobs:
            meta = json.loads(job["generated_from_json"] or "{}")
            if meta.get("source") == "seeded experimental generator":
                workload.append(job)
            if meta.get("source") == "experimental finalization":
                finalizations.append(job)

        self.assertEqual(len(workload), EXTERNAL_EXP_CFG["workload"]["jobs"])
        self.assertGreaterEqual(len(finalizations), 1)

        for job in workload:
            plan = json.loads(job["planned_json"])
            self.assertNotEqual(plan["from"], EXTERNAL)
            self.assertNotEqual(plan["to"], EXTERNAL)

        for job in finalizations:
            plan = json.loads(job["planned_json"])
            recorded = json.loads(job["result_json"])

            self.assertEqual(plan["to"], EXTERNAL)
            self.assertEqual(plan["amount_sats"], "all")
            self.assertEqual(recorded["address"], EXTERNAL)

            tx = self.chain.txs[job["txid"]]
            self.assertTrue(
                any(address == EXTERNAL for address, _ in tx["outputs"])
            )

        final_state = json.loads(
            self.e.get_experiment(self.exp)["final_state_json"]
        )["balances_sats"]

        self.assertNotIn(EXTERNAL, final_state)

        for worker in json.loads(flow["flow_wallets_json"]):
            self.assertEqual(
                self.chain.get_balances(worker)["mine"]["trusted"],
                0,
            )


class MultiDestinationExecutionTests(ExecBase):
    config = MULTI_EXP_CFG

    def test_terminal_distribution_builds_journals_broadcasts_and_confirms(self):
        terminal = None

        for _ in range(300):
            r = self.x.tick(self.exp)

            self.assertIsNone(
                r["blocked"],
                r,
            )

            flow = self.e.list_flows(self.exp)[0]
            jobs = self.e.list_jobs(flow["id"])

            terminal_jobs = [
                job for job in jobs
                if json.loads(
                    job["generated_from_json"] or "{}"
                ).get("source")
                == "experimental terminal distribution"
            ]

            if terminal_jobs:
                terminal = terminal_jobs[0]

                if terminal["state"] == "CONFIRMED":
                    break

            self.t += timedelta(
                seconds=r["wait_s"] or 1
            )
            self.chain.mine(2)

        else:
            self.fail(
                "terminal distribution did not reach CONFIRMED"
            )

        terminal = self.e.get_job(terminal["id"])
        plan = json.loads(terminal["planned_json"])
        result = json.loads(terminal["result_json"])

        self.assertEqual(
            terminal["state"],
            "CONFIRMED",
        )
        self.assertEqual(
            plan["from"],
            "flab_stage",
        )
        self.assertEqual(
            plan["amount_sats"],
            "all",
        )
        self.assertNotIn(
            "to",
            plan,
        )
        self.assertEqual(
            plan["distribution"],
            MULTI_EXP_FLOW["destinations"],
        )

        self.assertEqual(
            result["distributed_sats"],
            result["budget_sats"] - result["fee_sats"],
        )
        self.assertEqual(
            len(result["destinations"]),
            2,
        )

        internal = next(
            item
            for item in result["destinations"]
            if item["type"] == "wallet"
        )
        external = next(
            item
            for item in result["destinations"]
            if item["type"] == "address"
        )

        self.assertEqual(
            internal["wallet"],
            "flab_dest",
        )
        self.assertTrue(
            internal["resolved_address"],
        )

        self.assertTrue(
            self.chain.get_address_info(
                "flab_dest",
                internal["resolved_address"],
            ).get("ismine")
        )

        self.assertEqual(
            external["address"],
            EXTERNAL,
        )
        self.assertEqual(
            external["resolved_address"],
            EXTERNAL,
        )

        tx = self.chain.txs[terminal["txid"]]
        tx_outputs = dict(tx["outputs"])

        self.assertEqual(
            tx_outputs[internal["resolved_address"]],
            internal["amount_sats"],
        )
        self.assertEqual(
            tx_outputs[EXTERNAL],
            external["amount_sats"],
        )

        # Percentage rounding may differ by one satoshi, but the
        # complete net amount must be distributed exactly.
        self.assertEqual(
            internal["amount_sats"]
            + external["amount_sats"],
            result["distributed_sats"],
        )
        self.assertLessEqual(
            abs(
                internal["amount_sats"]
                - external["amount_sats"]
            ),
            1,
        )

        journal = self.e.conn.execute(
            """
            SELECT payload_json, status, result_json
            FROM action_journal
            WHERE job_id=? AND kind='broadcast'
            """,
            (terminal["id"],),
        ).fetchone()

        self.assertIsNotNone(journal)
        self.assertEqual(
            journal["status"],
            "done",
        )

        payload = json.loads(journal["payload_json"])
        journal_result = json.loads(journal["result_json"])

        self.assertEqual(
            payload["expected_txid"],
            terminal["txid"],
        )
        self.assertEqual(
            payload["destinations"],
            result["destinations"],
        )
        self.assertEqual(
            journal_result["destinations"],
            result["destinations"],
        )

        # Stop deliberately in NEXT_STATE. Reconciliation support for
        # terminal multi-output jobs is the next layer.
        self.assertEqual(
            self.e.get_flow(flow["id"])["state"],
            "NEXT_STATE",
        )
        self.assertEqual(
            self.e.get_experiment(self.exp)["state"],
            "RUNNING",
        )


class MultiDestinationReconciliationTests(ExecBase):
    config = MULTI_EXP_CFG

    def test_full_multi_destination_run_reconciles_and_completes(self):
        r = self.run_all(limit=400)

        self.assertTrue(r["done"], r)
        self.assertIsNone(r["blocked"], r)

        exp = self.e.get_experiment(self.exp)

        self.assertEqual(
            exp["state"],
            "COMPLETE",
        )

        stats = json.loads(exp["stats_json"])

        self.assertEqual(
            stats["issues"],
            [],
        )

        final = json.loads(
            exp["final_state_json"]
        )["balances_sats"]

        self.assertEqual(
            final["flab_stage"],
            0,
        )
        self.assertEqual(
            final["flab_a"],
            0,
        )
        self.assertEqual(
            final["flab_b"],
            0,
        )

        self.assertIn(
            "flab_dest",
            final,
        )
        self.assertGreater(
            final["flab_dest"],
            0,
        )

        self.assertNotIn(
            EXTERNAL,
            final,
        )

        flow = self.e.list_flows(self.exp)[0]

        self.assertNotIn(
            flow["destination_wallet"],
            final,
        )

        terminal = [
            job
            for job in self.e.list_jobs(flow["id"])
            if json.loads(
                job["generated_from_json"] or "{}"
            ).get("source")
            == "experimental terminal distribution"
        ]

        self.assertEqual(
            len(terminal),
            1,
        )

        result = json.loads(
            terminal[0]["result_json"]
        )

        self.assertEqual(
            sum(
                item["amount_sats"]
                for item in result["destinations"]
            ),
            result["distributed_sats"],
        )

    def test_terminal_reconciliation_catches_wrong_output(self):
        # Drive through terminal confirmation but stop before the
        # following NEXT_STATE tick triggers experiment reconciliation.
        terminal = None

        for _ in range(300):
            r = self.x.tick(self.exp)

            self.assertIsNone(
                r["blocked"],
                r,
            )

            flow = self.e.list_flows(self.exp)[0]

            matches = [
                job
                for job in self.e.list_jobs(flow["id"])
                if json.loads(
                    job["generated_from_json"] or "{}"
                ).get("source")
                == "experimental terminal distribution"
            ]

            if matches:
                terminal = matches[0]

                if terminal["state"] == "CONFIRMED":
                    break

            self.t += timedelta(
                seconds=r["wait_s"] or 1
            )
            self.chain.mine(2)

        else:
            self.fail(
                "terminal distribution did not confirm"
            )

        real = self.chain.decode_raw_transaction

        result = json.loads(
            self.e.get_job(
                terminal["id"]
            )["result_json"]
        )

        target = result["destinations"][0][
            "resolved_address"
        ]

        def liar(txhex):
            decoded = copy.deepcopy(real(txhex))

            for output in decoded["vout"]:
                if (
                    output["scriptPubKey"].get(
                        "address"
                    )
                    == target
                ):
                    output["value"] -= Decimal(
                        "0.00000001"
                    )
                    break

            return decoded

        self.chain.decode_raw_transaction = liar

        # The completion tick also runs reconciliation and must
        # fail closed immediately.
        r = self.x.tick(self.exp)

        self.assertTrue(r["done"], r)
        self.assertTrue(
            any(
                "RECONCILIATION FAILED"
                in action
                for action in r["actions"]
            ),
            r,
        )

        self.assertEqual(
            self.e.get_experiment(
                self.exp
            )["state"],
            "ERROR",
        )

        self.assertTrue(
            any(
                "terminal transaction outputs differ"
                in action
                for action in r["actions"]
            ),
            r,
        )


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

    def test_unsweepable_positive_worker_balance_pauses_finalization(self):
        # Drive only the approved randomized workload to completion.
        for _ in range(200):
            r = self.x.tick(self.exp)

            flow = self.e.list_flows(self.exp)[0]
            jobs = self.e.list_jobs(flow["id"])
            workload = [
                j for j in jobs
                if json.loads(j["generated_from_json"] or "{}").get("source")
                == "seeded experimental generator"
            ]

            if (
                len(workload) == EXP_CFG["workload"]["jobs"]
                and all(j["state"] == "CONFIRMED" for j in workload)
            ):
                break

            self.assertFalse(r["done"], r)
            self.assertIsNone(r["blocked"], r)
            self.t += timedelta(seconds=r["wait_s"] or 1)
            self.chain.mine(2)
        else:
            self.fail("experimental workload did not finish")

        # Simulate a positive worker remainder that is too small to cover the
        # approved planning fee reserve. The source balance is irrelevant here.
        reserve = self.b.planning_fee_reserve_sats()
        self.assertGreater(reserve, 1)

        def tiny_worker_balances(flow):
            return {
                flow["source_wallet"]: 1_000_000_000,
                "flab_a": reserve - 1,
                "flab_b": 0,
            }

        self.x._confirmed_balances = tiny_worker_balances

        # NEXT_STATE must see that a worker is not empty and return to PLAN.
        r = self.x.tick(self.exp)
        self.assertFalse(r["done"], r)
        self.assertIsNone(r["blocked"], r)

        # PLAN must refuse to silently complete or attempt an invalid sweep.
        r = self.x.tick(self.exp)

        self.assertIsNotNone(r["blocked"], r)
        self.assertIn("finalization fee reserve", r["blocked"])
        self.assertEqual(
            self.e.get_experiment(self.exp)["state"],
            "PAUSED",
        )

        flow = self.e.list_flows(self.exp)[0]
        finalizations = [
            j for j in self.e.list_jobs(flow["id"])
            if json.loads(j["generated_from_json"] or "{}").get("source")
            == "experimental finalization"
        ]
        self.assertEqual(finalizations, [])

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


class StagedAllocationRunTests(ExecBase):
    config = STAGED_EXP_CFG

    def test_allocation_refuses_nonempty_trusted_stage(self):
        self.chain.fund("flab_stage", 12_345_678)

        self.x.tick(self.exp)  # START -> PLAN
        r = self.x.tick(self.exp)

        self.assertIn(
            "allocation wallet flab_stage must be empty before commitment",
            r["blocked"],
        )
        self.assertIn("trusted=12345678 sats", r["blocked"])
        self.assertEqual(self.e.get_experiment(self.exp)["state"], "PAUSED")
        self.assertEqual(self.sent(), [])

    def test_allocation_refuses_untrusted_pending_stage(self):
        original = self.chain.get_balances

        def contaminated(wallet):
            result = original(wallet)
            if wallet == "flab_stage":
                result = copy.deepcopy(result)
                result["mine"]["untrusted_pending"] = Decimal("0.12345678")
            return result

        self.chain.get_balances = contaminated

        self.x.tick(self.exp)  # START -> PLAN
        r = self.x.tick(self.exp)

        self.assertIn(
            "allocation wallet flab_stage must be empty before commitment",
            r["blocked"],
        )
        self.assertIn("untrusted_pending=12345678 sats", r["blocked"])
        self.assertEqual(self.e.get_experiment(self.exp)["state"], "PAUSED")
        self.assertEqual(self.sent(), [])

    def test_allocation_refuses_immature_stage(self):
        original = self.chain.get_balances

        def contaminated(wallet):
            result = original(wallet)
            if wallet == "flab_stage":
                result = copy.deepcopy(result)
                result["mine"]["immature"] = Decimal("0.12345678")
            return result

        self.chain.get_balances = contaminated

        self.x.tick(self.exp)  # START -> PLAN
        r = self.x.tick(self.exp)

        self.assertIn(
            "allocation wallet flab_stage must be empty before commitment",
            r["blocked"],
        )
        self.assertIn("immature=12345678 sats", r["blocked"])
        self.assertEqual(self.e.get_experiment(self.exp)["state"], "PAUSED")
        self.assertEqual(self.sent(), [])

    def test_full_allocation_is_committed_before_randomized_work(self):
        allocation = STAGED_EXP_CFG["flows"][0]["allocation_sats"]

        r = self.run_all()
        self.assertTrue(r["done"], r)
        self.assertEqual(self.e.get_experiment(self.exp)["state"], "COMPLETE")

        flow = self.e.list_flows(self.exp)[0]
        jobs = self.e.list_jobs(flow["id"])

        commits = [
            j for j in jobs
            if json.loads(j["generated_from_json"] or "{}").get("source")
            == "experimental allocation commit"
        ]
        self.assertEqual(len(commits), 1)

        commit = commits[0]
        plan = json.loads(commit["planned_json"])
        result = json.loads(commit["result_json"])

        self.assertEqual(plan["from"], "flab_source")
        self.assertEqual(plan["to"], "flab_stage")
        self.assertEqual(plan["amount_sats"], allocation)
        self.assertEqual(plan["step"], 0)

        # The stage receives the full approved principal.  The reserve pays
        # the commitment transaction fee separately.
        self.assertEqual(result["amount_sats"], allocation)

        # Every later transaction is paid from experiment-owned principal.
        exp = self.e.get_experiment(self.exp)
        stats = json.loads(exp["stats_json"])
        experiment_fees = stats["total_fees_sats"] - result["fee_sats"]

        final = json.loads(exp["final_state_json"])["balances_sats"]

        self.assertEqual(final["flab_stage"], 0)
        self.assertEqual(final["flab_a"], 0)
        self.assertEqual(final["flab_b"], 0)
        self.assertEqual(
            final["flab_dest"],
            allocation - experiment_fees,
        )


class MultiDestinationNodeVerifierIntegration(ExecBase):
    config = MULTI_EXP_CFG
    use_node_verifier = True

    def test_full_multi_destination_run_with_real_verifier(self):
        r = self.run_all(limit=400)

        self.assertTrue(r["done"], r)
        self.assertIsNone(r["blocked"], r)
        self.assertEqual(
            self.e.get_experiment(self.exp)["state"],
            "COMPLETE",
        )


class NodeVerifierIntegration(ExecBase):
    use_node_verifier = True

    def test_full_run_with_the_real_verifier(self):
        self.assertTrue(self.run_all()["done"])
        self.assertEqual(self.e.get_experiment(self.exp)["state"], "COMPLETE")

    def test_non_allowlisted_wallet_loaded_blocks_the_run(self):
        self.chain.wallets.append("pool")
        r = self.x.tick(self.exp)
        self.assertIn("verification failed", r["blocked"])
        self.assertEqual(self.e.get_experiment(self.exp)["state"], "PAUSED")
        self.assertEqual(self.sent(), [])


class FeeReserveTests(unittest.TestCase):
    def test_reserve_is_small_when_node_reports_smart_fee(self):
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

        def nope(blocks=6):
            raise RpcError("no")

        c.estimate_smart_fee = nope
        b = TxBuilder(c, W, max_fee_sats=10_000_000)
        with self.assertRaises(BuildError):
            b.build("flab_source", c.get_new_address("flab_a"), 95_000_000)


if __name__ == "__main__":
    unittest.main()
