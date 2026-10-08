import copy
import hashlib
import json
import os
import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from flowlab import ConfigError, Engine
from flowlab.config_schema import validate
from flowlab.planner import (
    PlanError,
    experimental_decision,
    generate_experimental_finalization_job,
    generate_experimental_job,
    generate_jobs,
    next_due,
    generate_settlement_cycle_phase_job,
    settlement_cycle_decision,
    settlement_cycle_next_phase,
    settlement_cycle_progress,
)
from flowlab.plays import compile_play
from tests.test_engine import CFG, FakeVerifier

T = [
    {"from": "w1_source", "to": "w2_flowA", "amount_sats": 200_000_000, "delay_seconds": 30},
    {"from": "w2_flowA", "to": "w3_flowB", "amount_sats": 199_000_000, "delay_seconds": 60},
    {"from": "w3_flowB", "to": "w4_dest", "amount_sats": 198_000_000, "delay_seconds": 0},
]


def cfg_with(transfers):
    c = copy.deepcopy(CFG)
    if transfers is not None:
        c["flows"][0]["transfers"] = copy.deepcopy(transfers)
    return c


class PlannerBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.t = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)
        self.e = Engine(os.path.join(self.dir.name, "p.db"), verifier=FakeVerifier(),
                        clock=lambda: self.t)

    def tearDown(self):
        self.e.conn.close()
        self.dir.cleanup()

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)

    def planned(self, transfers=T):
        exp = self.e.create_experiment("plan")
        self.e.approve(exp, self.e.configure_experiment(exp, cfg_with(transfers)))
        self.e.start(exp)
        flow = self.e.list_flows(exp)[0]["id"]
        self.e.advance_flow(flow, "PLAN")
        return exp, flow

    def confirm(self, flow, job, n):
        self.e.advance_flow(flow, "EXECUTE")
        act = self.e.begin_action(job, "broadcast", {"amount_sats": 1})
        txid = f"{n:02x}" * 32
        self.e.complete_action(act, {"txid": txid})
        self.e.advance_flow(flow, "CONFIRMATION")
        self.e.record_confirmation(txid, 2, 100 + n)
        self.e.advance_flow(flow, "NEXT_STATE")
        self.e.advance_flow(flow, "PLAN")


class SchemaTests(PlannerBase):
    def bad(self, transfers):
        exp = self.e.create_experiment()
        with self.assertRaises(ConfigError):
            self.e.configure_experiment(exp, cfg_with(transfers))

    def test_good_config_accepted(self):
        exp = self.e.create_experiment()
        self.e.configure_experiment(exp, cfg_with(T))

    def test_external_destination_schema_is_explicit_and_terminal(self):
        cfg = cfg_with(T)
        flow = cfg["flows"][0]

        external = "D_EXTERNAL_TEST_ADDRESS"
        flow["destination_address"] = external
        del flow["destination_wallet"]
        flow["transfers"][-1]["to"] = external

        validated = validate(cfg)

        self.assertEqual(
            validated["flows"][0]["destination_address"],
            external,
        )
        self.assertNotIn(
            "destination_wallet",
            validated["flows"][0],
        )

    def test_destination_wallet_and_address_are_mutually_exclusive(self):
        cfg = cfg_with(T)
        cfg["flows"][0]["destination_address"] = "D_EXTERNAL_TEST_ADDRESS"

        with self.assertRaisesRegex(
            ConfigError,
            "exactly one destination definition",
        ):
            validate(cfg)

    def test_external_destination_can_only_be_the_final_sink(self):
        cfg = cfg_with(T)
        flow = cfg["flows"][0]

        external = "D_EXTERNAL_TEST_ADDRESS"
        flow["destination_address"] = external
        del flow["destination_wallet"]

        flow["transfers"][0]["to"] = external

        with self.assertRaises(ConfigError):
            validate(cfg)

    def test_rejections(self):
        self.bad([])
        self.bad([dict(T[0], to="w9_unknown")])
        self.bad([dict(T[0], to="w1_source")])
        self.bad([dict(T[0], amount_sats=0)])
        self.bad([dict(T[0], amount_sats=1.5)])
        self.bad([dict(T[0], delay_seconds=-1)])
        self.bad([dict(T[0], amount_sats=100_000_000_001)])
        self.bad([{"from": "w1_source", "to": "w2_flowA", "amount_sats": 5}])


class MultiDestinationSchemaTests(PlannerBase):
    @staticmethod
    def base_multi():
        cfg = cfg_with(T)
        flow = cfg["flows"][0]

        old_destination = flow.pop("destination_wallet")

        # The old deterministic fixture ends by paying its legacy terminal
        # destination. Multi-destination finalization is a separate terminal
        # phase, so remove that old terminal hop for schema tests.
        flow["transfers"] = flow["transfers"][:-1]
        flow["finalization_wallet"] = flow["flow_wallets"][-1]

        return cfg, flow, old_destination

    def test_percentage_multi_destination_schema(self):
        cfg, flow, internal = self.base_multi()

        flow["destinations"] = {
            "mode": "percentage",
            "items": [
                {
                    "type": "wallet",
                    "wallet": internal,
                    "percent_bps": 5000,
                },
                {
                    "type": "address",
                    "address": "D_EXTERNAL_TEST_ADDRESS",
                    "percent_bps": 5000,
                },
            ],
        }

        validated = validate(cfg)
        spec = validated["flows"][0]["destinations"]

        self.assertEqual(spec["mode"], "percentage")
        self.assertEqual(len(spec["items"]), 2)
        self.assertEqual(
            sum(item["percent_bps"] for item in spec["items"]),
            10_000,
        )

    def test_fixed_multi_destination_schema_with_remainder(self):
        cfg, flow, internal = self.base_multi()

        flow["destinations"] = {
            "mode": "fixed",
            "items": [
                {
                    "type": "address",
                    "address": "D_EXTERNAL_TEST_ADDRESS",
                    "amount_sats": 50_000_000,
                },
                {
                    "type": "wallet",
                    "wallet": internal,
                    "remainder": True,
                },
            ],
        }

        validated = validate(cfg)

        self.assertEqual(
            validated["flows"][0]["destinations"]["items"][1]["remainder"],
            True,
        )

    def test_multi_destination_percentage_must_total_100_percent(self):
        cfg, flow, internal = self.base_multi()

        flow["destinations"] = {
            "mode": "percentage",
            "items": [
                {
                    "type": "wallet",
                    "wallet": internal,
                    "percent_bps": 9000,
                },
            ],
        }

        with self.assertRaisesRegex(
            ConfigError,
            "10000 basis points",
        ):
            validate(cfg)

    def test_fixed_multi_destination_requires_one_remainder(self):
        cfg, flow, internal = self.base_multi()

        flow["destinations"] = {
            "mode": "fixed",
            "items": [
                {
                    "type": "wallet",
                    "wallet": internal,
                    "amount_sats": 50_000_000,
                },
            ],
        }

        with self.assertRaisesRegex(
            ConfigError,
            "exactly one remainder",
        ):
            validate(cfg)

    def test_multi_destination_limit_is_ten(self):
        cfg, flow, _ = self.base_multi()

        flow["destinations"] = {
            "mode": "percentage",
            "items": [
                {
                    "type": "address",
                    "address": f"D_EXTERNAL_{i}",
                    "percent_bps": 1000 if i < 10 else 1,
                }
                for i in range(11)
            ],
        }

        with self.assertRaisesRegex(
            ConfigError,
            "1..10 destinations",
        ):
            validate(cfg)

    def test_multi_destination_can_be_reviewed_and_approved(self):
        cfg, flow, internal = self.base_multi()

        flow["destinations"] = {
            "mode": "percentage",
            "items": [
                {
                    "type": "wallet",
                    "wallet": internal,
                    "percent_bps": 5000,
                },
                {
                    "type": "address",
                    "address": "D_EXTERNAL_TEST_ADDRESS",
                    "percent_bps": 5000,
                },
            ],
        }

        exp = self.e.create_experiment("multi destination approval")
        h = self.e.configure_experiment(exp, cfg)

        review = self.e.review(exp)

        self.assertIn("terminal distribution: percentage", review["text"])
        self.assertIn("50.00%", review["text"])
        self.assertIn(internal, review["text"])
        self.assertIn("D_EXTERNAL_TEST_ADDRESS", review["text"])

        self.e.approve(exp, h)

        db_flow = self.e.list_flows(exp)[0]

        self.assertTrue(
            db_flow["destination_wallet"].startswith("destinations:")
        )
        self.assertNotEqual(
            db_flow["destination_wallet"],
            "D_EXTERNAL_TEST_ADDRESS",
        )

    def test_multi_destination_cannot_coexist_with_legacy_destination(self):
        cfg, flow, internal = self.base_multi()

        flow["destination_wallet"] = internal
        flow["destinations"] = {
            "mode": "percentage",
            "items": [
                {
                    "type": "wallet",
                    "wallet": internal,
                    "percent_bps": 10_000,
                },
            ],
        }

        with self.assertRaisesRegex(
            ConfigError,
            "exactly one destination definition",
        ):
            validate(cfg)


