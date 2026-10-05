"""
Phase 1 tests. No node, no network, no keys: a fake Verifier stands in for
DigiByte Core. Run from the repo root:

    python -m unittest discover -s tests -v
"""

import copy
import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flowlab import (ApprovalError, ConfigError, EmergencyStopActive, Engine,  # noqa: E402
                     EngineError, GuardFailed, IllegalTransition, Verifier)
from flowlab.states import REQUIRED_CHECKS  # noqa: E402


class FakeVerifier(Verifier):
    def __init__(self):
        self.fail = {}      # check name -> detail string (reported as not-ok)
        self.omit = set()   # checks not reported at all
        self.boom = False
        self.calls = []

    def verify(self, experiment, flow, action):
        self.calls.append(action)
        if self.boom:
            raise RuntimeError("node unreachable")
        out = {c: (True, "ok") for c in REQUIRED_CHECKS if c not in self.omit}
        for name, detail in self.fail.items():
            out[name] = (False, detail)
        return out


CFG = {
    "flows": [{
        "description": "A->B",
        "source_wallet": "w1_source",
        "flow_wallets": ["w2_flowA", "w3_flowB"],
        "destination_wallet": "w4_dest",
        "allocation_sats": 100_000_000_000,
    }],
    "workload": {"mode": "count", "jobs": 3,
                 "amount_sats_min": 100_000_000, "amount_sats_max": 500_000_000,
                 "delay_seconds_min": 30, "delay_seconds_max": 1800},
    "confirmations_required": 2,
    "fee_policy": {"type": "estimate", "conf_target": 6},
    "address_policy": "new",
    "randomization": {"enabled": True, "model": "bounded_random"},
}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "flow.db")
        self.v = FakeVerifier()
        self.e = Engine(self.path, verifier=self.v)

    def tearDown(self):
        try:
            self.e.conn.close()
        except Exception:
            pass
        self.dir.cleanup()

    def approved(self, cfg=None):
        exp = self.e.create_experiment("test run", tags=["t"], notes="n")
        h = self.e.configure_experiment(exp, cfg or CFG)
        self.e.approve(exp, h, "go")
        return exp

    def running(self):
        exp = self.approved()
        self.e.start(exp)
        flow = self.e.list_flows(exp)[0]["id"]
        return exp, flow

    def to_execute_with_job(self):
        exp, flow = self.running()
        self.e.advance_flow(flow, "PLAN")
        job = self.e.add_job(flow, {"amount_sats": 200_000_000, "to": "w2_flowA"},
                             planned_delay_s=45, generated_from={"height": 100})
        self.e.advance_flow(flow, "EXECUTE")
        return exp, flow, job


class LifecycleTests(Base):
    def test_full_happy_path(self):
        exp, flow, job = self.to_execute_with_job()
        act = self.e.begin_action(job, "broadcast", {"amount_sats": 200_000_000, "wallet": "w1_source"})
        self.e.complete_action(act, {"txid": "a" * 64})
        self.e.advance_flow(flow, "CONFIRMATION")
        self.e.record_confirmation("a" * 64, 2, 1234)
        self.assertEqual(self.e.get_job(job)["state"], "CONFIRMED")
        self.e.advance_flow(flow, "NEXT_STATE")
        self.e.advance_flow(flow, "COMPLETE")
        self.e.begin_completion(exp)
        self.assertTrue(self.e.complete_experiment(exp, True, {"balances": "ok"}, {"jobs": 1}))
        self.e.go_idle(exp)
        self.assertEqual(self.e.get_experiment(exp)["state"], "IDLE")
        events = [r["event"] for r in self.e.audit_events(exp)]
        for needed in ("EXPERIMENT CREATED", "EXPERIMENT CONFIGURED", "EXPERIMENT APPROVED",
                       "JOB GENERATED", "ACTION INTENT", "TRANSACTION BROADCAST",
                       "CONFIRMATION RECEIVED", "EXPERIMENT COMPLETED"):
            self.assertIn(needed, events)

    def test_illegal_transitions_rejected(self):
        exp = self.e.create_experiment()
        with self.assertRaises(ApprovalError):
            self.e.approve(exp, "x")                       # not CONFIGURED
        with self.assertRaises(IllegalTransition):
            self.e.start(exp)                              # CREATED -> RUNNING
        exp2, flow = self.running()
        with self.assertRaises(IllegalTransition):
            self.e.advance_flow(flow, "EXECUTE")           # START -> EXECUTE skips PLAN

    def test_jobs_only_while_planning_and_deps_gate_actions(self):
        exp, flow = self.running()
        with self.assertRaises(EngineError):
            self.e.add_job(flow, {"amount_sats": 1})       # flow still START
        self.e.advance_flow(flow, "PLAN")
        j1 = self.e.add_job(flow, {"amount_sats": 1_000})
        j2 = self.e.add_job(flow, {"amount_sats": 2_000}, depends_on=[j1])
        self.e.advance_flow(flow, "EXECUTE")
        with self.assertRaises(EngineError):
            self.e.begin_action(j2, "broadcast", {})       # j1 not CONFIRMED

    def test_flow_error_pauses_experiment_and_needs_resolution(self):
        exp, flow = self.running()
        self.e.flow_error(flow, "unexpected UTXO set")
        self.assertEqual(self.e.get_experiment(exp)["state"], "PAUSED")
        with self.assertRaises(EngineError):
            self.e.advance_flow(flow, "PLAN")
        self.e.flow_resolve_error(flow, "reconciled by hand")
        self.e.resume(exp)
        self.assertEqual(self.e.get_experiment(exp)["state"], "RUNNING")


