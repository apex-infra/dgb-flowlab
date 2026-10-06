"""
config_schema.py -- validation of an experiment configuration.

Phase 1 validates shape and bounds only. It does not know about
DigiByte; wallet names are opaque strings. All amounts are integer
satoshis. Credentials are rejected outright -- an experiment config is
displayed, hashed, exported and logged, so it must never carry any.
"""

import copy
import json
import hashlib

from . import audit

WORKLOAD_MODES = ("count", "duration", "automatic")
ADDRESS_POLICIES = ("existing", "new")
RANDOM_MODELS = ("uniform", "weighted", "bounded_random", "seeded_deterministic")


class ConfigError(ValueError):
    pass


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _need(cond, msg):
    if not cond:
        raise ConfigError(msg)


MAX_REPEAT = 500


def _expand_repeat(fl, names, where):
    """Shorthand for a long, uniform list. Expands to plain explicit transfers
    (which is what gets reviewed and approved): hop i goes cycle[i] -> cycle[i+1],
    around the cycle, each with the same delay and an amount that drops by a fixed
    step_down_sats per hop (so each wallet can pay the fee out of what it received)."""
    r = fl["repeat"]
    need = {"cycle", "count", "amount_sats", "delay_seconds"}
    _need(isinstance(r, dict) and need <= set(r) <= need | {"step_down_sats", "sweep"},
          f"{where}.repeat needs: cycle, count, amount_sats, delay_seconds (optional step_down_sats, sweep)")
    cyc = r["cycle"]
    _need(isinstance(cyc, list) and len(cyc) >= 2 and all(c in names for c in cyc),
          f"{where}.repeat.cycle must list at least two of this flow's wallets")
    _need(cyc[0] == fl["source_wallet"], f"{where}.repeat.cycle must start at the source wallet")
    _need(all(cyc[i] != cyc[(i + 1) % len(cyc)] for i in range(len(cyc))),
          f"{where}.repeat.cycle: neighbouring wallets must differ")
    step = r.get("step_down_sats", 0)
    sweep = r.get("sweep", False)
    _need(isinstance(sweep, bool), f"{where}.repeat.sweep must be true or false")
    _need(not (sweep and step), f"{where}.repeat: sweep and step_down_sats cannot be combined")
    _need(_is_int(r["count"]) and 1 <= r["count"] <= MAX_REPEAT, f"{where}.repeat.count must be 1..{MAX_REPEAT}")
    _need(_is_int(r["amount_sats"]) and r["amount_sats"] >= 1, f"{where}.repeat.amount_sats must be a positive integer")
    _need(_is_int(step) and step >= 0, f"{where}.repeat.step_down_sats must be >= 0")
    _need(_is_int(r["delay_seconds"]) and r["delay_seconds"] >= 0, f"{where}.repeat.delay_seconds must be >= 0")
    _need(r["amount_sats"] - (r["count"] - 1) * step >= 1, f"{where}.repeat: the amount would reach zero")
    n = len(cyc)
    def amount(i):
        if sweep and cyc[i % n] != fl["source_wallet"]:
            return "all"          # whole balance minus the fee: nothing is left behind
        return r["amount_sats"] - i * step
    return [{"from": cyc[i % n], "to": cyc[(i + 1) % n], "amount_sats": amount(i),
             "delay_seconds": r["delay_seconds"]} for i in range(r["count"])]


def _validate_experimental_topology(fl, where):
    """Validate the approved wallet transitions available to experimental jobs.

    The destination wallet is intentionally excluded here; reaching it is a
    finalization concern, not part of the experimental workload.
    """
    topo = fl.get("experimental_topology")
    _need(isinstance(topo, dict), f"{where}.experimental_topology must be an object")
    _need(set(topo) == {"transitions"},
          f"{where}.experimental_topology must contain exactly: transitions")

    transitions = topo["transitions"]
    _need(isinstance(transitions, list) and transitions,
          f"{where}.experimental_topology.transitions must be a non-empty list")

    source = fl["source_wallet"]
    active = [source, *fl["flow_wallets"]]
    allowed = set(active)

    seen = set()
    source_outbound = False

    for j, t in enumerate(transitions):
        w = f"{where}.experimental_topology.transitions[{j}]"
        _need(isinstance(t, dict) and set(t) == {"from", "to"},
              f"{w} must have exactly: from, to")
        _need(t["from"] in allowed and t["to"] in allowed,
              f"{w}: wallets must be the source or one of this flow's experimental wallets")
        _need(not (t["from"] == source and t["to"] == source),
              f"{w}: source -> source is not an experimental transition")

        edge = (t["from"], t["to"])
        _need(edge not in seen, f"{w}: duplicate transition {t['from']} -> {t['to']}")
        seen.add(edge)

        if t["from"] == source and t["to"] != source:
            source_outbound = True

    _need(source_outbound,
          f"{where}.experimental_topology needs at least one transition out of the source wallet")