class ExperimentalDecisionTests(PlannerBase):
    def experimental_flow(self, seed=12345, model="uniform"):
        cfg = copy.deepcopy(CFG)
        cfg["randomization"] = {
            "enabled": True,
            "model": model,
            "seed": seed,
        }
        cfg["finalization"] = {
            "mode": "sweep_workers_to_destination",
        }
        cfg["flows"][0]["experimental_topology"] = {
            "transitions": [
                {"from": "w1_source", "to": "w2_flowA"},
                {"from": "w1_source", "to": "w3_flowB"},
                {"from": "w2_flowA", "to": "w2_flowA"},
                {"from": "w2_flowA", "to": "w3_flowB"},
                {"from": "w3_flowB", "to": "w2_flowA"},
                {"from": "w3_flowB", "to": "w3_flowB"},
            ]
        }

        exp = self.e.create_experiment("experimental planner")
        h = self.e.configure_experiment(exp, cfg)
        self.e.approve(exp, h)
        self.e.start(exp)

        flow = self.e.list_flows(exp)[0]["id"]
        self.e.advance_flow(flow, "PLAN")
        return exp, flow

    def test_same_seed_and_index_reproduce_same_decision(self):
        _, flow = self.experimental_flow(seed=2262026)

        a = experimental_decision(self.e, flow, 0)
        b = experimental_decision(self.e, flow, 0)

        self.assertEqual(a, b)

    def test_decision_stays_inside_approved_bounds(self):
        _, flow = self.experimental_flow(seed=777)

        for i in range(50):
            d = experimental_decision(self.e, flow, i)
            self.assertGreaterEqual(d["amount_sats"], CFG["workload"]["amount_sats_min"])
            self.assertLessEqual(d["amount_sats"], CFG["workload"]["amount_sats_max"])
            self.assertGreaterEqual(d["delay_seconds"], CFG["workload"]["delay_seconds_min"])
            self.assertLessEqual(d["delay_seconds"], CFG["workload"]["delay_seconds_max"])

    def test_route_is_always_from_approved_topology(self):
        _, flow = self.experimental_flow(seed=424242)

        cfg = json.loads(self.e.get_experiment(
            self.e.get_flow(flow)["experiment_id"]
        )["config_json"])
        allowed = {
            (t["from"], t["to"])
            for t in cfg["flows"][0]["experimental_topology"]["transitions"]
        }

        for i in range(100):
            d = experimental_decision(self.e, flow, i)
            self.assertIn((d["from"], d["to"]), allowed)

    def test_same_seed_and_index_reproduce_same_route(self):
        _, flow = self.experimental_flow(seed=555)

        a = experimental_decision(self.e, flow, 12)
        b = experimental_decision(self.e, flow, 12)

        self.assertEqual((a["from"], a["to"]), (b["from"], b["to"]))

    def test_destination_is_never_used_during_experimental_workload(self):
        _, flow = self.experimental_flow(seed=888)

        for i in range(100):
            d = experimental_decision(self.e, flow, i)
            self.assertNotEqual(d["from"], "w4_dest")
            self.assertNotEqual(d["to"], "w4_dest")

    def test_self_transfer_can_be_selected(self):
        _, flow = self.experimental_flow(seed=1)

        seen_self = False
        for i in range(500):
            d = experimental_decision(self.e, flow, i)
            if d["from"] == d["to"]:
                seen_self = True
                break

        self.assertTrue(seen_self)


    def test_empty_wallet_routes_are_filtered_out(self):
        _, flow = self.experimental_flow(seed=4242)

        balances = {
            "w1_source": 500_000_000,
            "w2_flowA": 250_000_000,
            "w3_flowB": 0,
        }

        for i in range(100):
            d = experimental_decision(self.e, flow, i, balances)
            self.assertNotEqual(d["from"], "w3_flowB")

    def test_generated_amount_is_capped_by_observed_balance(self):
        _, flow = self.experimental_flow(seed=5150)

        balances = {
            "w1_source": 150_000_000,
            "w2_flowA": 0,
            "w3_flowB": 0,
        }

        for i in range(30):
            d = experimental_decision(self.e, flow, i, balances)
            self.assertEqual(d["from"], "w1_source")
            self.assertLessEqual(d["amount_sats"], 150_000_000)
            self.assertGreaterEqual(
                d["amount_sats"],
                CFG["workload"]["amount_sats_min"],
            )

    def test_no_fundable_route_is_refused(self):
        _, flow = self.experimental_flow(seed=600)

        balances = {
            "w1_source": 0,
            "w2_flowA": 0,
            "w3_flowB": 0,
        }

        with self.assertRaises(PlanError):
            experimental_decision(self.e, flow, 0, balances)

    def test_fee_reserve_reduces_generated_amount_ceiling(self):
        _, flow = self.experimental_flow(seed=810)

        balances = {
            "w1_source": 150_000_000,
            "w2_flowA": 0,
            "w3_flowB": 0,
        }

        for i in range(30):
            d = experimental_decision(
                self.e, flow, i, balances, fee_reserve_sats=10_000_000
            )
            self.assertLessEqual(d["amount_sats"], 140_000_000)
            self.assertEqual(
                d["generated_from"]["fee_reserve_sats"],
                10_000_000,
            )

    def test_route_is_ineligible_when_only_fee_reserve_remains(self):
        _, flow = self.experimental_flow(seed=820)

        balances = {
            "w1_source": 109_000_000,
            "w2_flowA": 0,
            "w3_flowB": 0,
        }

        with self.assertRaises(PlanError):
            experimental_decision(
                self.e,
                flow,
                0,
                balances,
                fee_reserve_sats=10_000_000,
            )

    def test_source_route_is_capped_by_remaining_allocation(self):
        _, flow = self.experimental_flow(seed=840)

        balances = {
            "w1_source": 5_000_000_000,
            "w2_flowA": 0,
            "w3_flowB": 0,
        }

        remaining = CFG["workload"]["amount_sats_min"] + 12_345

        for i in range(30):
            d = experimental_decision(
                self.e,
                flow,
                i,
                balances,
                fee_reserve_sats=10_000_000,
                source_budget_remaining_sats=remaining,
            )

            self.assertEqual(d["from"], "w1_source")
            self.assertLessEqual(d["amount_sats"], remaining)
            self.assertEqual(
                d["generated_from"]["source_budget_remaining_sats"],
                remaining,
            )

    def test_source_routes_become_ineligible_when_budget_below_minimum(self):
        _, flow = self.experimental_flow(seed=850)

        balances = {
            "w1_source": 5_000_000_000,
            "w2_flowA": 250_000_000,
            "w3_flowB": 0,
        }

        for i in range(30):
            d = experimental_decision(
                self.e,
                flow,
                i,
                balances,
                source_budget_remaining_sats=(
                    CFG["workload"]["amount_sats_min"] - 1
                ),
            )
            self.assertNotEqual(d["from"], "w1_source")

    def test_exhausted_source_budget_does_not_block_internal_routes(self):
        _, flow = self.experimental_flow(seed=860)

        balances = {
            "w1_source": 5_000_000_000,
            "w2_flowA": 500_000_000,
            "w3_flowB": 500_000_000,
        }

        for i in range(30):
            d = experimental_decision(
                self.e,
                flow,
                i,
                balances,
                source_budget_remaining_sats=0,
            )
            self.assertIn(d["from"], ("w2_flowA", "w3_flowB"))

    def test_invalid_fee_reserve_is_refused(self):
        _, flow = self.experimental_flow(seed=830)

        with self.assertRaises(PlanError):
            experimental_decision(
                self.e,
                flow,
                0,
                {"w1_source": 500_000_000},
                fee_reserve_sats=-1,
            )


    def test_balance_snapshot_metadata_is_recorded(self):
        _, flow = self.experimental_flow(seed=700)

        balances = {
            "w1_source": 500_000_000,
            "w2_flowA": 0,
            "w3_flowB": 0,
        }

        d = experimental_decision(self.e, flow, 0, balances)

        self.assertEqual(d["generated_from"]["observed_balance_sats"], 500_000_000)
        self.assertEqual(d["generated_from"]["eligible_transition_count"], 2)


    def test_decision_records_replay_metadata(self):
        _, flow = self.experimental_flow(seed=999)

        d = experimental_decision(self.e, flow, 7)

        self.assertEqual(d["generated_from"]["seed"], 999)
        self.assertEqual(d["generated_from"]["decision_index"], 7)
        self.assertEqual(d["generated_from"]["flow_index"], 0)
        self.assertEqual(d["generated_from"]["generator_version"], 1)
        self.assertEqual(d["generated_from"]["model"], "uniform")

    def test_disabled_randomization_is_refused(self):
        _, flow = self.planned()

        with self.assertRaises(PlanError):
            experimental_decision(self.e, flow, 0)

    def test_unsupported_generator_model_is_refused(self):
        _, flow = self.experimental_flow(seed=123, model="weighted")

        with self.assertRaises(PlanError):
            experimental_decision(self.e, flow, 0)


