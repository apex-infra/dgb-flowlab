"""
dashboard.py -- live, read-only view of an experiment.

It opens the database read-only and (optionally) asks the node for wallet
balances. It cannot change anything: no engine, no approvals, no sending.
render() is a pure function of a snapshot, so it is easy to test.
"""

import json
import sqlite3
import time
from datetime import datetime, timezone

from .rpc import to_sats

C = {"g": "\x1b[32m", "y": "\x1b[33m", "c": "\x1b[36m", "r": "\x1b[31m",
     "d": "\x1b[2m", "b": "\x1b[1m", "0": "\x1b[0m"}
STATE_COLOR = {"CONFIRMED": "g", "BROADCAST": "y", "PLANNED": "c", "COMPLETE": "g", "RUNNING": "g",
               "WAITING": "y", "PAUSED": "r", "ERROR": "r", "ABORTED": "r", "RECOVERY": "r",
               "APPROVED": "c", "IDLE": "d"}
FINISHED = ("COMPLETE", "IDLE", "ABORTED")

SAFE_GENERATED_KEYS = {
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
}


def _iso(s):
    return datetime.fromisoformat(s) if s else None


def dgb(sats):
    sign = "-" if sats < 0 else ""
    sats = abs(int(sats))
    return f"{sign}{sats // 10**8}.{sats % 10**8:08d}"


