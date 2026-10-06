"""
engine.py -- the persistent flow / experiment state machine (Phase 1).

What this module does NOT do, by design: talk to a node, hold keys, or
broadcast anything. It is the bookkeeping spine the later phases hang
their real actions on. The properties it enforces:

  1. Configure once, approve once. Approval pins a SHA-256 of the exact
     configuration; the config is then immutable (also enforced by a
     database trigger).
  2. Every state change is table-driven, atomic with its audit row, and
     illegal transitions raise.
  3. Fail closed. Before any action the Verifier must positively report
     every required check; silence, errors and surprises all pause the
     experiment instead of continuing.
  4. Write-ahead journal. An action's intent is persisted BEFORE it is
     attempted. A crash leaves the intent behind; recovery marks it
     'unknown' and a human/reconciler must resolve it. It is never
     retried automatically, and a unique index makes a second live
     action for the same (job, kind) impossible -- so "lost the
     connection right after broadcasting" cannot become a duplicate.
  5. Emergency stop aborts everything, blocks all new actions, and
     destroys nothing: history stays, and already-broadcast
     transactions can still be recorded as they confirm.

Concurrency note: verification (which will do network I/O in Phase 2)
runs OUTSIDE the write transaction; state is re-read and re-checked
inside it, and a change in between aborts the operation.
"""

import json
import secrets
import sqlite3
from datetime import datetime, timezone

from . import audit, config_schema, db, verify
from .states import (ACTIVE_STATES, EXPERIMENT_TRANSITIONS, FLOW_TRANSITIONS,
                     ExperimentState as E, FlowState as F)


class EngineError(Exception):
    pass


class IllegalTransition(EngineError):
    pass


class ApprovalError(EngineError):
    pass


class EmergencyStopActive(EngineError):
    pass


class GuardFailed(EngineError):
    def __init__(self, failures):
        self.failures = failures
        super().__init__("pre-action verification failed: " + "; ".join(failures))


_PAUSEABLE = {E.APPROVED.value, E.RUNNING.value, E.WAITING.value, E.COMPLETING.value}


def _rid(prefix):
    return f"{prefix}-{secrets.token_hex(4).upper()}"