class ExperimentalJobTests(ExperimentalDecisionTests):
    def balances(self):
        return {
            "w1_source": 500_000_000,
            "w2_flowA": 0,
            "w3_flowB": 0,
        }

    def test_one_generated_decision_is_persisted_as_one_job(self):
        _, flow = self.experimental_flow(seed=9001)

        jid = generate_experimental_job(
            self.e,
            flow,
            self.balances(),
            fee_reserve_sats=10_000_000,
        )

        jobs = self.e.list_jobs(flow)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["id"], jid)

        planned = json.loads(jobs[0]["planned_json"])
        generated = json.loads(jobs[0]["generated_from_json"])

        self.assertEqual(planned["step"], 0)
        self.assertIn(planned["from"], self.balances())
        self.assertGreaterEqual(planned["amount_sats"], CFG["workload"]["amount_sats_min"])
        self.assertLessEqual(planned["amount_sats"], 490_000_000)

        self.assertEqual(generated["decision_index"], 0)
        self.assertEqual(generated["seed"], 9001)
        self.assertEqual(generated["fee_reserve_sats"], 10_000_000)
        self.assertEqual(generated["balance_snapshot_sats"], self.balances())

    def test_generated_jobs_account_for_source_allocation(self):
        _, flow = self.experimental_flow(seed=870)

        allocation = self.e.get_flow(flow)["initial_alloc_sats"]

        balances = {
            "w1_source": allocation * 10,
            "w2_flowA": allocation,
            "w3_flowB": allocation,
        }

        source_used = 0
        cfg = json.loads(self.e.get_experiment(
            self.e.get_flow(flow)["experiment_id"]
        )["config_json"])

        for i in range(cfg["workload"]["jobs"]):
            jid = generate_experimental_job(
                self.e,
                flow,
                balances,
                fee_reserve_sats=0,
            )

            job = self.e.get_job(jid)
            plan = json.loads(job["planned_json"])
            generated = json.loads(job["generated_from_json"])

            self.assertEqual(
                generated["source_budget_used_sats"],
                source_used,
            )
            self.assertEqual(
                generated["source_budget_remaining_sats"],
                allocation - source_used,
            )

            if plan["from"] == "w1_source":
                source_used += plan["amount_sats"]
                self.assertLessEqual(source_used, allocation)

            self.e.advance_flow(flow, "EXECUTE")
            act = self.e.begin_action(jid, "broadcast", {"amount_sats": 1})
            txid = f"{i + 80:02x}" * 32
            self.e.complete_action(act, {"txid": txid})
            self.e.advance_flow(flow, "CONFIRMATION")
            self.e.record_confirmation(txid, 2, 200 + i)
            self.e.advance_flow(flow, "NEXT_STATE")
            self.e.advance_flow(flow, "PLAN")

    def test_finalization_job_does_not_advance_decision_index(self):
        _, flow = self.experimental_flow(seed=9004)
        balances = self.balances()

        first = generate_experimental_job(
            self.e,
            flow,
            balances,
            fee_reserve_sats=0,
        )
        self.assertEqual(
            json.loads(self.e.get_job(first)["generated_from_json"])["decision_index"],
            0,
        )
        self.confirm(flow, first, 70)

        cleanup = self.e.add_job(
            flow,
            {
                "from": "w2_flowA",
                "to": "w4_dest",
                "amount_sats": "all",
                "step": 99,
            },
            planned_delay_s=0,
            generated_from={
                "source": "experimental finalization",
                "phase": "finalization",
                "worker": "w2_flowA",
            },
            depends_on=[first],
        )
        self.confirm(flow, cleanup, 71)

        second = generate_experimental_job(
            self.e,
            flow,
            balances,
            fee_reserve_sats=0,
        )
        generated = json.loads(
            self.e.get_job(second)["generated_from_json"]
        )

        self.assertEqual(generated["decision_index"], 1)
        self.assertEqual(
            generated["source"],
            "seeded experimental generator",
        )

    def _finish_experimental_workload(self, flow, balances):
        cfg = json.loads(self.e.get_experiment(
            self.e.get_flow(flow)["experiment_id"]
        )["config_json"])

        for i in range(cfg["workload"]["jobs"]):
            jid = generate_experimental_job(
                self.e,
                flow,
                balances,
                fee_reserve_sats=0,
            )
            self.confirm(flow, jid, 120 + i)

    def test_finalization_skips_zero_balance_worker(self):
        _, flow = self.experimental_flow(seed=9010)
        balances = {
            "w1_source": 500_000_000,
            "w2_flowA": 0,
            "w3_flowB": 250_000_000,
        }

        self._finish_experimental_workload(flow, balances)

        jid = generate_experimental_finalization_job(
            self.e,
            flow,
            balances,
            fee_reserve_sats=10_000_000,
        )

        job = self.e.get_job(jid)
        plan = json.loads(job["planned_json"])
        generated = json.loads(job["generated_from_json"])

        self.assertEqual(plan["from"], "w3_flowB")
        self.assertEqual(plan["to"], "w4_dest")
        self.assertEqual(plan["amount_sats"], "all")
        self.assertEqual(generated["phase"], "finalization")
        self.assertEqual(generated["worker"], "w3_flowB")

    def test_finalization_refuses_positive_balance_below_fee_reserve(self):
        _, flow = self.experimental_flow(seed=9011)
        balances = {
            "w1_source": 500_000_000,
            "w2_flowA": 5_000_000,
            "w3_flowB": 0,
        }

        self._finish_experimental_workload(flow, balances)

        with self.assertRaisesRegex(
            PlanError,
            "does not cover finalization fee reserve",
        ):
            generate_experimental_finalization_job(
                self.e,
                flow,
                balances,
                fee_reserve_sats=10_000_000,
            )

    def test_finalization_returns_none_when_all_workers_are_empty(self):
        _, flow = self.experimental_flow(seed=9012)
        balances = {
            "w1_source": 500_000_000,
            "w2_flowA": 0,
            "w3_flowB": 0,
        }

        self._finish_experimental_workload(flow, balances)

        self.assertIsNone(
            generate_experimental_finalization_job(
                self.e,
                flow,
                balances,
                fee_reserve_sats=10_000_000,
            )
        )

    def test_second_job_requires_first_to_be_confirmed(self):
        _, flow = self.experimental_flow(seed=9002)

        generate_experimental_job(self.e, flow, self.balances())

        with self.assertRaises(PlanError):
            generate_experimental_job(self.e, flow, self.balances())

    def test_count_workload_stops_at_approved_job_count(self):
        _, flow = self.experimental_flow(seed=9003)

        cfg = json.loads(self.e.get_experiment(
            self.e.get_flow(flow)["experiment_id"]
        )["config_json"])
        limit = cfg["workload"]["jobs"]

        balances = self.balances()

        for i in range(limit):
            jid = generate_experimental_job(self.e, flow, balances)

            job = self.e.get_job(jid)
            self.assertEqual(json.loads(job["planned_json"])["step"], i)

            self.e.advance_flow(flow, "EXECUTE")
            act = self.e.begin_action(jid, "broadcast", {"amount_sats": 1})
            txid = f"{i + 1:02x}" * 32
            self.e.complete_action(act, {"txid": txid})
            self.e.advance_flow(flow, "CONFIRMATION")
            self.e.record_confirmation(txid, 2, 100 + i)
            self.e.advance_flow(flow, "NEXT_STATE")

            if i + 1 < limit:
                self.e.advance_flow(flow, "PLAN")

        self.e.advance_flow(flow, "PLAN")

        with self.assertRaises(PlanError):
            generate_experimental_job(self.e, flow, balances)