def snapshot(db_path, exp_id=None, now=None):
    """Everything the screen needs, read straight from the database."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        q = "SELECT * FROM experiments " + ("WHERE id=? " if exp_id else "") + "ORDER BY created_at DESC LIMIT 1"
        exp = con.execute(q, (exp_id,) if exp_id else ()).fetchone()
        if exp is None:
            return None

        cfg = json.loads(exp["config_json"] or "{}")
        rnd = cfg.get("randomization") or {"enabled": False}
        experimental = bool(rnd.get("enabled"))
        workload = cfg.get("workload") or {}
        staged = experimental and any(
            f.get("allocation_wallet")
            for f in cfg.get("flows", [])
            if isinstance(f, dict)
        )

        snap = {
            "exp": dict(exp),
            "flows": [],
            "events": [],
            "next_due_s": None,
            "mode": "experimental" if experimental else "deterministic",
            "staged": staged,
            "staged_accounting": None,
            "target_jobs": (
                workload.get("jobs")
                if experimental and workload.get("mode") == "count"
                else None
            ),
            "randomization": (
                {"model": rnd.get("model"), "seed": rnd.get("seed")}
                if experimental
                else None
            ),
        }
        for f in con.execute("SELECT * FROM flows WHERE experiment_id=? ORDER BY created_at", (exp["id"],)):
            jobs, prev = [], None
            rows = con.execute(
                "SELECT j.*, t.confirmations AS confs, "
                "t.confirmed_at AS confirmed_at "
                "FROM jobs j LEFT JOIN flow_txids t "
                "ON t.txid=j.txid "
                "WHERE j.flow_id=? ORDER BY j.seq",
                (f["id"],),
            ).fetchall()
            for j in rows:
                plan = json.loads(j["planned_json"])
                res = json.loads(j["result_json"] or "{}")
                generated_raw = json.loads(j["generated_from_json"] or "{}")
                generated = {
                    k: generated_raw[k]
                    for k in SAFE_GENERATED_KEYS
                    if k in generated_raw
                }
                jobs.append({
                    "seq": j["seq"],
                    "state": j["state"],
                    "from": plan["from"],
                    "to": plan["to"],
                    "planned": plan["amount_sats"],
                    "amount": res.get("amount_sats"),
                    "fee": res.get("fee_sats"),
                    "txid": j["txid"],
                    "confs": j["confs"],
                    "delay_s": j["planned_delay_s"],
                    "actual_executed_at": j["actual_executed_at"],
                    "confirmed_at": j["confirmed_at"],
                    "generated": generated or None,
                })
                if j["state"] == "PLANNED" and snap["next_due_s"] is None and now is not None:
                    base = _iso(prev["updated_at"] if prev else j["created_at"])
                    snap["next_due_s"] = max(0, int((base - now).total_seconds() + (j["planned_delay_s"] or 0)))
                if j["state"] == "CONFIRMED":
                    prev = j
            snap["flows"].append({"id": f["id"], "state": f["state"], "error": f["error_state"],
                                  "required": f["confirmations_required"], "source": f["source_wallet"],
                                  "wallets": json.loads(f["flow_wallets_json"]),
                                  "dest": f["destination_wallet"], "jobs": jobs})
        if staged:
            all_jobs = [
                job
                for flow in snap["flows"]
                for job in flow["jobs"]
            ]

            allocation_jobs = [
                job for job in all_jobs
                if job["generated"]
                and job["generated"].get("source") == "experimental allocation commit"
            ]

            workload_jobs = [
                job for job in all_jobs
                if job["generated"]
                and isinstance(job["generated"].get("decision_index"), int)
            ]

            finalization_jobs = [
                job for job in all_jobs
                if job["generated"]
                and job["generated"].get("phase") == "finalization"
            ]

            approved_principal = sum(
                f.get("allocation_sats", 0)
                for f in cfg.get("flows", [])
                if isinstance(f, dict) and f.get("allocation_wallet")
            )

            confirmed_allocations = [
                job for job in allocation_jobs
                if job["state"] == "CONFIRMED"
            ]

            committed_principal = (
                approved_principal
                if confirmed_allocations
                else 0
            )

            if any(job["state"] == "CONFIRMED" for job in allocation_jobs):
                commitment_status = "confirmed"
            elif any(job["state"] == "BROADCAST" for job in allocation_jobs):
                commitment_status = "broadcast"
            elif allocation_jobs:
                commitment_status = "planned"
            else:
                commitment_status = "not started"

            commitment_fees = sum(
                job["fee"] or 0
                for job in allocation_jobs
            )

            experiment_fees = sum(
                job["fee"] or 0
                for job in all_jobs
                if not (
                    job["generated"]
                    and job["generated"].get("source")
                    == "experimental allocation commit"
                )
            )

            cumulative_workload = sum(
                job["amount"]
                for job in workload_jobs
                if isinstance(job["amount"], int)
            )

            destination_receipts = sum(
                job["amount"]
                for job in finalization_jobs
                if job["state"] == "CONFIRMED"
                and isinstance(job["amount"], int)
            )

            principal_after_fees = max(
                0,
                committed_principal - experiment_fees,
            )

            accounting_delta = None
            accounting_reconciled = None

            if exp["state"] == "COMPLETE" and committed_principal:
                accounting_delta = (
                    committed_principal
                    - experiment_fees
                    - destination_receipts
                )
                accounting_reconciled = accounting_delta == 0

            snap["staged_accounting"] = {
                "approved_principal_sats": approved_principal,
                "committed_principal_sats": committed_principal,
                "commitment_status": commitment_status,
                "principal_after_fees_sats": principal_after_fees,
                "cumulative_workload_sats": cumulative_workload,
                "experiment_fees_sats": experiment_fees,
                "commitment_fee_sats": commitment_fees,
                "destination_receipts_sats": destination_receipts,
                "accounting_delta_sats": accounting_delta,
                "accounting_reconciled": accounting_reconciled,
            }

        snap["events"] = [dict(r) for r in con.execute(
            "SELECT ts, event, amount_sats, txid, resulting_state FROM audit_log "
            "WHERE experiment_id=? ORDER BY id DESC LIMIT 8", (exp["id"],))]
        return snap
    finally:
        con.close()


def extras(db_path):
    """Read-only facts the control panel needs: experiments, emergency flag, interrupted actions."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        flag = con.execute("SELECT value FROM meta WHERE key='emergency_stop'").fetchone()
        live = []
        for r in con.execute("SELECT id, job_id, kind, status, payload_json FROM action_journal "
                             "WHERE status IN ('intent','unknown') ORDER BY id"):
            pay = json.loads(r["payload_json"])
            live.append({"id": r["id"], "job": r["job_id"], "kind": r["kind"], "status": r["status"],
                         "amount_sats": pay.get("amount_sats"), "txid": pay.get("expected_txid")})
        exps = [dict(r) for r in con.execute(
            "SELECT id, state, description FROM experiments ORDER BY created_at DESC LIMIT 30")]
        return {"emergency": bool(flag and flag[0] == "1"), "unresolved": live, "experiments": exps}
    finally:
        con.close()


def balances(rpc, wallets):
    """{wallet: sats or None}. A node that cannot be reached just shows dashes."""
    out = {}
    for w in wallets:
        try:
            out[w] = to_sats(rpc.get_balances(w)["mine"]["trusted"])
        except Exception:  # noqa: BLE001
            out[w] = None
    return out


