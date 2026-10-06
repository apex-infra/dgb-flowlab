"""
planner.py -- turns the operator's explicit transfer list into jobs.

Nothing here chooses anything. Every hop (from, to, amount, delay) is written
by the operator in the approved configuration; the planner only creates the
jobs in that exact order, each depending on the previous one being CONFIRMED,
and reports which one (if any) is allowed to run now.
"""

import hashlib
import json
import random
from datetime import datetime, timedelta


GENERATOR_VERSION = 1


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


def experimental_decision(engine, flow_id, decision_index):
    """Return one reproducible experimental amount/delay decision.

    This is deliberately pure planning logic: it does not create a job,
    inspect live UTXOs, call RPC, or broadcast anything.
    """
    if not isinstance(decision_index, int) or isinstance(decision_index, bool) or decision_index < 0:
        raise PlanError("decision_index must be a non-negative integer")

    flow = engine.get_flow(flow_id)
    cfg = json.loads(engine.get_experiment(flow["experiment_id"])["config_json"])

    matches = [
        (i, f) for i, f in enumerate(cfg["flows"])
        if f["source_wallet"] == flow["source_wallet"]
        and f["destination_wallet"] == flow["destination_wallet"]
    ]
    if len(matches) != 1:
        raise PlanError("cannot match this flow to exactly one flow in the approved config")

    flow_index, _ = matches[0]
    rnd = cfg["randomization"]
    if not rnd.get("enabled"):
        raise PlanError("experimental decision requested but randomization is disabled")
    if rnd.get("model") not in ("uniform", "seeded_deterministic"):
        raise PlanError(f"randomization model {rnd.get('model')!r} is not implemented by generator v1")

    workload = cfg.get("workload")
    if not workload:
        raise PlanError("experimental decision requires a workload envelope")

    material = (
        f"flowlab-generator-v{GENERATOR_VERSION}:"
        f"{rnd['seed']}:{flow_index}:{decision_index}"
    ).encode()
    derived_seed = int.from_bytes(hashlib.sha256(material).digest(), "big")
    rng = random.Random(derived_seed)

    amount = rng.randint(
        workload["amount_sats_min"],
        workload["amount_sats_max"],
    )
    delay = rng.randint(
        workload["delay_seconds_min"],
        workload["delay_seconds_max"],
    )

    return {
        "amount_sats": amount,
        "delay_seconds": delay,
        "generated_from": {
            "source": "seeded experimental generator",
            "generator_version": GENERATOR_VERSION,
            "model": rnd["model"],
            "seed": rnd["seed"],
            "flow_index": flow_index,
            "decision_index": decision_index,
        },
    }


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