class MultiDestinationFinalizationPlannerTests(PlannerBase):
    def multi_flow(self):
        cfg = copy.deepcopy(CFG)
        flow = cfg["flows"][0]

        flow.pop("destination_wallet")

        flow["allocation_wallet"] = "w2_flowA"
        flow["finalization_wallet"] = "w2_flowA"

        flow["destinations"] = {
            "mode": "percentage",
            "items": [
                {
                    "type": "wallet",
                    "wallet": "w4_dest",
                    "percent_bps": 5000,
                },
                {
                    "type": "address",
                    "address": "D_EXTERNAL_TEST_ADDRESS",
                    "percent_bps": 5000,
                },
            ],
        }

        flow.pop("transfers", None)

        flow["experimental_topology"] = {
            "transitions": [
                {
                    "from": "w2_flowA",
                    "to": "w3_flowB",
                },
                {
                    "from": "w3_flowB",
                    "to": "w2_flowA",
                },
            ],
        }

        cfg["randomization"] = {
            "enabled": True,
            "model": "uniform",
            "seed": 9020,
        }

        cfg["finalization"] = {
            "mode": "consolidate_then_distribute",
        }

        exp = self.e.create_experiment(
            "multi destination finalization planner"
        )
        h = self.e.configure_experiment(exp, cfg)
        self.e.approve(exp, h)
        self.e.start(exp)

        flow_id = self.e.list_flows(exp)[0]["id"]
        self.e.advance_flow(flow_id, "PLAN")

        return exp, flow_id

    def finish_workload(self, flow_id):
        cfg = json.loads(
            self.e.get_experiment(
                self.e.get_flow(flow_id)["experiment_id"]
            )["config_json"]
        )

        balances = {
            "w1_source": 500_000_000,
            "w2_flowA": 300_000_000,
            "w3_flowB": 300_000_000,
        }

        for i in range(cfg["workload"]["jobs"]):
            jid = generate_experimental_job(
                self.e,
                flow_id,
                balances,
                fee_reserve_sats=0,
            )
            self.confirm(flow_id, jid, 180 + i)

    def test_consolidates_non_finalizer_then_creates_terminal_job(self):
        _, flow_id = self.multi_flow()
        self.finish_workload(flow_id)

        first = generate_experimental_finalization_job(
            self.e,
            flow_id,
            {
                "w1_source": 500_000_000,
                "w2_flowA": 150_000_000,
                "w3_flowB": 250_000_000,
            },
            fee_reserve_sats=10_000_000,
        )

        first_job = self.e.get_job(first)
        first_plan = json.loads(first_job["planned_json"])
        first_meta = json.loads(
            first_job["generated_from_json"]
        )

        self.assertEqual(first_plan["from"], "w3_flowB")
        self.assertEqual(first_plan["to"], "w2_flowA")
        self.assertEqual(first_plan["amount_sats"], "all")
        self.assertEqual(
            first_meta["source"],
            "experimental finalization consolidation",
        )
        self.assertEqual(
            first_meta["phase"],
            "consolidation",
        )

        self.confirm(flow_id, first, 220)

        terminal = generate_experimental_finalization_job(
            self.e,
            flow_id,
            {
                "w1_source": 500_000_000,
                "w2_flowA": 390_000_000,
                "w3_flowB": 0,
            },
            fee_reserve_sats=10_000_000,
        )

        terminal_job = self.e.get_job(terminal)
        terminal_plan = json.loads(
            terminal_job["planned_json"]
        )
        terminal_meta = json.loads(
            terminal_job["generated_from_json"]
        )

        self.assertEqual(
            terminal_plan["from"],
            "w2_flowA",
        )
        self.assertEqual(
            terminal_plan["amount_sats"],
            "all",
        )
        self.assertNotIn("to", terminal_plan)
        self.assertEqual(
            terminal_plan["distribution"]["mode"],
            "percentage",
        )
        self.assertEqual(
            len(terminal_plan["distribution"]["items"]),
            2,
        )
        self.assertEqual(
            terminal_meta["source"],
            "experimental terminal distribution",
        )
        self.assertEqual(
            terminal_meta["phase"],
            "terminal_distribution",
        )

    def test_finalizer_is_never_swept_to_itself(self):
        _, flow_id = self.multi_flow()
        self.finish_workload(flow_id)

        jid = generate_experimental_finalization_job(
            self.e,
            flow_id,
            {
                "w1_source": 500_000_000,
                "w2_flowA": 400_000_000,
                "w3_flowB": 0,
            },
            fee_reserve_sats=10_000_000,
        )

        plan = json.loads(
            self.e.get_job(jid)["planned_json"]
        )

        self.assertEqual(plan["from"], "w2_flowA")
        self.assertNotIn("to", plan)
        self.assertIn("distribution", plan)

    def test_terminal_job_is_generated_only_once(self):
        _, flow_id = self.multi_flow()
        self.finish_workload(flow_id)

        jid = generate_experimental_finalization_job(
            self.e,
            flow_id,
            {
                "w1_source": 500_000_000,
                "w2_flowA": 400_000_000,
                "w3_flowB": 0,
            },
            fee_reserve_sats=10_000_000,
        )

        self.confirm(flow_id, jid, 230)

        self.assertIsNone(
            generate_experimental_finalization_job(
                self.e,
                flow_id,
                {
                    "w1_source": 500_000_000,
                    "w2_flowA": 0,
                    "w3_flowB": 0,
                },
                fee_reserve_sats=10_000_000,
            )
        )


