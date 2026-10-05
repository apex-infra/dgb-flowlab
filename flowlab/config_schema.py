"""
config_schema.py -- validation of an experiment configuration.

Phase 1 validates shape and bounds only. It does not know about
DigiByte; wallet names are opaque strings. All amounts are integer
satoshis. Credentials are rejected outright -- an experiment config is
displayed, hashed, exported and logged, so it must never carry any.
"""

import copy
import json
import secrets
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

    rnd = cfg.get("randomization")
    _need(isinstance(rnd, dict) and isinstance(rnd.get("enabled"), bool),
          "randomization.enabled (bool) required")
    if rnd["enabled"]:
        _need(rnd.get("model") in RANDOM_MODELS, f"randomization.model must be one of {RANDOM_MODELS}")
        if rnd.get("seed") is None:
            rnd["seed"] = secrets.randbits(32)       # recorded before approval, so it is replayable
        _need(_is_int(rnd["seed"]) and rnd["seed"] >= 0, "randomization.seed must be a non-negative integer")
    return cfg


def canonical(cfg):
    return json.dumps(cfg, sort_keys=True, separators=(",", ":"))


def config_hash(cfg):
    return hashlib.sha256(canonical(cfg).encode("utf-8")).hexdigest()