class ApprovalTests(Base):
    def test_config_validation(self):
        exp = self.e.create_experiment()
        bad = copy.deepcopy(CFG)
        bad["workload"]["amount_sats_min"] = 10**12
        with self.assertRaises(ConfigError):
            self.e.configure_experiment(exp, bad)
        bad = copy.deepcopy(CFG)
        bad["flows"][0]["flow_wallets"] = ["w1_source"]    # duplicate wallet
        with self.assertRaises(ConfigError):
            self.e.configure_experiment(exp, bad)
        bad = copy.deepcopy(CFG)
        bad["flows"][0]["allocation_sats"] = 1.5           # float amounts are not allowed
        with self.assertRaises(ConfigError):
            self.e.configure_experiment(exp, bad)
        bad = copy.deepcopy(CFG)
        bad["rpc_password"] = "hunter2"
        with self.assertRaises(ConfigError):
            self.e.configure_experiment(exp, bad)

    def test_seed_generated_and_recorded_before_approval(self):
        exp = self.e.create_experiment()
        self.e.configure_experiment(exp, CFG)
        row = self.e.get_experiment(exp)
        cfg = json.loads(row["config_json"])
        self.assertIsInstance(cfg["randomization"]["seed"], int)
        self.assertEqual(row["random_seed"], cfg["randomization"]["seed"])

    def test_approval_requires_matching_hash_and_is_one_time(self):
        exp = self.e.create_experiment()
        h1 = self.e.configure_experiment(exp, CFG)
        cfg2 = copy.deepcopy(CFG)
        cfg2["randomization"] = {"enabled": False}
        h2 = self.e.configure_experiment(exp, cfg2)        # reconfigure before approval is fine
        self.assertNotEqual(h1, h2)
        with self.assertRaises(ApprovalError):
            self.e.approve(exp, h1)                        # stale hash
        self.e.approve(exp, h2)
        with self.assertRaises(ApprovalError):
            self.e.approve(exp, h2)                        # second approval
        with self.assertRaises(IllegalTransition):
            self.e.configure_experiment(exp, CFG)          # no edits after approval

    def test_config_immutable_at_database_level(self):
        exp = self.approved()
        with self.assertRaises(sqlite3.DatabaseError):
            self.e.conn.execute("UPDATE experiments SET config_json='{}' WHERE id=?", (exp,))
        with self.assertRaises(sqlite3.DatabaseError):
            self.e.conn.execute("UPDATE experiments SET random_seed=1 WHERE id=?", (exp,))

    def test_review_shows_full_config(self):
        exp = self.e.create_experiment("my test")
        self.e.configure_experiment(exp, CFG)
        text = self.e.review(exp)["text"]
        for needle in ("w1_source -> w2_flowA -> w3_flowB -> w4_dest", "INITIAL PARAMETERS",
                       "AUTOMATIC EXECUTION", "seed="):
            self.assertIn(needle, text)


