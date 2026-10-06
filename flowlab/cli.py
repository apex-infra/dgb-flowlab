"""
cli.py -- the operator's command line.

    new CONFIG.json     create + configure an experiment, print the full review
    review EXP          show the review again
    approve EXP HASH    approve it, once, quoting the config hash from the review
    run EXP             run it start to finish (Ctrl+C is safe; run again to continue)
    status EXP          where it is right now
    list                all experiments
    resume EXP          resume a paused experiment (after you fixed the cause)
    recover EXP         after a crash: show what was interrupted, leave RECOVERY if clean
    resolve ACTION OUTCOME [--txid T] [--evidence TEXT]   settle an interrupted broadcast
    stop                emergency stop: abort everything, keep all history
    watch [EXP] [--once]   live read-only dashboard (newest experiment if none given)
    serve [EXP] [--port N] [--read-only]   the dashboard as a web page on 127.0.0.1 only, with controls (default port 8787)
"""

import argparse
import json
import shutil
import sqlite3
import time

from . import dashboard
from .engine import Engine, EngineError
from .executor import Executor
from .node_verifier import NodeVerifier
from .rpc import RpcClient
from .tx_builder import TxBuilder

DB, LOCAL = "flowlab.db", "config.local.json"


def _load(path):
    with open(path) as f:
        return json.load(f)


ROLE_KEYS = ("reserve", "stage", "workers", "hubs", "destinations")


def _wallet_roles(local):
    """Validate optional dashboard wallet-role metadata.

    The existing wallets array remains the RPC/security allowlist.
    wallet_roles only describes how those allowlisted wallets are intended
    to be used by FlowLab's experiment builder.
    """
    roles = local.get("wallet_roles")
    if roles is None:
        return {}

    if not isinstance(roles, dict):
        raise ValueError("wallet_roles must be an object")

    extra = set(roles) - set(ROLE_KEYS)
    if extra:
        raise ValueError("unknown wallet role(s): " + ", ".join(sorted(extra)))

    allowed = set(local["wallets"])
    out = {}
    assigned = {}

    for role in ROLE_KEYS:
        values = roles.get(role, [])

        if not isinstance(values, list) or not all(
            isinstance(w, str) and w for w in values
        ):
            raise ValueError(f"wallet_roles.{role} must be a list of wallet names")

        if len(values) != len(set(values)):
            raise ValueError(f"wallet_roles.{role} contains duplicates")

        outside = sorted(set(values) - allowed)
        if outside:
            raise ValueError(
                f"wallet_roles.{role} contains wallet(s) outside wallets allowlist: "
                + ", ".join(outside)
            )

        for wallet in values:
            if wallet in assigned:
                raise ValueError(
                    f"{wallet} is assigned to both {assigned[wallet]} and {role}"
                )
            assigned[wallet] = role

        out[role] = list(values)

    for required in ("reserve", "stage", "destinations"):
        if not out[required]:
            raise ValueError(f"wallet_roles.{required} must contain at least one wallet")

    return out


def _node(local, rpc):
    rpc = rpc or RpcClient(local["rpc_user"], local["rpc_password"], local["rpc_port"],
                           local["rpc_host"], allowed_wallets=local["wallets"])
    return rpc, TxBuilder(rpc, local["wallets"], max_fee_sats=local.get("max_fee_sats", 10_000_000))


def _open(db, rpc=None):
    if rpc is None:
        return Engine(db), None
    verifier = NodeVerifier(rpc)
    engine = Engine(db, verifier=verifier)
    verifier.known_txids = lambda flow: [] if not flow else [r[0] for r in engine.conn.execute(
        "SELECT txid FROM flow_txids WHERE flow_id=?", (flow["id"],))]
    return engine, verifier


def main(argv=None, rpc=None, out=print, sleep=time.sleep, db=DB, local_path=LOCAL):
    p = argparse.ArgumentParser(prog="flowctl")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("new"); s.add_argument("config"); s.add_argument("--description", default="")
    for name in ("review", "status", "run", "resume", "recover"):
        sub.add_parser(name).add_argument("exp")
    s = sub.add_parser("approve"); s.add_argument("exp"); s.add_argument("hash"); s.add_argument("--note", default="")
    s = sub.add_parser("resolve"); s.add_argument("action", type=int)
    s.add_argument("outcome", choices=["broadcast", "not_broadcast"])
    s.add_argument("--txid"); s.add_argument("--evidence", default="")
    sub.add_parser("list")
    sub.add_parser("stop")
    s = sub.add_parser("watch"); s.add_argument("exp", nargs="?"); s.add_argument("--once", action="store_true")
    s = sub.add_parser("serve"); s.add_argument("exp", nargs="?"); s.add_argument("--port", type=int, default=8787)
    s.add_argument("--read-only", action="store_true")
    a = p.parse_args(argv)
    if a.cmd in ("watch", "serve"):
        return _watch(a, rpc, out, sleep, db, local_path)

    needs_node = a.cmd in ("run", "resume", "recover")
    builder = None
    if needs_node:
        if rpc is None:
            rpc, builder = _node(_load(local_path), None)
        else:
            builder = TxBuilder(rpc, rpc.wallets, max_fee_sats=10_000_000)
    e, _ = _open(db, rpc if needs_node else None)
    clean = False
    try:
        code = _dispatch(a, e, rpc, builder, out, sleep)
        clean = a.cmd in ("run", "recover")
        return code
    except KeyboardInterrupt:
        out("\nstopped by you. Nothing is lost; run the same command again to continue.")
        clean = True
        return 0
    except EngineError as err:
        out(f"refused: {err}")
        return 1
    finally:
        e.close(clean=clean)


