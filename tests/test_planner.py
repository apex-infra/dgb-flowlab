import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from flowlab import ConfigError, Engine
from flowlab.planner import (
    PlanError,
    experimental_decision,
    generate_experimental_job,
    generate_jobs,
    next_due,
)
from tests.test_engine import CFG, FakeVerifier

T = [
    {"from": "w1_source", "to": "w2_flowA", "amount_sats": 200_000_000, "delay_seconds": 30},
    {"from": "w2_flowA", "to": "w3_flowB", "amount_sats": 199_000_000, "delay_seconds": 60},
    {"from": "w3_flowB", "to": "w4_dest", "amount_sats": 198_000_000, "delay_seconds": 0},
]


def cfg_with(transfers):
    c = copy.deepcopy(CFG)
    if transfers is not None:
        c["flows"][0]["transfers"] = transfers
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

    def test_rejections(self):
        self.bad([])
        self.bad([dict(T[0], to="w9_unknown")])
        self.bad([dict(T[0], to="w1_source")])
        self.bad([dict(T[0], amount_sats=0)])
        self.bad([dict(T[0], amount_sats=1.5)])
        self.bad([dict(T[0], delay_seconds=-1)])
        self.bad([dict(T[0], amount_sats=100_000_000_001)])
        self.bad([{"from": "w1_source", "to": "w2_flowA", "amount_sats": 5}])


class ExperimentalDecisionTests(PlannerBase):
    def experimental_flow(self, seed=12345, model="uniform"):
        cfg = copy.deepcopy(CFG)
        cfg["randomization"] = {
            "enabled": True,
            "model": model,
            "seed": seed,
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