class GuardTests(Base):
    def test_failing_check_pauses_and_blocks(self):
        exp, flow = self.running()
        self.v.fail["balance"] = "balance below plan"
        with self.assertRaises(GuardFailed):
            self.e.advance_flow(flow, "PLAN")
        row = self.e.get_experiment(exp)
        self.assertEqual(row["state"], "PAUSED")
        self.assertIn("balance below plan", row["state_reason"])
        self.assertEqual(self.e.get_flow(flow)["state"], "START")   # nothing moved

    def test_omitted_check_fails_closed(self):
        exp = self.approved()
        self.v.omit.add("height")
        with self.assertRaises(GuardFailed):
            self.e.start(exp)
        self.assertEqual(self.e.get_experiment(exp)["state"], "PAUSED")

    def test_verifier_exception_fails_closed(self):
        exp = self.approved()
        self.v.boom = True
        with self.assertRaises(GuardFailed):
            self.e.start(exp)

    def test_no_verifier_fails_closed(self):
        e2 = Engine(os.path.join(self.dir.name, "nov.db"), verifier=None)
        exp = e2.create_experiment()
        h = e2.configure_experiment(exp, CFG)
        e2.approve(exp, h)
        with self.assertRaises(GuardFailed):
            e2.start(exp)
        e2.conn.close()

    def test_resume_re_verifies(self):
        exp, flow = self.running()
        self.e.pause(exp, "operator")
        self.v.fail["wallet"] = "wallet not loaded"
        with self.assertRaises(GuardFailed):
            self.e.resume(exp)
        self.assertEqual(self.e.get_experiment(exp)["state"], "PAUSED")
        self.v.fail.clear()
        self.e.resume(exp)
        self.assertEqual(self.e.get_experiment(exp)["state"], "RUNNING")


class RecoveryTests(Base):
    def test_state_survives_restart(self):
        exp, flow = self.running()
        self.e.advance_flow(flow, "PLAN")
        self.e.close(clean=True)
        e2 = Engine(self.path, verifier=self.v)
        summary = e2.recover()
        self.assertTrue(summary["clean_shutdown"])
        self.assertEqual(e2.get_experiment(exp)["state"], "RUNNING")
        self.assertEqual(e2.get_flow(flow)["state"], "PLAN")
        e2.conn.close()

    def test_unclean_restart_goes_to_recovery_and_needs_verification(self):
        exp, flow = self.running()
        self.e.recover()                                   # a normal start consumes the clean flag
        self.e.conn.close()                                # crash: no clean close
        e2 = Engine(self.path, verifier=self.v)
        summary = e2.recover()
        self.assertFalse(summary["clean_shutdown"])
        self.assertEqual(e2.get_experiment(exp)["state"], "RECOVERY")
        e2.leave_recovery(exp)
        self.assertEqual(e2.get_experiment(exp)["state"], "RUNNING")
        e2.conn.close()

    def test_crash_after_intent_never_duplicates_broadcast(self):
        exp, flow, job = self.to_execute_with_job()
        act = self.e.begin_action(job, "broadcast", {"amount_sats": 200_000_000})
        # ---- crash: broadcast may or may not have happened; no complete_action ----
        self.e.conn.close()
        e2 = Engine(self.path, verifier=self.v)
        summary = e2.recover()
        self.assertEqual(summary["unknown_actions"], [act])
        self.assertEqual(e2.get_experiment(exp)["state"], "RECOVERY")
        with self.assertRaises(EngineError):
            e2.leave_recovery(exp)                         # unresolved action blocks
        with self.assertRaises(EngineError):
            e2.begin_action(job, "broadcast", {})          # not RUNNING
        e2.resolve_unknown_action(act, "broadcast", txid="b" * 64, evidence="found in listtransactions")
        self.assertEqual(e2.get_job(job)["txid"], "b" * 64)
        e2.leave_recovery(exp)
        with self.assertRaises(EngineError):               # job already BROADCAST, slot is taken
            e2.begin_action(job, "broadcast", {})
        e2.conn.close()

    def test_unknown_resolved_as_not_broadcast_allows_replan_once(self):
        exp, flow, job = self.to_execute_with_job()
        act = self.e.begin_action(job, "broadcast", {})
        self.e.conn.close()
        e2 = Engine(self.path, verifier=self.v)
        e2.recover()
        e2.resolve_unknown_action(act, "not_broadcast", evidence="no matching tx in wallet or mempool")
        e2.leave_recovery(exp)
        act2 = e2.begin_action(job, "broadcast", {})       # allowed now: previous attempt provably failed
        self.assertNotEqual(act, act2)
        e2.conn.close()

    def test_duplicate_live_action_refused(self):
        exp, flow, job = self.to_execute_with_job()
        self.e.begin_action(job, "broadcast", {})
        with self.assertRaises(EngineError):
            self.e.begin_action(job, "broadcast", {})

    def test_failed_reconciliation_goes_to_error_not_complete(self):
        exp, flow, job = self.to_execute_with_job()
        act = self.e.begin_action(job, "broadcast", {})
        self.e.complete_action(act, {"txid": "c" * 64})
        self.e.advance_flow(flow, "CONFIRMATION")
        self.e.record_confirmation("c" * 64, 5)
        self.e.advance_flow(flow, "NEXT_STATE")
        self.e.advance_flow(flow, "COMPLETE")
        self.e.begin_completion(exp)
        self.assertFalse(self.e.complete_experiment(exp, False))
        self.assertEqual(self.e.get_experiment(exp)["state"], "ERROR")

    def test_completion_requires_confirmations(self):
        exp, flow, job = self.to_execute_with_job()
        act = self.e.begin_action(job, "broadcast", {})
        self.e.complete_action(act, {"txid": "d" * 64})
        self.e.advance_flow(flow, "CONFIRMATION")
        self.e.record_confirmation("d" * 64, 1)            # needs 2
        self.e.advance_flow(flow, "NEXT_STATE")
        self.e.advance_flow(flow, "COMPLETE")
        self.e.begin_completion(exp)
        with self.assertRaises(EngineError):
            self.e.complete_experiment(exp, True)


