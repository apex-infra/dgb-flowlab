"""
webctl.py -- every operator action the dashboard offers, as plain methods.

These are the same engine calls the command line makes (new, approve, run, stop,
resume, recover, resolve), with the same checks: one-time approval pinned to the
config hash, fail-closed verification, write-ahead journal. Nothing here can
bypass them. At most one run is active at a time, in a background thread.
"""

import threading
import time
from collections import deque

from .cli import _open, _run
from .config_schema import validate

FINISHED = ("COMPLETE", "IDLE", "ABORTED")
# The dashboard only builds explicit, deterministic flows: any other key is refused, not ignored.
TOP_KEYS = {"flows", "confirmations_required", "fee_policy", "address_policy"}
FLOW_KEYS = {"description", "source_wallet", "flow_wallets", "destination_wallet", "allocation_sats",
             "repeat", "transfers"}


class ControlError(Exception):
    pass


class _Halt(Exception):
    pass


def _text(body, key, required=True, limit=500):
    v = body.get(key, "")
    if not isinstance(v, str) or (required and not v.strip()) or len(v) > limit:
        raise ControlError(f"'{key}' is required" if required else f"'{key}' is invalid")
    return v.strip()


class Controller:
    def __init__(self, db, rpc=None, builder=None, wallets=(), sleep=None, control=True):
        self.db, self.rpc, self.builder = db, rpc, builder
        self.wallets, self.control, self._sleep = list(wallets), control, sleep
        self._lock, self._halt = threading.Lock(), threading.Event()
        self._thread, self.exp = None, None
        self.log = deque(maxlen=300)
        self.actions = {"review": self.review, "new": self.new, "approve": self.approve, "run": self.run,
                        "stop": self.stop, "halt": self.halt, "clear_stop": self.clear_stop, "resume": self.resume,
                        "recover": self.recover, "resolve": self.resolve}

    # ----------------------------------------------------------------- plumbing
    def say(self, line):
        self.log.append({"t": time.strftime("%H:%M:%S"), "line": str(line)})

    def active(self):
        return bool(self._thread and self._thread.is_alive())

    def _with_engine(self, fn, node=False):
        if node and not (self.rpc and self.builder):
            raise ControlError("the node is not connected (check config.local.json)")
        e = _open(self.db, self.rpc if node else None)[0]
        try:
            return fn(e)
        finally:
            e.close(clean=False)

    def _wait(self, seconds):
        if self._halt.is_set():
            raise _Halt()
        if self._sleep:
            self._sleep(seconds)
        elif self._halt.wait(seconds):
            raise _Halt()

    def _not_while_running(self):
        if self.active():
            raise ControlError("a run is active; stop it first")

    # ------------------------------------------------------------------ actions
    def review(self, body):
        exp = _text(body, "exp")
        return self._with_engine(lambda e: {"exp": exp, "text": e.review(exp)["text"],
                                            "hash": e.get_experiment(exp)["config_hash"]})

    def new(self, body):
        cfg = body.get("config")
        if not isinstance(cfg, dict):
            raise ControlError("'config' must be an object")
        extra = set(cfg) - TOP_KEYS
        flows = cfg.get("flows") if isinstance(cfg.get("flows"), list) else []
        for fl in flows:
            extra |= set(fl) - FLOW_KEYS if isinstance(fl, dict) else set()
        if extra:
            raise ControlError("not supported here: " + ", ".join(sorted(extra)))
        cfg = validate(cfg)

        def go(e):
            exp = e.create_experiment(_text(body, "description", required=False, limit=200))
            h = e.configure_experiment(exp, cfg)
            return {"exp": exp, "hash": h, "text": e.review(exp)["text"]}
        return self._with_engine(go)

    def approve(self, body):
        exp, h = _text(body, "exp"), _text(body, "hash", limit=64)
        self._with_engine(lambda e: e.approve(exp, h, _text(body, "note", required=False, limit=200)))
        return {"ok": True}

    def run(self, body):
        exp = _text(body, "exp")
        state = self._with_engine(lambda e: e.get_experiment(exp)["state"])
        if state in ("CREATED", "CONFIGURED"):
            raise ControlError("approve it first")
        if state in FINISHED:
            raise ControlError(f"the experiment is {state}")
        if not (self.rpc and self.builder):
            raise ControlError("the node is not connected (check config.local.json)")
        with self._lock:
            self._not_while_running()
            self._halt.clear()
            self.exp = exp
            self._thread = threading.Thread(target=self._runner, args=(exp,), daemon=True)
            self._thread.start()
        return {"ok": True}

    def _runner(self, exp):
        e, clean = None, False
        try:
            e = _open(self.db, self.rpc)[0]
            code = _run(e, self.rpc, self.builder, exp, self.say, self._wait)
            self.say(f"run ended (code {code})")
            clean = True
        except _Halt:
            self.say("stopped. Press Continue to carry on.")
            clean = True
        except Exception as err:  # noqa: BLE001
            self.say(f"ERROR: {type(err).__name__}: {err}")
        finally:
            if e is not None:
                e.close(clean=clean)

    def stop(self, body):
        why = "operator emergency stop (dashboard)"
        stopped = self._with_engine(lambda e: e.emergency_stop(why))
        self._halt.set()
        self.say(f"EMERGENCY STOP: aborted {stopped}")
        return {"aborted": stopped}

    def halt(self, body):
        """Stop the run between steps without aborting the experiment; Continue picks it up again."""
        self._halt.set()
        self.say("halting after the current step")
        return {"ok": True}

    def clear_stop(self, body):
        note = _text(body, "note")
        self._with_engine(lambda e: e.clear_emergency_stop(note))
        self.say("emergency stop cleared")
        return {"ok": True}

    def resume(self, body):
        self._not_while_running()
        exp = _text(body, "exp")

        def go(e):
            e.recover()
            e.resume(exp)
        self._with_engine(go, node=True)
        return {"ok": True}

    def recover(self, body):
        self._not_while_running()
        exp = _text(body, "exp")

        def go(e):
            s = e.recover()
            left = e.get_experiment(exp)["state"] == "RECOVERY"
            if left:
                e.leave_recovery(exp)
            return {"clean_shutdown": s["clean_shutdown"], "interrupted": s["unknown_actions"], "left_recovery": left}
        return self._with_engine(go, node=True)

    def resolve(self, body):
        action, outcome = body.get("action"), body.get("outcome")
        if not isinstance(action, int) or isinstance(action, bool) or outcome not in ("broadcast", "not_broadcast"):
            raise ControlError("action must be a number and outcome 'broadcast' or 'not_broadcast'")
        txid = body.get("txid") or None
        evidence = _text(body, "evidence", required=False)
        self._with_engine(lambda e: e.resolve_unknown_action(action, outcome, txid, evidence))
        return {"ok": True}

    def close(self):
        """Stop the active run between steps and wait for it, so the shutdown is clean."""
        self._halt.set()
        if self._thread:
            self._thread.join(timeout=30)