class SettlementCyclePlannerTests(PlannerBase):
    def settlement_flow(self, max_total_transactions=100):
        cfg = compile_play(
            "settlement_cycle",
            {
                "source_wallet": "w1_source",
                "allocation_wallet": "w2_flowA",
                "workers": ["w3_flowB", "w4_dest"],
                "hubs": [],
                "allocation_sats": 500_000_000,
                "outbound_decisions": 2,
                "return_decisions": 3,
                "amount_sats_min": 10_000_000,
                "amount_sats_max": 50_000_000,
                "delay_seconds_min": 5,
                "delay_seconds_max": 60,
                "settlement_delay_seconds_min": 30,
                "settlement_delay_seconds_max": 180,
                "reserve_return_delay_seconds_min": 0,
                "reserve_return_delay_seconds_max": 60,
                "confirmations_required": 2,
                "seed": 7001,
                "max_total_transactions": max_total_transactions,
                "settlement": {
                    "mode": "fixed",
                    "items": [{
                        "type": "address",
                        "address": "D_EXTERNAL_TEST_ADDRESS",
                        "amount_sats": 50_000_000,
                    }],
                },
            },
        )

        exp = self.e.create_experiment("settlement planner")
        h = self.e.configure_experiment(exp, cfg)
        self.e.approve(exp, h)
        self.e.start(exp)

        flow = self.e.list_flows(exp)[0]["id"]
        self.e.advance_flow(flow, "PLAN")

        return exp, flow, cfg

    def add_confirmed_phase_job(
            self,
            flow,
            *,
            phase,
            source,
            decision_index=None,
            n=1,
    ):
        jobs = self.e.list_jobs(flow)

        generated = {
            "source": source,
            "phase": phase,
        }

        if decision_index is not None:
            generated["decision_index"] = decision_index

        jid = self.e.add_job(
            flow,
            {
                "from": "w2_flowA",
                "to": "w3_flowB",
                "amount_sats": 10_000_000,
                "step": len(jobs),
            },
            planned_delay_s=0,
            generated_from=generated,
            depends_on=[jobs[-1]["id"]] if jobs else (),
        )

        self.confirm(flow, jid, n)
        return jid

    def test_progress_counts_outbound_and_return_decisions_independently(self):
        _, flow, _ = self.settlement_flow()

        self.add_confirmed_phase_job(
            flow,
            phase="workload",
            source="settlement cycle outbound",
            decision_index=0,
            n=1,
        )
        self.add_confirmed_phase_job(
            flow,
            phase="workload",
            source="settlement cycle outbound",
            decision_index=1,
            n=2,
        )
        self.add_confirmed_phase_job(
            flow,
            phase="return_workload",
            source="settlement cycle return",
            decision_index=0,
            n=3,
        )

        progress = settlement_cycle_progress(self.e, flow)

        self.assertEqual(progress["outbound_decisions"], 2)
        self.assertEqual(progress["return_decisions"], 1)
        self.assertEqual(progress["total_transactions"], 3)

    def test_non_workload_phases_do_not_increment_decision_counts(self):
        _, flow, _ = self.settlement_flow()

        for n, phase in enumerate(
            (
                "allocation",
                "consolidation",
                "settlement",
                "consolidation",
                "reserve_return",
            ),
            1,
        ):
            self.add_confirmed_phase_job(
                flow,
                phase=phase,
                source=f"settlement cycle {phase}",
                n=n,
            )

        progress = settlement_cycle_progress(self.e, flow)

        self.assertEqual(progress["outbound_decisions"], 0)
        self.assertEqual(progress["return_decisions"], 0)
        self.assertEqual(progress["total_transactions"], 5)

    def test_unconfirmed_job_blocks_phase_progress(self):
        _, flow, _ = self.settlement_flow()

        jid = self.e.add_job(
            flow,
            {
                "from": "w2_flowA",
                "to": "w3_flowB",
                "amount_sats": 10_000_000,
                "step": 0,
            },
            planned_delay_s=0,
            generated_from={
                "source": "settlement cycle outbound",
                "phase": "workload",
                "decision_index": 0,
            },
        )

        progress = settlement_cycle_progress(self.e, flow)

        self.assertFalse(progress["all_jobs_confirmed"])
        self.assertEqual(progress["outbound_decisions"], 1)
        self.assertEqual(progress["total_transactions"], 1)
        self.assertEqual(self.e.get_job(jid)["state"], "PLANNED")

    def test_settlement_decision_is_seed_reproducible(self):
        _, flow, _ = self.settlement_flow()

        balances = {
            "w1_source": 500_000_000,
            "w2_flowA": 500_000_000,
            "w3_flowB": 0,
            "w4_dest": 0,
        }

        a = settlement_cycle_decision(
            self.e,
            flow,
            "workload",
            0,
            balances_sats=balances,
            fee_reserve_sats=1_000,
        )
        b = settlement_cycle_decision(
            self.e,
            flow,
            "workload",
            0,
            balances_sats=balances,
            fee_reserve_sats=1_000,
        )

        self.assertEqual(a, b)

    def test_settlement_decision_uses_only_approved_phase_routes(self):
        _, flow, cfg = self.settlement_flow()

        decision = settlement_cycle_decision(
            self.e,
            flow,
            "workload",
            0,
            balances_sats={
                "w1_source": 500_000_000,
                "w2_flowA": 500_000_000,
                "w3_flowB": 0,
                "w4_dest": 0,
            },
            fee_reserve_sats=1_000,
        )

        route = {
            "from": decision["from"],
            "to": decision["to"],
        }

        self.assertIn(
            route,
            cfg["settlement_cycle"]["outbound"]["transitions"],
        )

    def test_settlement_decision_respects_amount_and_delay_bounds(self):
        _, flow, cfg = self.settlement_flow()

        decision = settlement_cycle_decision(
            self.e,
            flow,
            "workload",
            0,
            balances_sats={
                "w1_source": 500_000_000,
                "w2_flowA": 500_000_000,
                "w3_flowB": 0,
                "w4_dest": 0,
            },
            fee_reserve_sats=1_000,
        )

        spec = cfg["settlement_cycle"]["outbound"]

        self.assertGreaterEqual(
            decision["amount_sats"],
            spec["amount_sats_min"],
        )
        self.assertLessEqual(
            decision["amount_sats"],
            spec["amount_sats_max"],
        )
        self.assertGreaterEqual(
            decision["delay_seconds"],
            spec["delay_seconds_min"],
        )
        self.assertLessEqual(
            decision["delay_seconds"],
            spec["delay_seconds_max"],
        )

    def test_settlement_decision_filters_unfunded_routes(self):
        _, flow, _ = self.settlement_flow()

        decision = settlement_cycle_decision(
            self.e,
            flow,
            "workload",
            0,
            balances_sats={
                "w1_source": 500_000_000,
                "w2_flowA": 50_000_000,
                "w3_flowB": 0,
                "w4_dest": 0,
            },
            fee_reserve_sats=1_000,
        )

        self.assertEqual(decision["from"], "w2_flowA")

    def test_return_workload_can_start_from_stage_after_settlement(self):
        _, flow, cfg = self.settlement_flow()

        decision = settlement_cycle_decision(
            self.e,
            flow,
            "return_workload",
            0,
            balances_sats={
                "w1_source": 500_000_000,
                "w2_flowA": 300_000_000,
                "w3_flowB": 0,
                "w4_dest": 0,
            },
            fee_reserve_sats=1_000,
        )

        route = {
            "from": decision["from"],
            "to": decision["to"],
        }

        self.assertEqual(decision["from"], "w2_flowA")
        self.assertIn(
            route,
            cfg["settlement_cycle"]["return"]["transitions"],
        )
        self.assertEqual(
            decision["generated_from"]["phase"],
            "return_workload",
        )
        self.assertEqual(
            decision["generated_from"]["decision_index"],
            0,
        )

    def test_settlement_decision_refuses_when_no_route_is_fundable(self):
        _, flow, _ = self.settlement_flow()

        with self.assertRaisesRegex(
            PlanError,
            "no approved Settlement Cycle transition",
        ):
            settlement_cycle_decision(
                self.e,
                flow,
                "workload",
                0,
                balances_sats={
                    "w1_source": 500_000_000,
                    "w2_flowA": 1_000,
                    "w3_flowB": 0,
                    "w4_dest": 0,
                },
                fee_reserve_sats=1_000,
            )

    def test_phase_generator_persists_outbound_workload_decision(self):
        _, flow, cfg = self.settlement_flow()

        self.add_confirmed_phase_job(
            flow,
            phase="allocation",
            source="experimental allocation commit",
            n=110,
        )

        balances = {
            "w1_source": 500_000_000,
            "w2_flowA": 500_000_000,
            "w3_flowB": 0,
            "w4_dest": 0,
        }

        expected = settlement_cycle_decision(
            self.e,
            flow,
            "workload",
            0,
            balances_sats=balances,
            fee_reserve_sats=1_000,
        )

        jid = generate_settlement_cycle_phase_job(
            self.e,
            flow,
            balances,
            fee_reserve_sats=1_000,
        )

        job = self.e.get_job(jid)
        plan = json.loads(job["planned_json"])
        meta = json.loads(job["generated_from_json"])

        self.assertEqual(plan["from"], expected["from"])
        self.assertEqual(plan["to"], expected["to"])
        self.assertEqual(
            plan["amount_sats"],
            expected["amount_sats"],
        )
        self.assertEqual(
            job["planned_delay_s"],
            expected["delay_seconds"],
        )
        self.assertEqual(meta["phase"], "workload")
        self.assertEqual(
            meta["source"],
            "settlement cycle outbound",
        )
        self.assertEqual(meta["decision_index"], 0)
        self.assertEqual(
            {
                "from": plan["from"],
                "to": plan["to"],
            },
            {
                "from": expected["from"],
                "to": expected["to"],
            },
        )
        self.assertIn(
            {
                "from": plan["from"],
                "to": plan["to"],
            },
            cfg["settlement_cycle"]["outbound"]["transitions"],
        )

    def test_phase_generator_advances_outbound_decision_index(self):
        _, flow, _ = self.settlement_flow()

        self.add_confirmed_phase_job(
            flow,
            phase="allocation",
            source="experimental allocation commit",
            n=120,
        )
        self.add_confirmed_phase_job(
            flow,
            phase="workload",
            source="settlement cycle outbound",
            decision_index=0,
            n=121,
        )

        balances = {
            "w1_source": 500_000_000,
            "w2_flowA": 450_000_000,
            "w3_flowB": 50_000_000,
            "w4_dest": 0,
        }

        expected = settlement_cycle_decision(
            self.e,
            flow,
            "workload",
            1,
            balances_sats=balances,
            fee_reserve_sats=1_000,
        )

        jid = generate_settlement_cycle_phase_job(
            self.e,
            flow,
            balances,
            fee_reserve_sats=1_000,
        )

        job = self.e.get_job(jid)
        meta = json.loads(job["generated_from_json"])

        self.assertEqual(meta["decision_index"], 1)
        self.assertEqual(
            json.loads(job["planned_json"])["amount_sats"],
            expected["amount_sats"],
        )

    def test_return_workload_has_independent_decision_index(self):
        _, flow, cfg = self.settlement_flow()

        self.add_confirmed_phase_job(
            flow,
            phase="allocation",
            source="experimental allocation commit",
            n=130,
        )

        for i in range(2):
            self.add_confirmed_phase_job(
                flow,
                phase="workload",
                source="settlement cycle outbound",
                decision_index=i,
                n=131 + i,
            )

        self.add_confirmed_phase_job(
            flow,
            phase="settlement",
            source="settlement cycle settlement",
            n=133,
        )

        balances = {
            "w1_source": 500_000_000,
            "w2_flowA": 300_000_000,
            "w3_flowB": 0,
            "w4_dest": 0,
        }

        expected = settlement_cycle_decision(
            self.e,
            flow,
            "return_workload",
            0,
            balances_sats=balances,
            fee_reserve_sats=1_000,
        )

        jid = generate_settlement_cycle_phase_job(
            self.e,
            flow,
            balances,
            fee_reserve_sats=1_000,
        )

        job = self.e.get_job(jid)
        plan = json.loads(job["planned_json"])
        meta = json.loads(job["generated_from_json"])

        self.assertEqual(meta["phase"], "return_workload")
        self.assertEqual(
            meta["source"],
            "settlement cycle return",
        )
        self.assertEqual(meta["decision_index"], 0)
        self.assertEqual(plan["from"], expected["from"])
        self.assertEqual(plan["to"], expected["to"])
        self.assertEqual(
            plan["amount_sats"],
            expected["amount_sats"],
        )
        self.assertIn(
            {
                "from": plan["from"],
                "to": plan["to"],
            },
            cfg["settlement_cycle"]["return"]["transitions"],
        )

    def test_phase_generator_persists_intermediate_settlement_job(self):
        _, flow, cfg = self.settlement_flow()

        self.add_confirmed_phase_job(
            flow,
            phase="allocation",
            source="experimental allocation commit",
            n=140,
        )

        for i in range(2):
            self.add_confirmed_phase_job(
                flow,
                phase="workload",
                source="settlement cycle outbound",
                decision_index=i,
                n=141 + i,
            )

        balances = {
            "w1_source": 500_000_000,
            "w2_flowA": 450_000_000,
            "w3_flowB": 0,
            "w4_dest": 0,
        }

        jid = generate_settlement_cycle_phase_job(
            self.e,
            flow,
            balances,
            fee_reserve_sats=1_000,
        )

        job = self.e.get_job(jid)
        plan = json.loads(job["planned_json"])
        meta = json.loads(job["generated_from_json"])

        self.assertEqual(plan["from"], "w2_flowA")
        self.assertEqual(plan["amount_sats"], "all")
        self.assertEqual(
            plan["settlement"],
            cfg["settlement_cycle"]["settlement"],
        )

        self.assertEqual(meta["phase"], "settlement")
        self.assertEqual(
            meta["source"],
            "settlement cycle settlement",
        )
        self.assertEqual(
            meta["balance_snapshot_sats"],
            balances,
        )

        spec = cfg["settlement_cycle"]["settlement"]

        self.assertGreaterEqual(
            job["planned_delay_s"],
            spec["delay_seconds_min"],
        )
        self.assertLessEqual(
            job["planned_delay_s"],
            spec["delay_seconds_max"],
        )

    def test_settlement_job_is_generated_only_once(self):
        _, flow, _ = self.settlement_flow()

        self.add_confirmed_phase_job(
            flow,
            phase="allocation",
            source="experimental allocation commit",
            n=150,
        )

        for i in range(2):
            self.add_confirmed_phase_job(
                flow,
                phase="workload",
                source="settlement cycle outbound",
                decision_index=i,
                n=151 + i,
            )

        self.add_confirmed_phase_job(
            flow,
            phase="settlement",
            source="settlement cycle settlement",
            n=153,
        )

        jid = generate_settlement_cycle_phase_job(
            self.e,
            flow,
            {
                "w1_source": 500_000_000,
                "w2_flowA": 300_000_000,
                "w3_flowB": 0,
                "w4_dest": 0,
            },
            fee_reserve_sats=1_000,
        )

        job = self.e.get_job(jid)
        meta = json.loads(job["generated_from_json"])

        self.assertEqual(
            meta["phase"],
            "return_workload",
        )

    def test_generate_pre_settlement_consolidation_sweeps_to_stage(self):
        _, flow, _ = self.settlement_flow()

        self.add_confirmed_phase_job(
            flow,
            phase="allocation",
            source="experimental allocation commit",
            n=70,
        )

        for i in range(2):
            self.add_confirmed_phase_job(
                flow,
                phase="workload",
                source="settlement cycle outbound",
                decision_index=i,
                n=71 + i,
            )

        jid = generate_settlement_cycle_phase_job(
            self.e,
            flow,
            {
                "w1_source": 500_000_000,
                "w2_flowA": 300_000_000,
                "w3_flowB": 100_000_000,
                "w4_dest": 0,
            },
            fee_reserve_sats=1_000,
        )

        job = self.e.get_job(jid)
        plan = json.loads(job["planned_json"])
        meta = json.loads(job["generated_from_json"])

        self.assertEqual(plan["from"], "w3_flowB")
        self.assertEqual(plan["to"], "w2_flowA")
        self.assertEqual(plan["amount_sats"], "all")
        self.assertEqual(meta["phase"], "consolidation")
        self.assertEqual(
            meta["source"],
            "settlement cycle consolidation",
        )

    def test_consolidation_skips_zero_balance_internal_wallets(self):
        _, flow, _ = self.settlement_flow()

        self.add_confirmed_phase_job(
            flow,
            phase="allocation",
            source="experimental allocation commit",
            n=80,
        )

        for i in range(2):
            self.add_confirmed_phase_job(
                flow,
                phase="workload",
                source="settlement cycle outbound",
                decision_index=i,
                n=81 + i,
            )

        jid = generate_settlement_cycle_phase_job(
            self.e,
            flow,
            {
                "w1_source": 500_000_000,
                "w2_flowA": 400_000_000,
                "w3_flowB": 0,
                "w4_dest": 50_000_000,
            },
            fee_reserve_sats=1_000,
        )

        plan = json.loads(
            self.e.get_job(jid)["planned_json"]
        )

        self.assertEqual(plan["from"], "w4_dest")
        self.assertEqual(plan["to"], "w2_flowA")

    def test_consolidation_refuses_balance_below_fee_reserve(self):
        _, flow, _ = self.settlement_flow()

        self.add_confirmed_phase_job(
            flow,
            phase="allocation",
            source="experimental allocation commit",
            n=90,
        )

        for i in range(2):
            self.add_confirmed_phase_job(
                flow,
                phase="workload",
                source="settlement cycle outbound",
                decision_index=i,
                n=91 + i,
            )

        with self.assertRaisesRegex(
            PlanError,
            "consolidation fee reserve",
        ):
            generate_settlement_cycle_phase_job(
                self.e,
                flow,
                {
                    "w1_source": 500_000_000,
                    "w2_flowA": 400_000_000,
                    "w3_flowB": 999,
                    "w4_dest": 0,
                },
                fee_reserve_sats=1_000,
            )

    def test_generate_reserve_return_sweeps_stage_to_reserve(self):
        _, flow, _ = self.settlement_flow()

        self.add_confirmed_phase_job(
            flow,
            phase="allocation",
            source="experimental allocation commit",
            n=100,
        )

        for i in range(2):
            self.add_confirmed_phase_job(
                flow,
                phase="workload",
                source="settlement cycle outbound",
                decision_index=i,
                n=101 + i,
            )

        self.add_confirmed_phase_job(
            flow,
            phase="settlement",
            source="settlement cycle settlement",
            n=103,
        )

        for i in range(3):
            self.add_confirmed_phase_job(
                flow,
                phase="return_workload",
                source="settlement cycle return",
                decision_index=i,
                n=104 + i,
            )

        jid = generate_settlement_cycle_phase_job(
            self.e,
            flow,
            {
                "w1_source": 500_000_000,
                "w2_flowA": 300_000_000,
                "w3_flowB": 0,
                "w4_dest": 0,
            },
            fee_reserve_sats=1_000,
        )

        job = self.e.get_job(jid)
        plan = json.loads(job["planned_json"])
        meta = json.loads(job["generated_from_json"])

        self.assertEqual(plan["from"], "w2_flowA")
        self.assertEqual(plan["to"], "w1_source")
        self.assertEqual(plan["amount_sats"], "all")
        self.assertEqual(meta["phase"], "reserve_return")
        self.assertEqual(
            meta["source"],
            "settlement cycle reserve return",
        )

    def test_reserve_return_delay_is_seeded_and_within_approved_bounds(self):
        _, flow, cfg = self.settlement_flow()

        self.add_confirmed_phase_job(
            flow,
            phase="allocation",
            source="experimental allocation commit",
            n=160,
        )

        for i in range(2):
            self.add_confirmed_phase_job(
                flow,
                phase="workload",
                source="settlement cycle outbound",
                decision_index=i,
                n=161 + i,
            )

        self.add_confirmed_phase_job(
            flow,
            phase="settlement",
            source="settlement cycle settlement",
            n=163,
        )

        for i in range(3):
            self.add_confirmed_phase_job(
                flow,
                phase="return_workload",
                source="settlement cycle return",
                decision_index=i,
                n=164 + i,
            )

        balances = {
            "w1_source": 500_000_000,
            "w2_flowA": 300_000_000,
            "w3_flowB": 0,
            "w4_dest": 0,
        }

        jid = generate_settlement_cycle_phase_job(
            self.e,
            flow,
            balances,
            fee_reserve_sats=1_000,
        )

        job = self.e.get_job(jid)
        spec = cfg["settlement_cycle"]["reserve_return"]

        self.assertGreaterEqual(
            job["planned_delay_s"],
            spec["delay_seconds_min"],
        )
        self.assertLessEqual(
            job["planned_delay_s"],
            spec["delay_seconds_max"],
        )

        material = (
            "flowlab-settlement-delay-v1:"
            f"{cfg['randomization']['seed']}:"
            f"{flow}:reserve_return"
        ).encode()

        derived_seed = int.from_bytes(
            hashlib.sha256(material).digest(),
            "big",
        )
        expected = random.Random(
            derived_seed
        ).randint(
            spec["delay_seconds_min"],
            spec["delay_seconds_max"],
        )

        self.assertEqual(
            job["planned_delay_s"],
            expected,
        )

        # This fixture deterministically selects a non-minimum delay,
        # proving reserve return is not simply hardcoded to the floor.
        self.assertNotEqual(
            job["planned_delay_s"],
            spec["delay_seconds_min"],
        )

    def test_phase_generator_refuses_when_previous_job_is_unconfirmed(self):
        _, flow, _ = self.settlement_flow()

        self.e.add_job(
            flow,
            {
                "from": "w1_source",
                "to": "w2_flowA",
                "amount_sats": 500_000_000,
                "step": 0,
            },
            planned_delay_s=0,
            generated_from={
                "source": "experimental allocation commit",
                "phase": "allocation",
            },
        )

        with self.assertRaisesRegex(
            PlanError,
            "previous Settlement Cycle job must be confirmed",
        ):
            generate_settlement_cycle_phase_job(
                self.e,
                flow,
                {
                    "w1_source": 500_000_000,
                    "w2_flowA": 0,
                    "w3_flowB": 0,
                    "w4_dest": 0,
                },
                fee_reserve_sats=1_000,
            )

    def test_next_phase_starts_with_allocation(self):
        _, flow, _ = self.settlement_flow()

        phase = settlement_cycle_next_phase(
            self.e,
            flow,
            {
                "w1_source": 1_000_000_000,
                "w2_flowA": 0,
                "w3_flowB": 0,
                "w4_dest": 0,
            },
        )

        self.assertEqual(phase, "allocation")

    def test_next_phase_moves_to_workload_after_allocation(self):
        _, flow, _ = self.settlement_flow()

        self.add_confirmed_phase_job(
            flow,
            phase="allocation",
            source="experimental allocation commit",
            n=10,
        )

        phase = settlement_cycle_next_phase(
            self.e,
            flow,
            {
                "w1_source": 500_000_000,
                "w2_flowA": 500_000_000,
                "w3_flowB": 0,
                "w4_dest": 0,
            },
        )

        self.assertEqual(phase, "workload")

    def test_outbound_completion_requires_consolidation_when_workers_hold_value(self):
        _, flow, _ = self.settlement_flow()

        self.add_confirmed_phase_job(
            flow,
            phase="allocation",
            source="experimental allocation commit",
            n=20,
        )

        for i in range(2):
            self.add_confirmed_phase_job(
                flow,
                phase="workload",
                source="settlement cycle outbound",
                decision_index=i,
                n=21 + i,
            )

        phase = settlement_cycle_next_phase(
            self.e,
            flow,
            {
                "w1_source": 500_000_000,
                "w2_flowA": 300_000_000,
                "w3_flowB": 100_000_000,
                "w4_dest": 100_000_000,
            },
        )

        self.assertEqual(phase, "consolidation")

    def test_outbound_consolidated_stage_advances_to_settlement(self):
        _, flow, _ = self.settlement_flow()

        self.add_confirmed_phase_job(
            flow,
            phase="allocation",
            source="experimental allocation commit",
            n=30,
        )

        for i in range(2):
            self.add_confirmed_phase_job(
                flow,
                phase="workload",
                source="settlement cycle outbound",
                decision_index=i,
                n=31 + i,
            )

        phase = settlement_cycle_next_phase(
            self.e,
            flow,
            {
                "w1_source": 500_000_000,
                "w2_flowA": 490_000_000,
                "w3_flowB": 0,
                "w4_dest": 0,
            },
        )

        self.assertEqual(phase, "settlement")

    def test_confirmed_settlement_advances_to_return_workload(self):
        _, flow, _ = self.settlement_flow()

        self.add_confirmed_phase_job(
            flow,
            phase="allocation",
            source="experimental allocation commit",
            n=40,
        )

        for i in range(2):
            self.add_confirmed_phase_job(
                flow,
                phase="workload",
                source="settlement cycle outbound",
                decision_index=i,
                n=41 + i,
            )

        self.add_confirmed_phase_job(
            flow,
            phase="settlement",
            source="settlement cycle settlement",
            n=43,
        )

        phase = settlement_cycle_next_phase(
            self.e,
            flow,
            {
                "w1_source": 500_000_000,
                "w2_flowA": 400_000_000,
                "w3_flowB": 0,
                "w4_dest": 0,
            },
        )

        self.assertEqual(phase, "return_workload")

    def test_return_completion_consolidates_then_returns_to_reserve(self):
        _, flow, _ = self.settlement_flow()

        self.add_confirmed_phase_job(
            flow,
            phase="allocation",
            source="experimental allocation commit",
            n=50,
        )

        for i in range(2):
            self.add_confirmed_phase_job(
                flow,
                phase="workload",
                source="settlement cycle outbound",
                decision_index=i,
                n=51 + i,
            )

        self.add_confirmed_phase_job(
            flow,
            phase="settlement",
            source="settlement cycle settlement",
            n=53,
        )

        for i in range(3):
            self.add_confirmed_phase_job(
                flow,
                phase="return_workload",
                source="settlement cycle return",
                decision_index=i,
                n=54 + i,
            )

        phase = settlement_cycle_next_phase(
            self.e,
            flow,
            {
                "w1_source": 500_000_000,
                "w2_flowA": 300_000_000,
                "w3_flowB": 25_000_000,
                "w4_dest": 0,
            },
        )

        self.assertEqual(phase, "consolidation")

        phase = settlement_cycle_next_phase(
            self.e,
            flow,
            {
                "w1_source": 500_000_000,
                "w2_flowA": 325_000_000,
                "w3_flowB": 0,
                "w4_dest": 0,
            },
        )

        self.assertEqual(phase, "reserve_return")

    def test_confirmed_reserve_return_completes_cycle(self):
        _, flow, _ = self.settlement_flow()

        self.add_confirmed_phase_job(
            flow,
            phase="allocation",
            source="experimental allocation commit",
            n=60,
        )

        for i in range(2):
            self.add_confirmed_phase_job(
                flow,
                phase="workload",
                source="settlement cycle outbound",
                decision_index=i,
                n=61 + i,
            )

        self.add_confirmed_phase_job(
            flow,
            phase="settlement",
            source="settlement cycle settlement",
            n=63,
        )

        for i in range(3):
            self.add_confirmed_phase_job(
                flow,
                phase="return_workload",
                source="settlement cycle return",
                decision_index=i,
                n=64 + i,
            )

        self.add_confirmed_phase_job(
            flow,
            phase="reserve_return",
            source="settlement cycle reserve return",
            n=67,
        )

        phase = settlement_cycle_next_phase(
            self.e,
            flow,
            {
                "w1_source": 800_000_000,
                "w2_flowA": 0,
                "w3_flowB": 0,
                "w4_dest": 0,
            },
        )

        self.assertEqual(phase, "complete")

    def test_max_total_transactions_is_checked_against_actual_jobs(self):
        _, flow, _ = self.settlement_flow(
            max_total_transactions=2,
        )

        self.add_confirmed_phase_job(
            flow,
            phase="allocation",
            source="settlement cycle allocation",
            n=1,
        )
        self.add_confirmed_phase_job(
            flow,
            phase="workload",
            source="settlement cycle outbound",
            decision_index=0,
            n=2,
        )

        progress = settlement_cycle_progress(self.e, flow)

        self.assertEqual(progress["total_transactions"], 2)
        self.assertEqual(progress["max_total_transactions"], 2)
        self.assertTrue(progress["transaction_limit_reached"])