class Engine:
    def __init__(self, path, verifier=None, clock=None):
        self.path = path
        self.verifier = verifier
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.conn = db.connect(path)
        db.init(self.conn)

    # ------------------------------------------------------------------ utils
    def now(self):
        return self._clock().isoformat()

    def close(self, clean=True):
        if clean:
            self.conn.execute("UPDATE meta SET value='1' WHERE key='clean_shutdown'")
        self.conn.close()

    def _one(self, sql, args, what):
        row = self.conn.execute(sql, args).fetchone()
        if row is None:
            raise EngineError(f"unknown {what}")
        return dict(row)

    def get_experiment(self, exp_id):
        return self._one("SELECT * FROM experiments WHERE id=?", (exp_id,), f"experiment {exp_id}")

    def get_flow(self, flow_id):
        return self._one("SELECT * FROM flows WHERE id=?", (flow_id,), f"flow {flow_id}")

    def get_job(self, job_id):
        return self._one("SELECT * FROM jobs WHERE id=?", (job_id,), f"job {job_id}")

    def list_flows(self, exp_id):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM flows WHERE experiment_id=? ORDER BY created_at, id", (exp_id,))]

    def list_jobs(self, flow_id):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM jobs WHERE flow_id=? ORDER BY seq", (flow_id,))]

    def audit_events(self, exp_id=None):
        if exp_id:
            cur = self.conn.execute("SELECT * FROM audit_log WHERE experiment_id=? ORDER BY id", (exp_id,))
        else:
            cur = self.conn.execute("SELECT * FROM audit_log ORDER BY id")
        return [dict(r) for r in cur]

    def emergency_stopped(self):
        return self.conn.execute("SELECT value FROM meta WHERE key='emergency_stop'").fetchone()[0] == "1"

    def _audit(self, event, **kw):
        audit.write(self.conn, self.now(), event, **kw)

    def _live_actions(self, exp_id, statuses=("intent", "unknown")):
        q = ",".join("?" * len(statuses))
        return self.conn.execute(
            f"""SELECT a.id FROM action_journal a JOIN flows f ON f.id=a.flow_id
                WHERE f.experiment_id=? AND a.status IN ({q})""",
            (exp_id, *statuses)).fetchall()

    def _move(self, exp, new_state, reason, event, **audit_kw):
        """State transition inside an open tx. Caller holds the tx."""
        cur, new = E(exp["state"]), E(new_state)
        if new not in EXPERIMENT_TRANSITIONS[cur]:
            raise IllegalTransition(f"experiment {exp['id']}: {cur.value} -> {new.value} is not allowed")
        paused_from = cur.value if new is E.PAUSED else None
        self.conn.execute(
            "UPDATE experiments SET state=?, state_reason=?, paused_from=? WHERE id=?",
            (new.value, reason, paused_from, exp["id"]))
        self._audit(event, experiment_id=exp["id"], state=cur.value, resulting_state=new.value,
                    detail={"reason": reason}, **audit_kw)

    # ------------------------------------------------------------------ guard
    def _guard(self, exp_id, flow_id, action, verifier=None):
        if self.emergency_stopped():
            raise EmergencyStopActive("emergency stop is active")
        exp = self.get_experiment(exp_id)
        flow = self.get_flow(flow_id) if flow_id else None
        failures = verify.evaluate(verifier or self.verifier, exp, flow, action)
        if not failures:
            return
        with db.tx(self.conn):
            exp = self.get_experiment(exp_id)
            reason = f"guard failed on '{action}': " + "; ".join(failures)
            if exp["state"] in _PAUSEABLE:
                self._move(exp, E.PAUSED, reason, "GUARD FAILED - EXPERIMENT PAUSED", flow_id=flow_id)
            else:
                self._audit("GUARD FAILED", experiment_id=exp_id, flow_id=flow_id,
                            state=exp["state"], detail={"failures": failures, "action": action})
        raise GuardFailed(failures)

    # --------------------------------------------------------------- lifecycle
    def create_experiment(self, description="", tags=(), notes=""):
        exp_id = _rid("EXP")
        with db.tx(self.conn):
            self.conn.execute(
                """INSERT INTO experiments(id, description, tags_json, operator_notes, state, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (exp_id, description, json.dumps(list(tags)), notes, E.CREATED.value, self.now()))
            self._audit("EXPERIMENT CREATED", experiment_id=exp_id, resulting_state=E.CREATED.value,
                        detail={"description": description, "tags": list(tags)})
        return exp_id

    def configure_experiment(self, exp_id, cfg):
        cfg = config_schema.validate(cfg)
        with db.tx(self.conn):
            exp = self.get_experiment(exp_id)
            if exp["state"] not in (E.CREATED.value, E.CONFIGURED.value):
                raise IllegalTransition(f"cannot configure an experiment in state {exp['state']}")
            h = config_schema.config_hash(cfg)
            seed = cfg["randomization"].get("seed") if cfg["randomization"]["enabled"] else None
            self.conn.execute(
                """UPDATE experiments SET config_json=?, config_hash=?, config_version=?,
                          random_seed=?, configured_at=? WHERE id=?""",
                (config_schema.canonical(cfg), h, exp["config_version"] + 1, seed, self.now(), exp_id))
            exp = self.get_experiment(exp_id)
            self._move(exp, E.CONFIGURED, f"configuration v{exp['config_version']}",
                       "EXPERIMENT CONFIGURED")
        return h

    def review(self, exp_id):
        """Everything the operator must see before approving (spec: display the complete configuration)."""
        exp = self.get_experiment(exp_id)
        if not exp["config_json"]:
            raise EngineError("experiment has no configuration yet")
        cfg = json.loads(exp["config_json"])
        lines = [
            f"EXPERIMENT {exp_id}   state={exp['state']}   config v{exp['config_version']}",
            f"config hash: {exp['config_hash']}",
            f"description: {exp['description']}",
            f"tags: {', '.join(json.loads(exp['tags_json'])) or '-'}",
            f"operator notes: {exp['operator_notes'] or '-'}",
            "",
            "INITIAL PARAMETERS (fixed once approved)",
        ]
        for i, fl in enumerate(cfg["flows"], 1):
            path = " -> ".join([fl["source_wallet"], *fl["flow_wallets"], fl["destination_wallet"]])
            lines.append(f"  flow {i}: {path}   allocation {fl['allocation_sats']} sats")
            for k, t in enumerate(fl.get("transfers", []), 1):
                amt = "ENTIRE BALANCE minus fee" if t["amount_sats"] == "all" else f"{t['amount_sats']} sats"
                lines.append(f"    {k:>3}. {t['from']} -> {t['to']}   {amt}   wait {t['delay_seconds']}s")

            topology = fl.get("experimental_topology")
            if topology:
                lines.append("    experimental topology:")
                for k, t in enumerate(topology["transitions"], 1):
                    lines.append(f"      {k:>3}. {t['from']} -> {t['to']}")

                finalization = cfg.get("finalization")
                if finalization:
                    lines.append(
                        f"    finalization: {finalization['mode']}"
                    )
                    for worker in fl["flow_wallets"]:
                        lines.append(
                            f"      {worker} -> {fl['destination_wallet']}   "
                            "ENTIRE BALANCE minus fee"
                        )
                    lines.append(
                        f"      source {fl['source_wallet']} is not swept"
                    )
        wl = cfg.get("workload")
        if wl:
            lines += [
                f"  workload: {wl['mode']}"
                + (f" ({wl['jobs']} jobs)" if wl["mode"] == "count" else "")
                + (f" ({wl['duration_seconds']}s window)" if wl["mode"] == "duration" else ""),
                f"  amount bounds: {wl['amount_sats_min']}..{wl['amount_sats_max']} sats",
                f"  delay bounds: {wl['delay_seconds_min']}..{wl['delay_seconds_max']} s",
            ]
        lines += [
            f"  address policy: {cfg['address_policy']}",
            f"  confirmations required: {cfg['confirmations_required']}",
            f"  fee policy: {json.dumps(cfg['fee_policy'], sort_keys=True)}",
            "  randomization: "
            + (f"ENABLED model={cfg['randomization']['model']} seed={cfg['randomization']['seed']}"
               if cfg["randomization"]["enabled"] else "disabled"),
            "",
            "AUTOMATIC EXECUTION: after approval, no further per-transaction approval is requested.",
        ]
        return {"experiment_id": exp_id, "config": cfg, "config_hash": exp["config_hash"],
                "config_version": exp["config_version"], "text": "\n".join(lines)}

    def approve(self, exp_id, config_hash, note=""):
        """The one explicit approval. Must quote the hash of the configuration that was reviewed."""
        with db.tx(self.conn):
            exp = self.get_experiment(exp_id)
            if exp["state"] != E.CONFIGURED.value:
                raise ApprovalError(f"only a CONFIGURED experiment can be approved (is {exp['state']})")
            if exp["approved_at"]:
                raise ApprovalError("already approved")
            if config_hash != exp["config_hash"]:
                raise ApprovalError("approval hash does not match the current configuration; re-review it")
            cfg = json.loads(exp["config_json"])
            rnd = cfg["randomization"]
            now = self.now()
            for fl in cfg["flows"]:
                fid = _rid("FLOW")
                self.conn.execute(
                    """INSERT INTO flows(id, experiment_id, description, source_wallet, flow_wallets_json,
                              destination_wallet, initial_alloc_sats, state, confirmations_required,
                              randomization_json, random_seed, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (fid, exp_id, fl["description"], fl["source_wallet"], json.dumps(fl["flow_wallets"]),
                     fl["destination_wallet"], fl["allocation_sats"], F.START.value,
                     cfg["confirmations_required"], json.dumps(rnd, sort_keys=True),
                     rnd.get("seed") if rnd["enabled"] else None, now, now))
                self._audit("FLOW CREATED", experiment_id=exp_id, flow_id=fid, resulting_state=F.START.value)
            self.conn.execute("UPDATE experiments SET approved_at=?, approval_note=? WHERE id=?",
                              (now, note, exp_id))
            exp = self.get_experiment(exp_id)
            self._move(exp, E.APPROVED, "operator approval", "EXPERIMENT APPROVED")

    def start(self, exp_id):
        self._guard(exp_id, None, "start")
        with db.tx(self.conn):
            exp = self.get_experiment(exp_id)
            self._move(exp, E.RUNNING, "started", "EXPERIMENT STARTED")
            self.conn.execute("UPDATE experiments SET started_at=? WHERE id=?", (self.now(), exp_id))

    def wait(self, exp_id, reason="scheduled delay"):
        with db.tx(self.conn):
            self._move(self.get_experiment(exp_id), E.WAITING, reason, "EXPERIMENT WAITING")

    def wake(self, exp_id):
        self._guard(exp_id, None, "wake")
        with db.tx(self.conn):
            self._move(self.get_experiment(exp_id), E.RUNNING, "wake", "EXPERIMENT WOKE")

    def pause(self, exp_id, reason="operator pause"):
        with db.tx(self.conn):
            self._move(self.get_experiment(exp_id), E.PAUSED, reason, "EXPERIMENT PAUSED")

    def resume(self, exp_id):
        exp = self.get_experiment(exp_id)
        if exp["state"] != E.PAUSED.value:
            raise IllegalTransition(f"cannot resume from {exp['state']}")
        if self._live_actions(exp_id):
            raise EngineError("unresolved actions exist; recover and resolve them first")
        self._guard(exp_id, None, "resume")
        with db.tx(self.conn):
            exp = self.get_experiment(exp_id)
            target = exp["paused_from"] or E.RUNNING.value
            self._move(exp, target, "resumed", "EXPERIMENT RESUMED")

    # ------------------------------------------------------------------- flows
    def advance_flow(self, flow_id, to_state):
        to = F(to_state)
        flow = self.get_flow(flow_id)
        exp = self.get_experiment(flow["experiment_id"])
        if exp["state"] != E.RUNNING.value:
            raise EngineError(f"experiment must be RUNNING to advance a flow (is {exp['state']})")
        if flow["error_state"] != "NONE":
            raise EngineError(f"flow is in error state {flow['error_state']}")
        if to not in FLOW_TRANSITIONS[F(flow["state"])]:
            raise IllegalTransition(f"flow {flow_id}: {flow['state']} -> {to.value} is not allowed")
        self._guard(exp["id"], flow_id, f"advance:{to.value}")
        with db.tx(self.conn):
            flow2 = self.get_flow(flow_id)
            exp2 = self.get_experiment(exp["id"])
            if flow2["state"] != flow["state"] or exp2["state"] != E.RUNNING.value:
                raise EngineError("state changed during verification; nothing was done")
            now = self.now()
            self.conn.execute(
                "UPDATE flows SET state=?, updated_at=?, completed_at=? WHERE id=?",
                (to.value, now, now if to is F.COMPLETE else None, flow_id))
            self._audit("FLOW ADVANCED", experiment_id=exp["id"], flow_id=flow_id,
                        state=flow["state"], resulting_state=to.value)

    def flow_error(self, flow_id, detail):
        with db.tx(self.conn):
            flow = self.get_flow(flow_id)
            self.conn.execute("UPDATE flows SET error_state='ERROR', error_detail=?, updated_at=? WHERE id=?",
                              (detail, self.now(), flow_id))
            self._audit("FLOW ERROR", experiment_id=flow["experiment_id"], flow_id=flow_id,
                        state=flow["state"], detail={"error": detail})
            exp = self.get_experiment(flow["experiment_id"])
            if exp["state"] in _PAUSEABLE:
                self._move(exp, E.PAUSED, f"flow {flow_id} error: {detail}", "EXPERIMENT PAUSED", flow_id=flow_id)

    def flow_resolve_error(self, flow_id, note):
        flow = self.get_flow(flow_id)
        if flow["error_state"] == "NONE":
            raise EngineError("flow has no error to resolve")
        self._guard(flow["experiment_id"], flow_id, "flow_resolve")
        with db.tx(self.conn):
            self.conn.execute(
                "UPDATE flows SET error_state='NONE', recovery_detail=?, updated_at=? WHERE id=?",
                (note, self.now(), flow_id))
            self._audit("FLOW ERROR RESOLVED", experiment_id=flow["experiment_id"], flow_id=flow_id,
                        detail={"note": note})

    # -------------------------------------------------------------------- jobs
    def add_job(self, flow_id, planned, planned_delay_s=None, generated_from=None, depends_on=()):
        flow = self.get_flow(flow_id)
        exp = self.get_experiment(flow["experiment_id"])
        if exp["state"] != E.RUNNING.value:
            raise EngineError("jobs can only be generated while RUNNING")
        if flow["state"] != F.PLAN.value or flow["error_state"] != "NONE":
            raise EngineError("jobs can only be generated while the flow is in PLAN with no error")
        if audit.has_secret_keys(planned) or audit.has_secret_keys(generated_from or {}):
            raise EngineError("job data must not contain credentials or key material")
        with db.tx(self.conn):
            for dep in depends_on:
                d = self.get_job(dep)
                if d["flow_id"] != flow_id:
                    raise EngineError("a job may only depend on jobs in its own flow")
            seq = self.conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM jobs WHERE flow_id=?",
                                    (flow_id,)).fetchone()[0]
            job_id, now = _rid("JOB"), self.now()
            self.conn.execute(
                """INSERT INTO jobs(id, flow_id, seq, planned_json, generated_from_json,
                          planned_delay_s, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)""",
                (job_id, flow_id, seq, json.dumps(planned, sort_keys=True),
                 json.dumps(generated_from or {}, sort_keys=True), planned_delay_s, now, now))
            for dep in depends_on:
                self.conn.execute("INSERT INTO job_dependencies VALUES (?,?)", (job_id, dep))
            self._audit("JOB GENERATED", experiment_id=exp["id"], flow_id=flow_id, job_id=job_id,
                        amount_sats=planned.get("amount_sats"), resulting_state="PLANNED",
                        detail={"seq": seq, "planned_delay_s": planned_delay_s, "generated_from": generated_from})
        return job_id

    # ----------------------------------------------------- action journal / io
    def begin_action(self, job_id, kind, payload):
        """Persist intent BEFORE doing the thing. Returns an action id."""
        job = self.get_job(job_id)
        flow = self.get_flow(job["flow_id"])
        exp = self.get_experiment(flow["experiment_id"])
        if exp["state"] != E.RUNNING.value:
            raise EngineError(f"experiment must be RUNNING to act (is {exp['state']})")
        if flow["error_state"] != "NONE":
            raise EngineError("flow is in an error state")
        if kind == "broadcast":
            if flow["state"] != F.EXECUTE.value:
                raise EngineError("broadcast requires the flow to be in EXECUTE")
            if job["state"] != "PLANNED":
                raise EngineError(f"job is {job['state']}, not PLANNED")
        unmet = self.conn.execute(
            """SELECT d.depends_on_job_id FROM job_dependencies d JOIN jobs j ON j.id=d.depends_on_job_id
               WHERE d.job_id=? AND j.state!='CONFIRMED'""", (job_id,)).fetchall()
        if unmet:
            raise EngineError("job dependencies are not confirmed yet")
        if audit.has_secret_keys(payload):
            raise EngineError("action payload must not contain credentials or key material")
        self._guard(exp["id"], flow["id"], f"begin:{kind}")
        try:
            with db.tx(self.conn):
                if self.get_experiment(exp["id"])["state"] != E.RUNNING.value:
                    raise EngineError("state changed during verification; nothing was done")
                cur = self.conn.execute(
                    """INSERT INTO action_journal(flow_id, job_id, kind, payload_json, status, created_at)
                       VALUES (?,?,?,?, 'intent', ?)""",
                    (flow["id"], job_id, kind, json.dumps(payload, sort_keys=True), self.now()))
                action_id = cur.lastrowid
                self._audit("ACTION INTENT", experiment_id=exp["id"], flow_id=flow["id"], job_id=job_id,
                            amount_sats=payload.get("amount_sats"), address=payload.get("address"),
                            wallet=payload.get("wallet"), detail={"kind": kind, "action_id": action_id})
        except sqlite3.IntegrityError as e:
            raise EngineError(f"a live '{kind}' action already exists for {job_id}; refusing duplicate") from e
        return action_id

    def _action(self, action_id):
        return self._one("SELECT * FROM action_journal WHERE id=?", (action_id,), f"action {action_id}")

    def complete_action(self, action_id, result):
        """Record that the action happened. Always allowed (recording reality), even after a stop."""
        act = self._action(action_id)
        if act["status"] != "intent":
            raise EngineError(f"action is {act['status']}, not 'intent'")
        if audit.has_secret_keys(result):
            raise EngineError("action result must not contain credentials or key material")
        flow = self.get_flow(act["flow_id"])
        with db.tx(self.conn):
            now = self.now()
            self.conn.execute(
                "UPDATE action_journal SET status='done', result_json=?, finished_at=? WHERE id=?",
                (json.dumps(redact_safe(result), sort_keys=True), now, action_id))
            txid = result.get("txid")
            if txid:
                self.conn.execute(
                    "INSERT INTO flow_txids(txid, flow_id, job_id, recorded_at) VALUES (?,?,?,?)",
                    (txid, act["flow_id"], act["job_id"], now))
                self.conn.execute(
                    """UPDATE jobs SET state='BROADCAST', txid=?, actual_executed_at=?, result_json=?,
                              updated_at=? WHERE id=?""",
                    (txid, now, json.dumps(redact_safe(result), sort_keys=True), now, act["job_id"]))
                self._audit("TRANSACTION BROADCAST", experiment_id=flow["experiment_id"], flow_id=act["flow_id"],
                            job_id=act["job_id"], txid=txid, resulting_state="BROADCAST")
            else:
                self._audit("ACTION DONE", experiment_id=flow["experiment_id"], flow_id=act["flow_id"],
                            job_id=act["job_id"], detail={"kind": act["kind"], "action_id": action_id})

    def fail_action(self, action_id, error):
        """Mark an action as DEFINITELY not performed. If that is uncertain, leave it as 'intent'."""
        act = self._action(action_id)
        if act["status"] != "intent":
            raise EngineError(f"action is {act['status']}, not 'intent'")
        flow = self.get_flow(act["flow_id"])
        with db.tx(self.conn):
            self.conn.execute(
                "UPDATE action_journal SET status='failed', result_json=?, finished_at=? WHERE id=?",
                (json.dumps({"error": error}), self.now(), action_id))
            self._audit("ACTION FAILED", experiment_id=flow["experiment_id"], flow_id=act["flow_id"],
                        job_id=act["job_id"], detail={"kind": act["kind"], "error": error})

    def resolve_unknown_action(self, action_id, outcome, txid=None, evidence=""):
        """A human / reconciler decides what an interrupted action really did."""
        act = self._action(action_id)
        if act["status"] != "unknown":
            raise EngineError(f"action is {act['status']}, not 'unknown'")
        if outcome not in ("broadcast", "not_broadcast"):
            raise EngineError("outcome must be 'broadcast' or 'not_broadcast'")
        if outcome == "broadcast" and not txid:
            raise EngineError("outcome 'broadcast' requires the txid")
        flow = self.get_flow(act["flow_id"])
        with db.tx(self.conn):
            now = self.now()
            if outcome == "broadcast":
                self.conn.execute(
                    "UPDATE action_journal SET status='done', result_json=?, finished_at=? WHERE id=?",
                    (json.dumps({"txid": txid, "resolved": evidence}), now, action_id))
                self.conn.execute(
                    "INSERT OR IGNORE INTO flow_txids(txid, flow_id, job_id, recorded_at) VALUES (?,?,?,?)",
                    (txid, act["flow_id"], act["job_id"], now))
                self.conn.execute(
                    "UPDATE jobs SET state='BROADCAST', txid=?, actual_executed_at=?, updated_at=? WHERE id=?",
                    (txid, now, now, act["job_id"]))
            else:
                self.conn.execute(
                    "UPDATE action_journal SET status='failed', result_json=?, finished_at=? WHERE id=?",
                    (json.dumps({"resolved": evidence}), now, action_id))
            self._audit("UNKNOWN ACTION RESOLVED", experiment_id=flow["experiment_id"], flow_id=act["flow_id"],
                        job_id=act["job_id"], txid=txid, detail={"outcome": outcome, "evidence": evidence})

    def record_confirmation(self, txid, confirmations, block_height=None):
        """Monitoring bookkeeping. Allowed in any state, including after an emergency stop."""
        row = self._one("SELECT * FROM flow_txids WHERE txid=?", (txid,), f"txid {txid}")
        flow = self.get_flow(row["flow_id"])
        with db.tx(self.conn):
            now = self.now()
            self.conn.execute(
                """UPDATE flow_txids SET confirmations=?, block_height=COALESCE(?, block_height),
                          confirmed_at=CASE WHEN ?>=? AND confirmed_at IS NULL THEN ? ELSE confirmed_at END
                   WHERE txid=?""",
                (confirmations, block_height, confirmations, flow["confirmations_required"], now, txid))
            if confirmations >= flow["confirmations_required"] and row["job_id"]:
                self.conn.execute(
                    "UPDATE jobs SET state='CONFIRMED', updated_at=? WHERE id=? AND state='BROADCAST'",
                    (now, row["job_id"]))
            self._audit("CONFIRMATION RECEIVED", experiment_id=flow["experiment_id"], flow_id=flow["id"],
                        job_id=row["job_id"], txid=txid,
                        detail={"confirmations": confirmations, "block_height": block_height})

    # ----------------------------------------------- stop / recovery / completion
    def emergency_stop(self, reason="emergency stop"):
        """Abort everything, block new actions, keep all history."""
        stopped = []
        with db.tx(self.conn):
            self.conn.execute("UPDATE meta SET value='1' WHERE key='emergency_stop'")
            for row in self.conn.execute("SELECT * FROM experiments").fetchall():
                exp = dict(row)
                if exp["state"] in (E.COMPLETE.value, E.IDLE.value, E.ABORTED.value):
                    continue
                self._move(exp, E.ABORTED, reason, "EXPERIMENT ABORTED (EMERGENCY STOP)")
                stopped.append(exp["id"])
            self._audit("EMERGENCY STOP", detail={"reason": reason, "experiments": stopped})
        return stopped

    def clear_emergency_stop(self, note):
        with db.tx(self.conn):
            self.conn.execute("UPDATE meta SET value='0' WHERE key='emergency_stop'")
            self._audit("EMERGENCY STOP CLEARED", detail={"note": note})

    def recover(self):
        """Call once at every process start, before doing anything else."""
        summary = {"clean_shutdown": False, "unknown_actions": [], "moved_to_recovery": []}
        with db.tx(self.conn):
            clean = self.conn.execute("SELECT value FROM meta WHERE key='clean_shutdown'").fetchone()[0] == "1"
            self.conn.execute("UPDATE meta SET value='0' WHERE key='clean_shutdown'")
            summary["clean_shutdown"] = clean
            for row in self.conn.execute("SELECT * FROM action_journal WHERE status='intent'").fetchall():
                self.conn.execute("UPDATE action_journal SET status='unknown' WHERE id=?", (row["id"],))
                summary["unknown_actions"].append(row["id"])
                flow = self.get_flow(row["flow_id"])
                self._audit("ACTION MARKED UNKNOWN (interrupted)", experiment_id=flow["experiment_id"],
                            flow_id=row["flow_id"], job_id=row["job_id"],
                            detail={"kind": row["kind"], "action_id": row["id"]})
            for row in self.conn.execute("SELECT * FROM experiments").fetchall():
                exp = dict(row)
                if exp["state"] not in {s.value for s in ACTIVE_STATES}:
                    continue
                has_unknown = bool(self._live_actions(exp["id"], ("unknown",)))
                if clean and not has_unknown:
                    continue
                why = "unresolved interrupted action" if has_unknown else "unclean shutdown"
                self._move(exp, E.RECOVERY, f"restart recovery: {why}", "EXPERIMENT ENTERED RECOVERY")
                summary["moved_to_recovery"].append(exp["id"])
            self._audit("RESTART RECOVERY CHECK", detail=summary)
        return summary

    def leave_recovery(self, exp_id):
        exp = self.get_experiment(exp_id)
        if exp["state"] != E.RECOVERY.value:
            raise IllegalTransition(f"experiment is {exp['state']}, not RECOVERY")
        if self._live_actions(exp_id):
            raise EngineError("unresolved interrupted actions remain; resolve each one first")
        if self.conn.execute("SELECT 1 FROM flows WHERE experiment_id=? AND error_state!='NONE'",
                             (exp_id,)).fetchone():
            raise EngineError("a flow is still in an error state")
        self._guard(exp_id, None, "leave_recovery")
        with db.tx(self.conn):
            self._move(self.get_experiment(exp_id), E.RUNNING, "recovery verified", "EXPERIMENT LEFT RECOVERY")

    def begin_completion(self, exp_id):
        self._guard(exp_id, None, "begin_completion")
        with db.tx(self.conn):
            self._move(self.get_experiment(exp_id), E.COMPLETING, "completion criteria met",
                       "EXPERIMENT COMPLETING")

    def complete_experiment(self, exp_id, reconciliation_ok, final_state=None, stats=None):
        """True on COMPLETE. A failed reconciliation moves to ERROR (review) and returns False."""
        exp = self.get_experiment(exp_id)
        if exp["state"] != E.COMPLETING.value:
            raise IllegalTransition(f"experiment is {exp['state']}, not COMPLETING")
        if self._live_actions(exp_id):
            raise EngineError("unresolved actions exist")
        flows = self.list_flows(exp_id)
        if any(f["state"] != F.COMPLETE.value for f in flows):
            raise EngineError("every flow must be COMPLETE first")
        short = self.conn.execute(
            """SELECT t.txid FROM flow_txids t JOIN flows f ON f.id=t.flow_id
               WHERE f.experiment_id=? AND t.confirmations < f.confirmations_required""", (exp_id,)).fetchall()
        if short:
            raise EngineError(f"{len(short)} transaction(s) still below the required confirmations")
        with db.tx(self.conn):
            exp = self.get_experiment(exp_id)
            if not reconciliation_ok:
                self._move(exp, E.ERROR, "reconciliation failed: ERROR / REVIEW",
                           "RECONCILIATION FAILED")
                return False
            self._move(exp, E.COMPLETE, "reconciled", "EXPERIMENT COMPLETED")
            self.conn.execute(
                "UPDATE experiments SET completed_at=?, final_state_json=?, stats_json=? WHERE id=?",
                (self.now(), json.dumps(final_state or {}, sort_keys=True),
                 json.dumps(stats or {}, sort_keys=True), exp_id))
        return True

    def go_idle(self, exp_id):
        with db.tx(self.conn):
            self._move(self.get_experiment(exp_id), E.IDLE, "idle", "EXPERIMENT IDLE")


def redact_safe(obj):
    return audit.redact(obj)
