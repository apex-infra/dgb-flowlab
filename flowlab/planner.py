"""
planner.py -- turns the operator's explicit transfer list into jobs.

Nothing here chooses anything. Every hop (from, to, amount, delay) is written
by the operator in the approved configuration; the planner only creates the
jobs in that exact order, each depending on the previous one being CONFIRMED,
and reports which one (if any) is allowed to run now.
"""

import json
from datetime import datetime, timedelta


class PlanError(Exception):
    pass


def transfers_for(engine, flow_id):
    flow = engine.get_flow(flow_id)
    cfg = json.loads(engine.get_experiment(flow["experiment_id"])["config_json"])
    match = [f for f in cfg["flows"] if f["source_wallet"] == flow["source_wallet"]
             and f["destination_wallet"] == flow["destination_wallet"]]
    if len(match) != 1:
        raise PlanError("cannot match this flow to exactly one flow in the approved config")
    transfers = match[0].get("transfers")
    if not transfers:
        raise PlanError("this flow has no explicit transfers in its approved config")
    return transfers


def generate_jobs(engine, flow_id):
    """Create one job per configured transfer, in order. Returns the job ids."""
    if engine.list_jobs(flow_id):
        raise PlanError("this flow already has jobs; refusing to plan it twice")
    ids = []
    for i, t in enumerate(transfers_for(engine, flow_id)):
        ids.append(engine.add_job(
            flow_id,
            {"from": t["from"], "to": t["to"], "amount_sats": t["amount_sats"], "step": i},
            planned_delay_s=t["delay_seconds"],
            generated_from={"step": i, "source": "approved config"},
            depends_on=[ids[-1]] if ids else ()))
    return ids


def next_due(engine, flow_id):
    """Strictly one job at a time, in order. Returns {"job", "wait_s", "reason"}."""
    jobs = engine.list_jobs(flow_id)
    now = datetime.fromisoformat(engine.now())
    prev = None
    for j in jobs:
        if j["state"] == "CONFIRMED":
            prev = j
            continue
        if j["state"] != "PLANNED":
            return {"job": None, "wait_s": None, "reason": f"job {j['seq']} is {j['state']}"}
        base = datetime.fromisoformat(prev["updated_at"] if prev else j["created_at"])
        ready = base + timedelta(seconds=j["planned_delay_s"] or 0)
        wait = max(0, int((ready - now).total_seconds() + 0.999))
        if wait > 0:
            return {"job": None, "wait_s": wait, "reason": f"job {j['seq']} not due yet"}
        return {"job": j, "wait_s": 0, "reason": "due"}
    return {"job": None, "wait_s": None, "reason": "all jobs confirmed"}