class EmergencyStopTests(Base):
    def test_stop_aborts_blocks_actions_and_keeps_history(self):
        exp, flow, job = self.to_execute_with_job()
        act = self.e.begin_action(job, "broadcast", {})
        self.e.complete_action(act, {"txid": "e" * 64})
        before = len(self.e.audit_events(exp))
        stopped = self.e.emergency_stop("operator hit stop")
        self.assertEqual(stopped, [exp])
        self.assertEqual(self.e.get_experiment(exp)["state"], "ABORTED")
        self.assertGreaterEqual(len(self.e.audit_events(exp)), before)      # nothing deleted
        self.assertEqual(self.e.get_job(job)["txid"], "e" * 64)             # history intact
        with self.assertRaises(EmergencyStopActive):
            self.e.start(self.approved())
        # monitoring of already-broadcast transactions continues
        self.e.record_confirmation("e" * 64, 3)
        self.assertEqual(self.e.get_job(job)["state"], "CONFIRMED")
        with self.assertRaises(IllegalTransition):
            self.e.resume(exp)                              # ABORTED is final


class AuditTests(Base):
    def test_audit_is_append_only(self):
        self.e.create_experiment()
        with self.assertRaises(sqlite3.DatabaseError):
            self.e.conn.execute("UPDATE audit_log SET event='x'")
        with self.assertRaises(sqlite3.DatabaseError):
            self.e.conn.execute("DELETE FROM audit_log")

    def test_secrets_never_reach_the_audit_log(self):
        exp, flow, job = self.to_execute_with_job()
        with self.assertRaises(EngineError):
            self.e.begin_action(job, "broadcast", {"rpc_password": "hunter2"})
        act = self.e.begin_action(job, "broadcast", {"amount_sats": 1})
        with self.assertRaises(EngineError):
            self.e.complete_action(act, {"txid": "f" * 64, "privkey": "L1abc"})
        blob = json.dumps(self.e.audit_events())
        self.assertNotIn("hunter2", blob)
        self.assertNotIn("L1abc", blob)

    def test_random_seed_is_logged_but_seed_phrase_is_redacted(self):
        from flowlab import audit
        self.assertEqual(audit.redact({"random_seed": 7})["random_seed"], 7)
        self.assertEqual(audit.redact({"seed_phrase": "a b c"})["seed_phrase"], "[REDACTED]")
        self.assertEqual(audit.redact({"x": "xprv" + "A" * 30})["x"], "[REDACTED]")


if __name__ == "__main__":
    unittest.main(verbosity=2)
