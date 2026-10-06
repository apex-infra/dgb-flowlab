"""
executor.py -- runs the operator's approved transfer list, one safe step at a time.

tick() makes at most one state change per flow and then returns, so it can be
called in a loop (or by hand). Every action still goes through the engine's
verification and journal. Anything unexpected pauses the experiment; nothing
is retried automatically.
"""

import json

from .engine import EngineError, GuardFailed
from .planner import (
    PlanError,
    generate_experimental_job,
    generate_jobs,
    next_due,
)
from .rpc import RpcError, to_sats
from .tx_builder import BuildError, broadcast

POLL_SECONDS = 15


class Executor:
    def __init__(self, engine, rpc, builder, log=lambda msg: None):
        self.engine, self.rpc, self.builder, self.log = engine, rpc, builder, log

    # ------------------------------------------------------------------ tick
    def tick(self, exp_id):
        e = self.engine
        exp = e.get_experiment(exp_id)
        out = {"actions": [], "wait_s": None, "blocked": None, "done": False}
        if exp["state"] != "RUNNING":
            out["blocked"] = f"experiment is {exp['state']}"
            return out
        waits = []
        try:
            for flow in e.list_flows(exp_id):
                self._tick_flow(exp_id, flow["id"], out["actions"], waits)
            flows = e.list_flows(exp_id)
            if flows and all(f["state"] == "COMPLETE" for f in flows):
                e.begin_completion(exp_id)
                ok, final, stats = self.reconcile(exp_id)
                done_ok = e.complete_experiment(exp_id, ok, final, stats)
                out["actions"].append("completed and reconciled" if done_ok
                                      else f"RECONCILIATION FAILED: {stats['issues']}")
                out["done"] = True
        except GuardFailed as g:
            out["blocked"] = f"verification failed: {g}"
            self._pause(exp_id, out["blocked"])
        except (BuildError, RpcError, PlanError, EngineError) as err:
            out["blocked"] = f"{type(err).__name__}: {err}"
            self._pause(exp_id, out["blocked"])
        if waits and not out["blocked"]:
            out["wait_s"] = min(waits)
        for a in out["actions"]:
            self.log(a)
        return out

    def _pause(self, exp_id, reason):
        if self.engine.get_experiment(exp_id)["state"] == "RUNNING":
            self.engine.pause(exp_id, reason[:300])

    # ---------------------------------------------------- experiment planning
    def _config_for_flow(self, flow):
        exp = self.engine.get_experiment(flow["experiment_id"])
        return json.loads(exp["config_json"])

    def _is_experimental(self, flow):
        cfg = self._config_for_flow(flow)
        return bool(cfg.get("randomization", {}).get("enabled"))

    def _experimental_complete(self, flow):
        cfg = self._config_for_flow(flow)
        workload = cfg.get("workload") or {}
        if workload.get("mode") != "count":
            raise PlanError("executor v1 currently supports count experimental workloads only")
        jobs = self.engine.list_jobs(flow["id"])
        return (
            len(jobs) >= workload["jobs"]
            and all(j["state"] == "CONFIRMED" for j in jobs)
        )

    def _confirmed_balances(self, flow):
        wallets = [
            flow["source_wallet"],
            *json.loads(flow["flow_wallets_json"]),
        ]
        return {
            wallet: to_sats(self.rpc.get_balances(wallet)["mine"]["trusted"])
            for wallet in wallets
        }

    def _generate_next_experimental_job(self, flow):
        balances = self._confirmed_balances(flow)
        fee_reserve = self.builder.max_fee_sats
        return generate_experimental_job(
            self.engine,
            flow["id"],
            balances,
            fee_reserve_sats=fee_reserve,
        )

    # ------------------------------------------------------------ flow steps
    def _tick_flow(self, exp_id, flow_id, actions, waits):
        e = self.engine
        flow = e.get_flow(flow_id)
        st = flow["state"]
        if flow["error_state"] != "NONE":
            raise EngineError(f"flow {flow_id} is in error state {flow['error_state']}")
        if st == "START":
            e.advance_flow(flow_id, "PLAN")
            if self._is_experimental(flow):
                actions.append("experimental flow entered progressive planning")
            else:
                n = len(generate_jobs(e, flow_id))
                actions.append(f"planned {n} jobs from the approved config")
        elif st == "PLAN":
            if self._is_experimental(flow):
                jobs = e.list_jobs(flow_id)
                if not jobs or all(j["state"] == "CONFIRMED" for j in jobs):
                    if not self._experimental_complete(flow):
                        jid = self._generate_next_experimental_job(flow)
                        actions.append(
                            f"generated experimental step {self._step(e.get_job(jid))}"
                        )
            r = next_due(e, flow_id)
            if r["job"]:
                e.advance_flow(flow_id, "EXECUTE")
                actions.append(f"step {self._step(r['job'])} is due")
            elif r["wait_s"]:
                waits.append(r["wait_s"])
            else:
                raise EngineError(f"nothing runnable in PLAN: {r['reason']}")
        elif st == "EXECUTE":
            self._execute(flow_id, actions)
        elif st == "CONFIRMATION":
            self._confirm(flow, actions, waits)
        elif st == "NEXT_STATE":
            jobs = e.list_jobs(flow_id)
            if self._is_experimental(flow):
                target = "COMPLETE" if self._experimental_complete(flow) else "PLAN"
            else:
                target = "COMPLETE" if all(j["state"] == "CONFIRMED" for j in jobs) else "PLAN"
            e.advance_flow(flow_id, target)

    @staticmethod
    def _step(job):
        return json.loads(job["planned_json"])["step"] + 1

    def _execute(self, flow_id, actions):
        e = self.engine
        jobs = [j for j in e.list_jobs(flow_id) if j["state"] == "PLANNED"]
        if not jobs:
            raise EngineError("EXECUTE with no planned job")
        job = jobs[0]
        live = e.conn.execute(
            "SELECT 1 FROM action_journal WHERE job_id=? AND status IN ('intent','unknown')",
            (job["id"],)).fetchone()
        if live:
            raise EngineError("an earlier broadcast for this job is unresolved; resolve it first")
        plan = json.loads(job["planned_json"])
        addr = self.rpc.get_new_address(plan["to"], "flowlab-recv")
        prepared = self.builder.build(plan["from"], addr, plan["amount_sats"])
        self.log("PREVIEW\n" + prepared.summary())
        txid = broadcast(e, self.rpc, job["id"], prepared)
        e.advance_flow(flow_id, "CONFIRMATION")
        actions.append(f"step {self._step(job)} broadcast {txid}")

    def _confirm(self, flow, actions, waits):
        e = self.engine
        jobs = e.list_jobs(flow["id"])
        pending = [j for j in jobs if j["state"] == "BROADCAST"]
        if not pending:
            e.advance_flow(flow["id"], "NEXT_STATE")
            return
        job = pending[0]
        plan = json.loads(job["planned_json"])
        info = self.rpc.get_transaction(plan["from"], job["txid"])
        confs = max(0, int(info.get("confirmations", 0)))
        e.record_confirmation(job["txid"], confs, info.get("blockheight"))
        if e.get_job(job["id"])["state"] == "CONFIRMED":
            e.advance_flow(flow["id"], "NEXT_STATE")
            actions.append(f"step {self._step(job)} confirmed ({confs} confirmations)")
        else:
            waits.append(POLL_SECONDS)

    # -------------------------------------------------------- reconciliation
    def reconcile(self, exp_id):
        """Check every transfer against what the node's wallets actually recorded."""
        e = self.engine
        issues, fees, moved, balances, njobs = [], 0, 0, {}, 0
        for flow in e.list_flows(exp_id):
            for job in e.list_jobs(flow["id"]):
                njobs += 1
                plan = json.loads(job["planned_json"])
                recorded = json.loads(job["result_json"] or "{}")
                tag = f"step {plan['step'] + 1}"
                try:
                    sent = self.rpc.get_transaction(plan["from"], job["txid"])
                    got = self.rpc.get_transaction(plan["to"], job["txid"])
                except RpcError as err:
                    issues.append(f"{tag}: cannot read transaction ({err})")
                    continue
                fee = -to_sats(sent.get("fee", 0))
                want = recorded.get("amount_sats") if plan["amount_sats"] == "all" else plan["amount_sats"]
                if want is None or to_sats(got["amount"]) != want:
                    issues.append(f"{tag}: receiver saw {to_sats(got['amount'])} sats, expected {want}")
                if fee != recorded.get("fee_sats"):
                    issues.append(f"{tag}: fee {fee} sats differs from the recorded {recorded.get('fee_sats')}")
                if got.get("confirmations", 0) < flow["confirmations_required"]:
                    issues.append(f"{tag}: below the required confirmations")
                fees += fee
                moved += want or 0
            for w in [flow["source_wallet"], *json.loads(flow["flow_wallets_json"]),
                      flow["destination_wallet"]]:
                balances[w] = to_sats(self.rpc.get_balances(w)["mine"]["trusted"])
        stats = {"jobs": njobs, "total_fees_sats": fees, "total_moved_sats": moved, "issues": issues}
        return (not issues), {"balances_sats": balances}, stats