def _watch(a, rpc, out, sleep, db, local_path):
    """watch is read-only. serve shows the same page; unless --read-only it also offers the operator actions."""
    wallets, max_fee, wallet_roles = list(getattr(rpc, "wallets", [])), 10_000_000, {}
    if rpc is None:
        try:
            local = _load(local_path)
            rpc = RpcClient(local["rpc_user"], local["rpc_password"], local["rpc_port"],
                            local["rpc_host"], allowed_wallets=local["wallets"])
            wallets = list(local["wallets"])
            max_fee = local.get("max_fee_sats", 10_000_000)
            wallet_roles = _wallet_roles(local)
        except (OSError, KeyError, ValueError):
            rpc = None
    try:
        if a.cmd == "serve":
            from . import webdash  # late: webctl imports this module
            builder = TxBuilder(rpc, wallets, max_fee_sats=max_fee) if rpc else None
            return webdash.serve(
                db, a.exp, rpc, wallets, a.port, builder, not a.read_only, out,
                wallet_roles=wallet_roles,
            )
        return dashboard.watch(db, a.exp, rpc, wallets, out, sleep, once=a.once,
                               width=shutil.get_terminal_size((100, 30)).columns - 1)
    except sqlite3.OperationalError:
        out("no database yet: create an experiment first (python3 flowctl.py new CONFIG.json)")
        return 1


def _dispatch(a, e, rpc, builder, out, sleep):
    c = a.cmd
    if c == "new":
        exp = e.create_experiment(a.description)
        e.configure_experiment(exp, _load(a.config))
        out(e.review(exp)["text"])
        out(f"\nTo approve (once): python3 flowctl.py approve {exp} {e.get_experiment(exp)['config_hash']}")
    elif c == "review":
        out(e.review(a.exp)["text"])
    elif c == "approve":
        e.approve(a.exp, a.hash, a.note)
        out(f"approved. Start it with: python3 flowctl.py run {a.exp}")
    elif c == "list":
        for r in e.conn.execute("SELECT id, state, description FROM experiments ORDER BY created_at"):
            out(f"{r['id']}  {r['state']:<10} {r['description']}")
    elif c == "status":
        _status(e, a.exp, out)
    elif c == "stop":
        out(f"emergency stop: aborted {e.emergency_stop('operator emergency stop')}")
    elif c == "resolve":
        e.resolve_unknown_action(a.action, a.outcome, a.txid, a.evidence)
        out("recorded")
    elif c == "resume":
        e.recover()
        e.resume(a.exp)
        out(f"resumed. Continue with: python3 flowctl.py run {a.exp}")
    elif c == "recover":
        s = e.recover()
        out(f"previous shutdown clean: {s['clean_shutdown']}   interrupted actions: {s['unknown_actions']}")
        if e.get_experiment(a.exp)["state"] == "RECOVERY":
            e.leave_recovery(a.exp)
            out("verified; recovery finished. Continue with: python3 flowctl.py run " + a.exp)
    elif c == "run":
        return _run(e, rpc, builder, a.exp, out, sleep)
    return 0


def _run(e, rpc, builder, exp, out, sleep):
    s = e.recover()
    st = e.get_experiment(exp)["state"]
    if st == "RECOVERY" or exp in s["moved_to_recovery"]:
        out(f"experiment is in RECOVERY. Run: python3 flowctl.py recover {exp}")
        return 2
    if st == "PAUSED":
        out(f"experiment is PAUSED ({e.get_experiment(exp)['state_reason']}). Fix the cause, then: python3 flowctl.py resume {exp}")
        return 2
    if st == "APPROVED":
        e.start(exp)
    x = Executor(e, rpc, builder, log=out)
    while True:
        r = x.tick(exp)
        if r["done"]:
            out("finished. See: python3 flowctl.py status " + exp)
            return 0
        if r["blocked"]:
            out("STOPPED: " + r["blocked"])
            return 1
        sleep(min(max(r["wait_s"] or 1, 1), 15))


def _status(e, exp, out):
    x = e.get_experiment(exp)
    out(f"{exp}  state={x['state']}  ({x['state_reason']})")
    for f in e.list_flows(exp):
        out(f"flow {f['id']}  {f['source_wallet']} -> {f['destination_wallet']}  state={f['state']}")
        for j in e.list_jobs(f["id"]):
            plan = json.loads(j["planned_json"])
            row = e.conn.execute("SELECT confirmations FROM flow_txids WHERE txid=?", (j["txid"],)).fetchone() if j["txid"] else None
            out(f"  {plan['step'] + 1:>3}. {j['state']:<9} {plan['from']} -> {plan['to']}  {plan['amount_sats']} sats"
                + (f"  tx {j['txid'][:12]}  conf {row[0]}" if row else ""))
    live = e.conn.execute("SELECT id, kind, status FROM action_journal WHERE status IN ('intent','unknown')").fetchall()
    for r in live:
        out(f"UNRESOLVED action {r['id']} ({r['kind']}, {r['status']})")