class PlanTests(PlannerBase):
    def test_jobs_created_in_order_with_dependencies(self):
        exp, flow = self.planned()
        ids = generate_jobs(self.e, flow)
        jobs = self.e.list_jobs(flow)
        self.assertEqual([j["id"] for j in jobs], ids)
        self.assertEqual([j["planned_delay_s"] for j in jobs], [30, 60, 0])
        deps = self.e.conn.execute("SELECT job_id, depends_on_job_id FROM job_dependencies").fetchall()
        self.assertEqual({(a, b) for a, b in deps}, {(ids[1], ids[0]), (ids[2], ids[1])})

    def test_cannot_plan_twice(self):
        exp, flow = self.planned()
        generate_jobs(self.e, flow)
        with self.assertRaises(PlanError):
            generate_jobs(self.e, flow)

    def test_flow_without_transfers_is_refused(self):
        exp, flow = self.planned(None)
        with self.assertRaises(PlanError):
            generate_jobs(self.e, flow)

    def test_schedule_is_one_at_a_time_in_order(self):
        exp, flow = self.planned()
        ids = generate_jobs(self.e, flow)
        r = next_due(self.e, flow)
        self.assertIsNone(r["job"])
        self.assertEqual(r["wait_s"], 30)
        self.advance(29)
        self.assertIsNone(next_due(self.e, flow)["job"])
        self.advance(1)
        self.assertEqual(next_due(self.e, flow)["job"]["id"], ids[0])
        self.confirm(flow, ids[0], 1)
        r = next_due(self.e, flow)
        self.assertEqual((r["job"], r["wait_s"]), (None, 60))
        self.advance(60)
        self.assertEqual(next_due(self.e, flow)["job"]["id"], ids[1])

    def test_in_flight_job_blocks_the_next(self):
        exp, flow = self.planned()
        ids = generate_jobs(self.e, flow)
        self.advance(30)
        self.e.advance_flow(flow, "EXECUTE")
        act = self.e.begin_action(ids[0], "broadcast", {"amount_sats": 1})
        self.e.complete_action(act, {"txid": "aa" * 32})
        self.advance(10_000)
        r = next_due(self.e, flow)
        self.assertIsNone(r["job"])
        self.assertIn("BROADCAST", r["reason"])

    def test_all_done(self):
        exp, flow = self.planned()
        ids = generate_jobs(self.e, flow)
        for n, jid in enumerate(ids, 1):
            self.advance(100)
            self.assertEqual(next_due(self.e, flow)["job"]["id"], jid)
            self.confirm(flow, jid, n)
        self.assertEqual(next_due(self.e, flow)["reason"], "all jobs confirmed")


if __name__ == "__main__":
    unittest.main()