def _paint(text, key, color):
    return f"{C[key]}{text}{C['0']}" if color else text


def _bar(done, total, width):
    filled = 0 if total == 0 else round(width * done / total)
    return "█" * filled + "░" * (width - filled)


def render(snap, bal=None, width=100, color=True, now=None):
    if snap is None:
        return "no experiment found"
    e, p = snap["exp"], (lambda t, k: _paint(t, k, color))
    jobs = [j for f in snap["flows"] for j in f["jobs"]]
    done = sum(1 for j in jobs if j["state"] == "CONFIRMED")
    fees = sum(j["fee"] or 0 for j in jobs)
    t0 = _iso(e["started_at"])
    t1 = _iso(e["completed_at"]) or now
    el = int((t1 - t0).total_seconds()) if t0 and t1 else 0
    L = [p(f" DGB FLOWLAB  {e['id']}  ", "b") + p(e["state"], STATE_COLOR.get(e["state"], "d"))
         + p(f"   {e['description']}", "d"),
         p("─" * width, "d"),
         f" progress {p(_bar(done, len(jobs), 30), 'g')} {done}/{len(jobs)} hops"
         f"   fees {dgb(fees)} DGB   elapsed {el // 60:02d}:{el % 60:02d}"
         + (f"   next hop in {snap['next_due_s']}s" if snap["next_due_s"] is not None and e["state"] not in FINISHED else ""),
         ""]
    if e["state_reason"] and e["state"] in ("PAUSED", "ERROR", "ABORTED", "RECOVERY"):
        L.insert(3, " " + p("! " + e["state_reason"], "r"))
    for f in snap["flows"]:
        L.append(p(f" FLOW {' → '.join([f['source'], *f['wallets'], f['dest']])}", "b")
                 + p(f"   [{f['state']}{'' if f['error'] == 'NONE' else ' ' + f['error']}]", "d"))
        L.append(p("  #  state      hop                          amount         fee        conf  txid", "d"))
        for j in f["jobs"]:
            amt = j["amount"] if j["amount"] is not None else j["planned"]
            amt = "ENTIRE BAL" if amt == "all" else dgb(amt)
            conf = "-" if j["confs"] is None else f"{j['confs']}/{f['required']}"
            hop = f"{j['from']} → {j['to']}"
            L.append(f" {j['seq']:>2}  " + p(f"{j['state']:<10}", STATE_COLOR.get(j["state"], "d"))
                     + f" {hop:<28} {amt:>13} {dgb(j['fee']) if j['fee'] else '-':>12} {conf:>6}  "
                     + p((j["txid"] or "")[:12], "d"))
        L.append("")
    if bal:
        total = sum(v for v in bal.values() if v) or 1
        L.append(p(" WALLETS", "b"))
        for w, v in bal.items():
            share = 0 if not v else v / total
            L.append(f"  {w:<12} {'-' if v is None else dgb(v):>14} DGB  " + p(_bar(round(share * 20), 20, 20), "c"))
        L.append("")
    L.append(p(" ACTIVITY", "b"))
    for ev in snap["events"]:
        stamp = ev["ts"][11:19] if ev["ts"] else ""
        extra = f"  {dgb(ev['amount_sats'])} DGB" if isinstance(ev["amount_sats"], int) and ev["amount_sats"] else ""
        L.append(p(f"  {stamp}", "d") + f" {ev['event']}{extra}" + p(f"  {(ev['txid'] or '')[:12]}", "d"))
    return "\n".join(L)


def watch(db_path, exp_id=None, rpc=None, wallets=(), out=print, sleep=time.sleep,
          once=False, width=100, refresh=2, color=True):
    try:
        while True:
            now = datetime.now(timezone.utc)
            snap = snapshot(db_path, exp_id, now)
            frame = render(snap, balances(rpc, wallets) if rpc and wallets else None, width, color, now)
            out(frame if once else "\x1b[H\x1b[J" + frame + "\n\n " + C["d"] + "Ctrl+C to leave (read-only)" + C["0"])
            if once or snap is None or snap["exp"]["state"] in FINISHED:
                return 0 if snap else 1
            sleep(refresh)
    except KeyboardInterrupt:
        return 0
