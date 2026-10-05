import copy
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from flowlab import ConfigError, Engine
from flowlab.planner import PlanError, generate_jobs, next_due
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
