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


class ConfigError(ValueError):
    pass


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _need(cond, msg):
    if not cond:
        raise ConfigError(msg)


def _validate_transfers(fl, names, where):
    """Explicit, ordered transfers: every hop is written down by the operator."""
    ts = fl["transfers"]
    _need(isinstance(ts, list) and ts, f"{where}.transfers must be a non-empty list")
    out_of_source = 0
    for j, t in enumerate(ts):
        w = f"{where}.transfers[{j}]"
        _need(isinstance(t, dict) and set(t) == {"from", "to", "amount_sats", "delay_seconds"},
              f"{w} must have exactly: from, to, amount_sats, delay_seconds")
        _need(t["from"] in names and t["to"] in names, f"{w}: wallets must belong to this flow")
        _need(t["from"] != t["to"], f"{w}: from and to must differ")
        _need(_is_int(t["amount_sats"]) and t["amount_sats"] >= 1, f"{w}.amount_sats must be a positive integer")
        _need(_is_int(t["delay_seconds"]) and t["delay_seconds"] >= 0, f"{w}.delay_seconds must be >= 0")
        if t["from"] == fl["source_wallet"]:
            out_of_source += t["amount_sats"]
    _need(out_of_source <= fl["allocation_sats"], f"{where}: transfers out of the source exceed its allocation")


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
        if fl.get("transfers") is not None:
            _validate_transfers(fl, names, where)

    wl = cfg.get("workload")
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
    _need(isinstance(rnd, dict) and rnd.get("enabled") in (False, None),
          "randomization is not supported: transfers are explicit and deterministic")
    cfg["randomization"] = {"enabled": False}
    return cfg


def canonical(cfg):
    return json.dumps(cfg, sort_keys=True, separators=(",", ":"))


def config_hash(cfg):
    return hashlib.sha256(canonical(cfg).encode("utf-8")).hexdigest()
