"""
Global UTXO Policy Engine.

This module is intentionally pure:
- no RPC
- no database access
- no transaction creation
- no broadcasting

It receives an observed UTXO set plus an approved policy and returns an
explicit, reproducible selection decision and telemetry.

Transaction builders remain responsible for fee estimation, transaction
construction, signing, verification, and broadcast safety.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from itertools import combinations
from statistics import median

from .rpc import to_sats


POLICY_VERSION = 1

SCOPES = frozenset({
    "live_wallet",
    "snapshot_at_start",
    "snapshot_at_phase_start",
})

STRATEGIES = frozenset({
    "legacy",
    "oldest_confirmed",
    "newest_confirmed",
    "largest_first",
    "smallest_first",
    "fewest_inputs",
    "exact_match",
    "balanced",
    "consolidation",
    "seeded_selection",
})

PHASES = frozenset({
    "allocation",
    "workload",
    "consolidation",
    "settlement",
    "return_workload",
    "reserve_return",
    "finalization",
    "terminal_distribution",
})

DEFAULT_POLICY = {
    "version": POLICY_VERSION,
    "default": {
        "scope": "live_wallet",
        "strategy": "legacy",
        "min_confirmations": 1,
        "max_confirmations": None,
        "min_utxo_sats": None,
        "max_utxo_sats": None,
        "max_inputs": None,
        "require_spendable": True,
        "require_safe": True,
        "seed": None,
        "fallback": "fail",
    },
    "phases": {},
}


class UtxoPolicyError(Exception):
    pass


@dataclass(frozen=True)
class SelectionResult:
    strategy: str
    scope: str
    phase: str | None
    candidate_count: int
    eligible_count: int
    selected_count: int
    selected_total_sats: int
    required_sats: int | None
    sweep: bool
    selected: tuple
    min_confirmations: int | None
    max_confirmations: int | None

    def telemetry(self):
        return {
            "policy_version": POLICY_VERSION,
            "strategy": self.strategy,
            "scope": self.scope,
            "phase": self.phase,
            "candidate_count": self.candidate_count,
            "eligible_count": self.eligible_count,
            "selected_count": self.selected_count,
            "selected_total_sats": self.selected_total_sats,
            "required_sats": self.required_sats,
            "sweep": self.sweep,
            "confirmation_min": self.min_confirmations,
            "confirmation_max": self.max_confirmations,
        }


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _need(condition, message):
    if not condition:
        raise UtxoPolicyError(message)


def _copy_rule(rule):
    return {
        "scope": rule["scope"],
        "strategy": rule["strategy"],
        "min_confirmations": rule["min_confirmations"],
        "max_confirmations": rule["max_confirmations"],
        "min_utxo_sats": rule["min_utxo_sats"],
        "max_utxo_sats": rule["max_utxo_sats"],
        "max_inputs": rule["max_inputs"],
        "require_spendable": rule["require_spendable"],
        "require_safe": rule["require_safe"],
        "seed": rule["seed"],
        "fallback": rule["fallback"],
    }


def _validate_rule(raw, *, where):
    _need(isinstance(raw, dict), f"{where} must be an object")

    allowed = {
        "scope",
        "strategy",
        "min_confirmations",
        "max_confirmations",
        "min_utxo_sats",
        "max_utxo_sats",
        "max_inputs",
        "require_spendable",
        "require_safe",
        "seed",
        "fallback",
    }

    unknown = set(raw) - allowed
    _need(not unknown, f"{where} contains unknown fields: {sorted(unknown)}")

    base = _copy_rule(DEFAULT_POLICY["default"])
    base.update(raw)

    _need(base["scope"] in SCOPES,
          f"{where}.scope must be one of {sorted(SCOPES)}")

    _need(base["strategy"] in STRATEGIES,
          f"{where}.strategy must be one of {sorted(STRATEGIES)}")

    _need(
        _is_int(base["min_confirmations"])
        and base["min_confirmations"] >= 0,
        f"{where}.min_confirmations must be a non-negative integer",
    )

    if base["max_confirmations"] is not None:
        _need(
            _is_int(base["max_confirmations"])
            and base["max_confirmations"] >= base["min_confirmations"],
            f"{where}.max_confirmations must be >= min_confirmations",
        )

    for name in ("min_utxo_sats", "max_utxo_sats"):
        value = base[name]
        if value is not None:
            _need(
                _is_int(value) and value > 0,
                f"{where}.{name} must be a positive integer or null",
            )

    if (
        base["min_utxo_sats"] is not None
        and base["max_utxo_sats"] is not None
    ):
        _need(
            base["min_utxo_sats"] <= base["max_utxo_sats"],
            f"{where}.min_utxo_sats must be <= max_utxo_sats",
        )

    if base["max_inputs"] is not None:
        _need(
            _is_int(base["max_inputs"]) and base["max_inputs"] >= 1,
            f"{where}.max_inputs must be a positive integer or null",
        )

    _need(
        isinstance(base["require_spendable"], bool),
        f"{where}.require_spendable must be a bool",
    )

    _need(
        isinstance(base["require_safe"], bool),
        f"{where}.require_safe must be a bool",
    )

    if base["seed"] is not None:
        _need(
            _is_int(base["seed"]) and base["seed"] >= 0,
            f"{where}.seed must be a non-negative integer or null",
        )

    if base["strategy"] == "seeded_selection":
        _need(
            base["seed"] is not None,
            f"{where}.seed is required for seeded_selection",
        )

    _need(
        base["fallback"] == "fail",
        f"{where}.fallback currently supports only 'fail'",
    )

    return base


def validate_policy(raw):
    """
    Validate and canonicalize one global policy object.

    Phase rules are partial overrides of the default rule.
    """
    if raw is None:
        raw = {}

    _need(isinstance(raw, dict), "utxo_policy must be an object")

    allowed = {"version", "default", "phases"}
    unknown = set(raw) - allowed
    _need(not unknown, f"utxo_policy contains unknown fields: {sorted(unknown)}")

    version = raw.get("version", POLICY_VERSION)
    _need(
        version == POLICY_VERSION,
        f"utxo_policy.version must be {POLICY_VERSION}",
    )

    default = _validate_rule(
        raw.get("default", {}),
        where="utxo_policy.default",
    )

    phases_raw = raw.get("phases", {})
    _need(isinstance(phases_raw, dict), "utxo_policy.phases must be an object")

    unknown_phases = set(phases_raw) - PHASES
    _need(
        not unknown_phases,
        f"utxo_policy contains unknown phases: {sorted(unknown_phases)}",
    )

    phases = {}

    for phase, override in phases_raw.items():
        _need(
            isinstance(override, dict),
            f"utxo_policy.phases.{phase} must be an object",
        )

        merged = dict(default)
        merged.update(override)

        phases[phase] = _validate_rule(
            merged,
            where=f"utxo_policy.phases.{phase}",
        )

    return {
        "version": POLICY_VERSION,
        "default": default,
        "phases": phases,
    }


def resolve_policy(policy, phase=None):
    policy = validate_policy(policy)

    if phase is not None:
        _need(
            phase in PHASES,
            f"unknown UTXO policy phase: {phase}",
        )

    if phase in policy["phases"]:
        return _copy_rule(policy["phases"][phase])

    return _copy_rule(policy["default"])


def outpoint(utxo):
    return (utxo["txid"], int(utxo["vout"]))


def _amount_sats(utxo):
    if "amount_sats" in utxo:
        value = utxo["amount_sats"]
        _need(
            _is_int(value) and value >= 0,
            "UTXO amount_sats must be a non-negative integer",
        )
        return value

    _need("amount" in utxo, "UTXO requires amount or amount_sats")
    return to_sats(utxo["amount"])


def _confirmations(utxo):
    value = utxo.get("confirmations", 0)

    _need(
        _is_int(value) and value >= 0,
        "UTXO confirmations must be a non-negative integer",
    )

    return value


def normalize_utxo(utxo):
    _need(isinstance(utxo, dict), "UTXO must be an object")
    _need(isinstance(utxo.get("txid"), str) and utxo["txid"],
          "UTXO requires txid")
    _need(_is_int(utxo.get("vout")) and utxo["vout"] >= 0,
          "UTXO vout must be a non-negative integer")

    return {
        **utxo,
        "amount_sats": _amount_sats(utxo),
        "confirmations": _confirmations(utxo),
        "spendable": bool(utxo.get("spendable", True)),
        "safe": bool(utxo.get("safe", True)),
    }


def eligible_utxos(
        utxos,
        rule,
        *,
        cohort_outpoints=None,
):
    """
    Apply scope and eligibility constraints.

    snapshot_* scopes require the caller to supply the approved/snapshotted
    outpoint set. The policy engine never guesses cohort membership.
    """
    rule = _validate_rule(rule, where="resolved_utxo_policy")

    coins = [normalize_utxo(u) for u in utxos]

    if rule["scope"] != "live_wallet":
        _need(
            cohort_outpoints is not None,
            f"{rule['scope']} requires cohort_outpoints",
        )

        cohort = {
            (str(txid), int(vout))
            for txid, vout in cohort_outpoints
        }

        coins = [u for u in coins if outpoint(u) in cohort]

    eligible = []

    for u in coins:
        amount = u["amount_sats"]
        confirmations = u["confirmations"]

        if rule["require_spendable"] and not u["spendable"]:
            continue

        if rule["require_safe"] and not u["safe"]:
            continue

        if confirmations < rule["min_confirmations"]:
            continue

        if (
            rule["max_confirmations"] is not None
            and confirmations > rule["max_confirmations"]
        ):
            continue

        if (
            rule["min_utxo_sats"] is not None
            and amount < rule["min_utxo_sats"]
        ):
            continue

        if (
            rule["max_utxo_sats"] is not None
            and amount > rule["max_utxo_sats"]
        ):
            continue

        eligible.append(u)

    return tuple(eligible)


def _legacy_key(u):
    return (
        -u["amount_sats"],
        u["txid"],
        u["vout"],
    )


def _oldest_key(u):
    return (
        -u["confirmations"],
        -u["amount_sats"],
        u["txid"],
        u["vout"],
    )


def _newest_key(u):
    return (
        u["confirmations"],
        -u["amount_sats"],
        u["txid"],
        u["vout"],
    )


def _largest_key(u):
    return (
        -u["amount_sats"],
        -u["confirmations"],
        u["txid"],
        u["vout"],
    )


def _smallest_key(u):
    return (
        u["amount_sats"],
        -u["confirmations"],
        u["txid"],
        u["vout"],
    )


def _balanced_key(coins):
    midpoint = median([u["amount_sats"] for u in coins])

    return lambda u: (
        abs(u["amount_sats"] - midpoint),
        -u["confirmations"],
        u["txid"],
        u["vout"],
    )


def ordered_candidates(coins, rule, *, seed_material=""):
    strategy = rule["strategy"]
    coins = list(coins)

    if strategy == "legacy":
        return sorted(coins, key=_legacy_key)

    if strategy == "oldest_confirmed":
        return sorted(coins, key=_oldest_key)

    if strategy == "newest_confirmed":
        return sorted(coins, key=_newest_key)

    if strategy in ("largest_first", "fewest_inputs"):
        return sorted(coins, key=_largest_key)

    if strategy in ("smallest_first", "consolidation"):
        return sorted(coins, key=_smallest_key)

    if strategy == "balanced":
        return sorted(coins, key=_balanced_key(coins))

    if strategy == "exact_match":
        # Exact-match selection is handled separately. This stable ordering is
        # the deterministic fallback search order inside that algorithm.
        return sorted(coins, key=_largest_key)

    if strategy == "seeded_selection":
        material = (
            f"flowlab-utxo-policy-v{POLICY_VERSION}:"
            f"{rule['seed']}:{seed_material}"
        ).encode()

        derived = int.from_bytes(hashlib.sha256(material).digest(), "big")
        rng = random.Random(derived)

        ordered = sorted(
            coins,
            key=lambda u: (u["txid"], u["vout"]),
        )
        rng.shuffle(ordered)
        return ordered

    raise UtxoPolicyError(f"unsupported UTXO strategy: {strategy}")


def _prefix_selection(
        coins,
        required_sats,
        *,
        fee_reserve_for_n,
        max_inputs,
):
    chosen = []
    total = 0

    for u in coins:
        chosen.append(u)
        total += u["amount_sats"]

        if max_inputs is not None and len(chosen) > max_inputs:
            break

        reserve = fee_reserve_for_n(len(chosen))

        if total >= required_sats + reserve:
            return chosen

    return None


def _exact_match_selection(
        coins,
        required_sats,
        *,
        fee_reserve_for_n,
        max_inputs,
):
    """
    Search bounded combinations for the smallest excess above target+fee.

    To keep runtime predictable, exact combinatorial search is capped at the
    first 18 deterministically ordered eligible candidates. Larger sets still
    remain usable through deterministic prefix fallback.
    """
    ordered = sorted(coins, key=_largest_key)

    limit = min(len(ordered), 18)

    if max_inputs is None:
        max_n = limit
    else:
        max_n = min(max_inputs, limit)

    best = None

    for n in range(1, max_n + 1):
        reserve = fee_reserve_for_n(n)
        target = required_sats + reserve

        for combo in combinations(ordered[:limit], n):
            total = sum(u["amount_sats"] for u in combo)

            if total < target:
                continue

            score = (
                total - target,
                n,
                tuple((u["txid"], u["vout"]) for u in combo),
            )

            if best is None or score < best[0]:
                best = (score, list(combo))

        if best is not None and best[0][0] == 0:
            break

    if best is not None:
        return best[1]

    return _prefix_selection(
        ordered,
        required_sats,
        fee_reserve_for_n=fee_reserve_for_n,
        max_inputs=max_inputs,
    )


def select_utxos(
        utxos,
        policy,
        *,
        required_sats=None,
        sweep=False,
        phase=None,
        cohort_outpoints=None,
        seed_material="",
        fee_reserve_for_n=None,
):
    """
    Select UTXOs according to an approved global policy.

    Exact-amount jobs:
      required_sats must be supplied and selection may choose a subset.

    Sweep/all jobs:
      every eligible UTXO is selected. Strategy only determines stable order.

    fee_reserve_for_n:
      callable accepting selected input count and returning required fee reserve.
      The caller may use the same fee estimator used by TxBuilder.
    """
    _need(isinstance(sweep, bool), "sweep must be a bool")

    if sweep:
        _need(
            required_sats is None,
            "sweep selection must not provide required_sats",
        )
    else:
        _need(
            _is_int(required_sats) and required_sats > 0,
            "required_sats must be a positive integer",
        )

    if fee_reserve_for_n is None:
        fee_reserve_for_n = lambda _n: 0

    rule = resolve_policy(policy, phase)

    candidates = tuple(normalize_utxo(u) for u in utxos)

    eligible = eligible_utxos(
        candidates,
        rule,
        cohort_outpoints=cohort_outpoints,
    )

    if not eligible:
        raise UtxoPolicyError(
            "UTXO policy produced no eligible inputs"
        )

    ordered = ordered_candidates(
        eligible,
        rule,
        seed_material=seed_material,
    )

    if sweep:
        if (
            rule["max_inputs"] is not None
            and len(ordered) > rule["max_inputs"]
        ):
            raise UtxoPolicyError(
                "UTXO sweep exceeds approved max_inputs"
            )

        chosen = list(ordered)

    elif rule["strategy"] == "exact_match":
        chosen = _exact_match_selection(
            ordered,
            required_sats,
            fee_reserve_for_n=fee_reserve_for_n,
            max_inputs=rule["max_inputs"],
        )

        if chosen is None:
            raise UtxoPolicyError(
                "UTXO policy cannot satisfy requested amount"
            )

    else:
        chosen = _prefix_selection(
            ordered,
            required_sats,
            fee_reserve_for_n=fee_reserve_for_n,
            max_inputs=rule["max_inputs"],
        )

        if chosen is None:
            raise UtxoPolicyError(
                "UTXO policy cannot satisfy requested amount"
            )

    total = sum(u["amount_sats"] for u in chosen)

    confirmations = [
        u["confirmations"]
        for u in chosen
    ]

    return SelectionResult(
        strategy=rule["strategy"],
        scope=rule["scope"],
        phase=phase,
        candidate_count=len(candidates),
        eligible_count=len(eligible),
        selected_count=len(chosen),
        selected_total_sats=total,
        required_sats=required_sats,
        sweep=sweep,
        selected=tuple(chosen),
        min_confirmations=min(confirmations) if confirmations else None,
        max_confirmations=max(confirmations) if confirmations else None,
    )