def _validate_transfers(fl, names, where):
    """Explicit, ordered transfers: every hop is written down by the operator."""
    ts = fl["transfers"]
    _need(isinstance(ts, list) and ts, f"{where}.transfers must be a non-empty list")
    bal = {n: 0 for n in names}
    bal[fl["source_wallet"]] = fl["allocation_sats"]
    for j, t in enumerate(ts):
        w = f"{where}.transfers[{j}]"
        _need(isinstance(t, dict) and set(t) == {"from", "to", "amount_sats", "delay_seconds"},
              f"{w} must have exactly: from, to, amount_sats, delay_seconds")
        _need(t["from"] in names and t["to"] in names, f"{w}: wallets must belong to this flow")
        _need(t["from"] != t["to"], f"{w}: from and to must differ")
        sweep = t["amount_sats"] == "all"
        _need(sweep or (_is_int(t["amount_sats"]) and t["amount_sats"] >= 1),
              f"{w}.amount_sats must be a positive integer, or \"all\" (whole balance minus fee)")
        _need(not sweep or t["from"] != fl["source_wallet"],
              f"{w}: \"all\" is not allowed out of the source wallet")
        _need(_is_int(t["delay_seconds"]) and t["delay_seconds"] >= 0, f"{w}.delay_seconds must be >= 0")
        amt = bal[t["from"]] if sweep else t["amount_sats"]
        _need(amt >= 1 and bal[t["from"]] >= amt, f"{w}: {t['from']} would not hold enough to send this")
        bal[t["from"]] -= amt
        bal[t["to"]] += amt


def validate(cfg):
    """Return a normalized deep copy of cfg, or raise ConfigError."""
    _need(isinstance(cfg, dict), "config must be an object")
    _need(not audit.has_secret_keys(cfg), "config must not contain credentials or key material")
    cfg = copy.deepcopy(cfg)

    flows = cfg.get("flows")
    _need(isinstance(flows, list) and flows, "config.flows must be a non-empty list")
    for i, fl in enumerate(flows):
        where = f"flows[{i}]"
        _need(isinstance(fl, dict), f"{where} must be an object")
        for key in ("source_wallet", "destination_wallet"):
            _need(isinstance(fl.get(key), str) and fl[key], f"{where}.{key} required")
        fw = fl.get("flow_wallets")
        _need(isinstance(fw, list) and all(isinstance(w, str) and w for w in fw),
              f"{where}.flow_wallets must be a list of wallet names")
        names = [fl["source_wallet"], *fw, fl["destination_wallet"]]
        _need(len(set(names)) == len(names), f"{where}: wallets must all be distinct")
        _need(_is_int(fl.get("allocation_sats")) and fl["allocation_sats"] > 0,
              f"{where}.allocation_sats must be a positive integer (satoshis)")
        fl.setdefault("description", "")
        if fl.get("repeat") is not None:
            _need(fl.get("transfers") is None, f"{where}: use either repeat or transfers, not both")
            fl["transfers"] = _expand_repeat(fl, names, where)
            del fl["repeat"]
        if fl.get("transfers") is not None:
            _validate_transfers(fl, names, where)
        if fl.get("experimental_topology") is not None:
            _validate_experimental_topology(fl, where)

    wl = cfg.get("workload")
    if wl is not None:
        _need(isinstance(wl, dict), "config.workload required")
        _need(wl.get("mode") in WORKLOAD_MODES, f"workload.mode must be one of {WORKLOAD_MODES}")
        if wl["mode"] == "count":
            _need(_is_int(wl.get("jobs")) and wl["jobs"] > 0, "workload.jobs must be a positive integer")
        if wl["mode"] == "duration":
            _need(_is_int(wl.get("duration_seconds")) and wl["duration_seconds"] > 0,
                  "workload.duration_seconds must be a positive integer")
        for lo, hi, floor in (("amount_sats_min", "amount_sats_max", 1),
                              ("delay_seconds_min", "delay_seconds_max", 0)):
            _need(_is_int(wl.get(lo)) and _is_int(wl.get(hi)), f"workload.{lo}/{hi} must be integers")
            _need(floor <= wl[lo] <= wl[hi], f"workload: need {floor} <= {lo} <= {hi}")
        smallest_alloc = min(fl["allocation_sats"] for fl in flows)
        _need(wl["amount_sats_max"] <= smallest_alloc,
              "workload.amount_sats_max exceeds a flow's allocation")

    _need(_is_int(cfg.get("confirmations_required")) and cfg["confirmations_required"] >= 1,
          "confirmations_required must be an integer >= 1")
    _need(isinstance(cfg.get("fee_policy"), dict), "fee_policy must be an object")
    _need(cfg.get("address_policy") in ADDRESS_POLICIES,
          f"address_policy must be one of {ADDRESS_POLICIES}")

    rnd = cfg.get("randomization", {"enabled": False})
    _need(isinstance(rnd, dict) and isinstance(rnd.get("enabled"), bool),
          "randomization.enabled must be a bool")
    if rnd["enabled"]:
        _need(rnd.get("model") in RANDOM_MODELS,
              f"randomization.model must be one of {RANDOM_MODELS}")
        seed = rnd.get("seed")
        _need(_is_int(seed) and seed >= 0,
              "randomization.seed must be a non-negative integer")
        _need(wl is not None,
              "randomization requires a workload envelope")
        for i, fl in enumerate(flows):
            _need(fl.get("experimental_topology") is not None,
                  f"flows[{i}].experimental_topology required when randomization is enabled")
    else:
        cfg["randomization"] = {"enabled": False}
    return cfg


def canonical(cfg):
    return json.dumps(cfg, sort_keys=True, separators=(",", ":"))


def config_hash(cfg):
    return hashlib.sha256(canonical(cfg).encode("utf-8")).hexdigest()
