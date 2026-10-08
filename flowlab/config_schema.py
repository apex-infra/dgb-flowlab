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
from .utxo_policy import UtxoPolicyError, validate_policy

WORKLOAD_MODES = ("count", "duration", "automatic")
ADDRESS_POLICIES = ("existing", "new")
RANDOM_MODELS = ("uniform", "weighted", "bounded_random", "seeded_deterministic")
FINALIZATION_MODES = (
    "sweep_workers_to_destination",
    "consolidate_then_distribute",
)


class ConfigError(ValueError):
    pass


def destination_endpoint(flow):
    """Return the approved final endpoint string for a flow."""
    wallet = flow.get("destination_wallet")
    address = flow.get("destination_address")

    if isinstance(wallet, str) and wallet:
        return wallet
    if isinstance(address, str) and address:
        return address

    raise ConfigError(
        "flow has no destination_wallet or destination_address"
    )


def destination_is_external(flow):
    """True only for an explicit destination_address endpoint."""
    return (
        isinstance(flow.get("destination_address"), str)
        and bool(flow["destination_address"])
    )


MAX_DESTINATIONS = 10
DESTINATION_MODES = ("percentage", "fixed")


def settlement_cycle_identity(flow, cycle):
    """Return the stable DB identity for one Settlement Cycle V1 flow.

    The identity commits to both the flow-level allocation/topology fields
    and the complete Settlement Cycle lifecycle contract. The identity
    itself is deliberately excluded from the hashed payload.
    """
    payload = {
        "version": 1,
        "flow": {
            "source_wallet": flow["source_wallet"],
            "allocation_wallet": flow["allocation_wallet"],
            "flow_wallets": flow["flow_wallets"],
            "allocation_sats": flow["allocation_sats"],
        },
        "settlement_cycle": cycle,
    }

    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    digest = hashlib.sha256(encoded).hexdigest()
    return f"settlement_cycle:{digest}"


def destination_identity(flow):
    """Stable flow identity for DB matching.

    Legacy single-destination configs retain their old endpoint string.
    Multi-destination and Settlement Cycle configs use deterministic
    synthetic markers so the existing flows.destination_wallet TEXT
    column can remain unchanged.
    """
    flow_identity = flow.get("flow_identity")

    if isinstance(flow_identity, str) and flow_identity:
        return flow_identity

    if flow.get("destinations") is None:
        return destination_endpoint(flow)

    payload = json.dumps(
        flow["destinations"],
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"destinations:{digest}"


def destination_items(flow):
    """Return terminal destination items in normalized form."""
    spec = flow.get("destinations")

    if spec is not None:
        return copy.deepcopy(spec["items"])

    wallet = flow.get("destination_wallet")
    if isinstance(wallet, str) and wallet:
        return [{"type": "wallet", "wallet": wallet}]

    address = flow.get("destination_address")
    if isinstance(address, str) and address:
        return [{"type": "address", "address": address}]

    raise ConfigError("flow has no destination definition")


def destination_wallets(flow):
    """Return internal terminal wallet names."""
    return [
        item["wallet"]
        for item in destination_items(flow)
        if item["type"] == "wallet"
    ]


def destination_addresses(flow):
    """Return external terminal DigiByte addresses."""
    return [
        item["address"]
        for item in destination_items(flow)
        if item["type"] == "address"
    ]


def has_multi_destinations(flow):
    return flow.get("destinations") is not None


def _validate_destination_set(fl, where):
    spec = fl.get("destinations")

    _need(
        isinstance(spec, dict),
        f"{where}.destinations must be an object",
    )
    _need(
        set(spec) == {"mode", "items"},
        f"{where}.destinations must contain exactly: mode, items",
    )

    mode = spec["mode"]
    items = spec["items"]

    _need(
        mode in DESTINATION_MODES,
        f"{where}.destinations.mode must be one of {DESTINATION_MODES}",
    )
    _need(
        isinstance(items, list)
        and 1 <= len(items) <= MAX_DESTINATIONS,
        f"{where}.destinations.items must contain 1..{MAX_DESTINATIONS} destinations",
    )

    targets = []
    percent_total = 0
    remainder_count = 0
    fixed_total = 0

    for j, item in enumerate(items):
        w = f"{where}.destinations.items[{j}]"

        _need(isinstance(item, dict), f"{w} must be an object")

        kind = item.get("type")
        _need(
            kind in ("wallet", "address"),
            f"{w}.type must be wallet or address",
        )

        if kind == "wallet":
            target_key = "wallet"
        else:
            target_key = "address"

        target = item.get(target_key)

        _need(
            isinstance(target, str) and target,
            f"{w}.{target_key} must be a non-empty string",
        )

        targets.append((kind, target))

        if mode == "percentage":
            _need(
                set(item) == {"type", target_key, "percent_bps"},
                f"{w} must contain exactly: type, {target_key}, percent_bps",
            )

            bps = item["percent_bps"]

            _need(
                _is_int(bps) and 1 <= bps <= 10_000,
                f"{w}.percent_bps must be an integer in 1..10000",
            )

            percent_total += bps

        else:
            amount_form = (
                set(item) == {"type", target_key, "amount_sats"}
            )
            remainder_form = (
                set(item) == {"type", target_key, "remainder"}
                and item.get("remainder") is True
            )

            _need(
                amount_form or remainder_form,
                f"{w} must contain either type, {target_key}, amount_sats "
                f"or type, {target_key}, remainder=true",
            )

            if amount_form:
                amount = item["amount_sats"]

                _need(
                    _is_int(amount) and amount > 0,
                    f"{w}.amount_sats must be a positive integer",
                )

                fixed_total += amount

            else:
                remainder_count += 1

    _need(
        len(set(targets)) == len(targets),
        f"{where}.destinations contains duplicate terminal targets",
    )

    if mode == "percentage":
        _need(
            percent_total == 10_000,
            f"{where}.destinations percentage total must equal exactly "
            "10000 basis points",
        )
    else:
        _need(
            remainder_count == 1,
            f"{where}.destinations fixed mode requires exactly one "
            "remainder destination",
        )
        _need(
            fixed_total < fl["allocation_sats"],
            f"{where}.destinations fixed amounts must leave allocation "
            "for the remainder destination and network fee",
        )


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
    allocation_wallet = fl.get("allocation_wallet", source)

    # New committed-allocation configs keep the stocked reserve/source outside
    # randomized workload topology.  Legacy configs without allocation_wallet
    # continue to treat source_wallet as the workload entry point.
    active = [allocation_wallet, *fl["flow_wallets"]]
    allowed = set(active)

    seen = set()
    allocation_outbound = False

    for j, t in enumerate(transitions):
        w = f"{where}.experimental_topology.transitions[{j}]"
        _need(isinstance(t, dict) and set(t) == {"from", "to"},
              f"{w} must have exactly: from, to")
        _need(t["from"] in allowed and t["to"] in allowed,
              f"{w}: wallets must be the source or one of this flow's experimental wallets")
        _need(not (t["from"] == allocation_wallet and t["to"] == allocation_wallet),
              f"{w}: allocation wallet -> itself is not an experimental transition")

        if allocation_wallet != source:
            _need(t["from"] != source and t["to"] != source,
                  f"{w}: reserve/source wallet is outside experimental workload topology")

        edge = (t["from"], t["to"])
        _need(edge not in seen, f"{w}: duplicate transition {t['from']} -> {t['to']}")
        seen.add(edge)

        if t["from"] == allocation_wallet and t["to"] != allocation_wallet:
            allocation_outbound = True

    _need(
        allocation_outbound,
        f"{where}.experimental_topology needs at least one transition out of "
        "the allocation wallet",
    )


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

    play = cfg.get("play")
    if play is not None:
        _need(isinstance(play, dict), "config.play must be an object")
        _need(
            set(play) == {"name", "version"},
            "config.play must contain exactly name and version",
        )
        _need(
            play.get("name") in (
                "fan_out_fan_in",
                "settlement_cycle",
            ),
            "config.play.name is not supported",
        )
        _need(
            play.get("version") == 1,
            "config.play.version is not supported",
        )

    settlement_cycle_play = (
        isinstance(play, dict)
        and play.get("name") == "settlement_cycle"
        and play.get("version") == 1
    )

    if settlement_cycle_play:
        cycle = cfg.get("settlement_cycle")

        _need(
            isinstance(cycle, dict),
            "config.settlement_cycle must be an object",
        )
        _need(
            set(cycle) == {
                "workers",
                "hubs",
                "outbound",
                "settlement",
                "return",
                "reserve_return",
                "max_total_transactions",
            },
            "config.settlement_cycle has unsupported fields",
        )

    flows = cfg.get("flows")
    _need(isinstance(flows, list) and flows, "config.flows must be a non-empty list")
    for i, fl in enumerate(flows):
        where = f"flows[{i}]"
        _need(isinstance(fl, dict), f"{where} must be an object")

        if settlement_cycle_play:
            flow_identity = fl.get("flow_identity")

            _need(
                isinstance(flow_identity, str)
                and flow_identity.startswith("settlement_cycle:"),
                f"{where}.flow_identity must be a Settlement Cycle identity",
            )

            expected_identity = settlement_cycle_identity(
                fl,
                cfg["settlement_cycle"],
            )

            _need(
                flow_identity == expected_identity,
                f"{where}.flow_identity does not match the immutable "
                "Settlement Cycle contract",
            )
        else:
            _need(
                fl.get("flow_identity") is None,
                f"{where}.flow_identity is only valid for Settlement Cycle",
            )
        _need(
            isinstance(fl.get("source_wallet"), str) and fl["source_wallet"],
            f"{where}.source_wallet required",
        )

        has_destination_wallet = (
            isinstance(fl.get("destination_wallet"), str)
            and bool(fl["destination_wallet"])
        )
        has_destination_address = (
            isinstance(fl.get("destination_address"), str)
            and bool(fl["destination_address"])
        )
        has_destination_set = fl.get("destinations") is not None

        destination_count = sum((
            bool(has_destination_wallet),
            bool(has_destination_address),
            bool(has_destination_set),
        ))

        if settlement_cycle_play:
            _need(
                destination_count == 0,
                f"{where}: Settlement Cycle must not define a "
                "terminal destination",
            )
        else:
            _need(
                destination_count == 1,
                f"{where} requires exactly one destination definition: "
                "destination_wallet, destination_address, or destinations",
            )

        legacy_destination = (
            fl["destination_wallet"]
            if has_destination_wallet
            else fl["destination_address"]
            if has_destination_address
            else None
        )

        fw = fl.get("flow_wallets")
        _need(
            isinstance(fw, list)
            and all(isinstance(w, str) and w for w in fw),
            f"{where}.flow_wallets must be a list of wallet names",
        )

        managed_names = [fl["source_wallet"], *fw]

        _need(
            len(set(managed_names)) == len(managed_names),
            f"{where}: source and flow wallets must all be distinct",
        )

        if legacy_destination is not None:
            names = [*managed_names, legacy_destination]
            _need(
                len(set(names)) == len(names),
                f"{where}: wallets/endpoints must all be distinct",
            )
        else:
            names = list(managed_names)

        allocation_wallet = fl.get("allocation_wallet")
        if allocation_wallet is not None:
            _need(
                isinstance(allocation_wallet, str) and allocation_wallet,
                f"{where}.allocation_wallet must be a wallet name",
            )
            _need(
                allocation_wallet in fw,
                f"{where}.allocation_wallet must be one of flow_wallets",
            )
            _need(
                allocation_wallet != fl["source_wallet"],
                f"{where}.allocation_wallet must differ from source",
            )

            if legacy_destination is not None:
                _need(
                    allocation_wallet != legacy_destination,
                    f"{where}.allocation_wallet must differ from destination",
                )

        _need(
            _is_int(fl.get("allocation_sats"))
            and fl["allocation_sats"] > 0,
            f"{where}.allocation_sats must be a positive integer (satoshis)",
        )

        if has_destination_set:
            _validate_destination_set(fl, where)

            finalization_wallet = fl.get("finalization_wallet")

            _need(
                isinstance(finalization_wallet, str)
                and finalization_wallet,
                f"{where}.finalization_wallet required with destinations",
            )
            _need(
                finalization_wallet in managed_names,
                f"{where}.finalization_wallet must be the source or one "
                "of flow_wallets",
            )

            if allocation_wallet is not None:
                _need(
                    finalization_wallet == allocation_wallet,
                    f"{where}.experimental multi-destination flows must "
                    "finalize through allocation_wallet",
                )

        else:
            _need(
                fl.get("finalization_wallet") is None,
                f"{where}.finalization_wallet is only valid with destinations",
            )

        if settlement_cycle_play:
            cycle = cfg["settlement_cycle"]

            _need(
                fl.get("allocation_wallet") is not None,
                f"{where}.allocation_wallet required for Settlement Cycle",
            )
            _need(
                cycle["reserve_return"]["from_wallet"]
                == fl["allocation_wallet"],
                "Settlement Cycle reserve return must originate from "
                "allocation_wallet",
            )
            _need(
                cycle["reserve_return"]["to_wallet"]
                == fl["source_wallet"],
                "Settlement Cycle reserve return must return to source_wallet",
            )

            expected_flow_wallets = [
                fl["allocation_wallet"],
                *cycle["hubs"],
                *cycle["workers"],
            ]

            _need(
                fl["flow_wallets"] == expected_flow_wallets,
                f"{where}.flow_wallets do not match the approved "
                "Settlement Cycle topology",
            )

            _need(
                cycle["settlement"]["source_wallet"]
                == fl["allocation_wallet"],
                "Settlement Cycle settlement source must be "
                "allocation_wallet",
            )

        fl.setdefault("description", "")
        if fl.get("repeat") is not None:
            _need(
                not has_destination_address and not has_destination_set,
                f"{where}.repeat currently requires one internal "
                "destination_wallet",
            )
            _need(fl.get("transfers") is None, f"{where}: use either repeat or transfers, not both")
            fl["transfers"] = _expand_repeat(fl, names, where)
            del fl["repeat"]

        has_transfers = fl.get("transfers") is not None
        has_experimental = fl.get("experimental_topology") is not None

        _need(
            not (has_transfers and has_experimental),
            f"{where}: use either explicit transfers or experimental_topology, not both",
        )

        if has_transfers:
            _validate_transfers(fl, names, where)

            if has_destination_address:
                transfers = fl["transfers"]
                _need(
                    transfers[-1]["to"] == legacy_destination,
                    f"{where}: final transfer must end at destination_address",
                )
                _need(
                    all(
                        t["from"] != legacy_destination
                        for t in transfers
                    ),
                    f"{where}: external destination_address cannot be a sender",
                )
                _need(
                    all(
                        t["to"] != legacy_destination
                        for t in transfers[:-1]
                    ),
                    f"{where}: destination_address may only appear in the final transfer",
                )

        if has_experimental:
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

    try:
        cfg["utxo_policy"] = validate_policy(cfg.get("utxo_policy"))
    except UtxoPolicyError as exc:
        raise ConfigError(f"invalid utxo_policy: {exc}") from exc

    rnd = cfg.get("randomization", {"enabled": False})
    _need(isinstance(rnd, dict) and isinstance(rnd.get("enabled"), bool),
          "randomization.enabled must be a bool")

    if settlement_cycle_play:
        _need(
            rnd.get("enabled") is True,
            "Settlement Cycle requires randomization.enabled=true",
        )
        _need(
            rnd.get("model") == "seeded_deterministic",
            "Settlement Cycle requires seeded_deterministic randomization",
        )
        _need(
            _is_int(rnd.get("seed")) and rnd["seed"] >= 0,
            "Settlement Cycle seed must be a non-negative integer",
        )

        cycle = cfg["settlement_cycle"]

        _need(
            isinstance(cycle["workers"], list)
            and len(cycle["workers"]) >= 2
            and all(
                isinstance(w, str) and w
                for w in cycle["workers"]
            ),
            "Settlement Cycle requires at least two workers",
        )
        _need(
            isinstance(cycle["hubs"], list)
            and all(
                isinstance(w, str) and w
                for w in cycle["hubs"]
            ),
            "Settlement Cycle hubs must be a list of wallet names",
        )

        for phase_name in ("outbound", "return"):
            phase = cycle[phase_name]

            _need(
                isinstance(phase, dict)
                and set(phase) == {
                    "decisions",
                    "amount_sats_min",
                    "amount_sats_max",
                    "delay_seconds_min",
                    "delay_seconds_max",
                    "multi_output",
                    "transitions",
                },
                f"Settlement Cycle {phase_name} contract is invalid",
            )
            _need(
                _is_int(phase["decisions"])
                and phase["decisions"] > 0,
                f"Settlement Cycle {phase_name}.decisions "
                "must be positive",
            )
            _need(
                phase["multi_output"] is False,
                f"Settlement Cycle {phase_name} V1 must use "
                "single-recipient workload decisions",
            )
            _need(
                _is_int(phase["amount_sats_min"])
                and phase["amount_sats_min"] > 0
                and _is_int(phase["amount_sats_max"])
                and phase["amount_sats_max"]
                >= phase["amount_sats_min"],
                f"Settlement Cycle {phase_name} amount bounds "
                "are invalid",
            )
            _need(
                _is_int(phase["delay_seconds_min"])
                and phase["delay_seconds_min"] >= 0
                and _is_int(phase["delay_seconds_max"])
                and phase["delay_seconds_max"]
                >= phase["delay_seconds_min"],
                f"Settlement Cycle {phase_name} delay bounds "
                "are invalid",
            )

            _need(
                isinstance(phase["transitions"], list)
                and phase["transitions"],
                f"Settlement Cycle {phase_name}.transitions "
                "must be a non-empty list",
            )

            for i, transition in enumerate(
                    phase["transitions"]
            ):
                _need(
                    isinstance(transition, dict)
                    and set(transition) == {"from", "to"}
                    and isinstance(transition["from"], str)
                    and bool(transition["from"])
                    and isinstance(transition["to"], str)
                    and bool(transition["to"])
                    and transition["from"] != transition["to"],
                    f"Settlement Cycle {phase_name}.transitions[{i}] "
                    "is invalid",
                )

            stage = fl["allocation_wallet"]
            workers = cycle["workers"]
            hubs = cycle["hubs"]

            if phase_name == "outbound":
                expected_transitions = [
                    *(
                        {"from": stage, "to": hub}
                        for hub in hubs
                    ),
                    *(
                        {"from": stage, "to": worker}
                        for worker in workers
                    ),
                    *(
                        {"from": hub, "to": worker}
                        for hub in hubs
                        for worker in workers
                    ),
                ]
            else:
                expected_transitions = [
                    *(
                        {"from": stage, "to": hub}
                        for hub in hubs
                    ),
                    *(
                        {"from": stage, "to": worker}
                        for worker in workers
                    ),
                    *(
                        {"from": hub, "to": worker}
                        for hub in hubs
                        for worker in workers
                    ),
                ]

            _need(
                phase["transitions"] == expected_transitions,
                f"Settlement Cycle {phase_name}.transitions "
                "do not match the approved topology",
            )

        settlement = cycle["settlement"]

        _need(
            isinstance(settlement, dict)
            and set(settlement) == {
                "source_wallet",
                "mode",
                "items",
                "retain_remainder",
                "delay_seconds_min",
                "delay_seconds_max",
            },
            "Settlement Cycle settlement contract is invalid",
        )
        _need(
            settlement["mode"] in DESTINATION_MODES,
            "Settlement Cycle settlement mode must be "
            "percentage or fixed",
        )
        _need(
            settlement["retain_remainder"] is True,
            "Settlement Cycle must retain a remainder",
        )
        _need(
            isinstance(settlement["items"], list)
            and 1 <= len(settlement["items"]) <= MAX_DESTINATIONS,
            "Settlement Cycle settlement requires 1..10 payouts",
        )

        settlement_targets = []
        settlement_total = 0

        for i, item in enumerate(settlement["items"]):
            w = f"settlement_cycle.settlement.items[{i}]"

            _need(isinstance(item, dict), f"{w} must be an object")

            kind = item.get("type")
            _need(
                kind in ("wallet", "address"),
                f"{w}.type must be wallet or address",
            )

            target_key = "wallet" if kind == "wallet" else "address"
            target = item.get(target_key)

            _need(
                isinstance(target, str) and target,
                f"{w}.{target_key} must be a non-empty string",
            )
            settlement_targets.append((kind, target))

            value_key = (
                "percent_bps"
                if settlement["mode"] == "percentage"
                else "amount_sats"
            )

            _need(
                set(item) == {"type", target_key, value_key},
                f"{w} has unsupported fields",
            )

            value = item[value_key]

            _need(
                _is_int(value) and value > 0,
                f"{w}.{value_key} must be a positive integer",
            )

            if settlement["mode"] == "percentage":
                _need(
                    value <= 10_000,
                    f"{w}.percent_bps must be <= 10000",
                )

            settlement_total += value

        _need(
            len(set(settlement_targets))
            == len(settlement_targets),
            "Settlement Cycle settlement contains duplicate targets",
        )

        smallest_alloc = min(
            fl["allocation_sats"]
            for fl in flows
        )

        if settlement["mode"] == "percentage":
            _need(
                settlement_total < 10_000,
                "Settlement Cycle payout percentages must total "
                "less than 10000 basis points",
            )
        else:
            _need(
                settlement_total < smallest_alloc,
                "Settlement Cycle fixed payouts must leave retained "
                "experiment value",
            )

        for lo, hi in (
            ("delay_seconds_min", "delay_seconds_max"),
        ):
            _need(
                _is_int(settlement[lo])
                and settlement[lo] >= 0
                and _is_int(settlement[hi])
                and settlement[hi] >= settlement[lo],
                "Settlement Cycle settlement delay bounds are invalid",
            )

        reserve_return = cycle["reserve_return"]

        _need(
            isinstance(reserve_return, dict)
            and set(reserve_return) == {
                "from_wallet",
                "to_wallet",
                "delay_seconds_min",
                "delay_seconds_max",
            },
            "Settlement Cycle reserve_return contract is invalid",
        )
        _need(
            _is_int(reserve_return["delay_seconds_min"])
            and reserve_return["delay_seconds_min"] >= 0
            and _is_int(reserve_return["delay_seconds_max"])
            and reserve_return["delay_seconds_max"]
            >= reserve_return["delay_seconds_min"],
            "Settlement Cycle reserve-return delay bounds are invalid",
        )
        _need(
            _is_int(cycle["max_total_transactions"])
            and cycle["max_total_transactions"] > 0,
            "Settlement Cycle max_total_transactions "
            "must be positive",
        )

        # Settlement Cycle owns its phase-specific workload contract.
        # It does not use the legacy top-level workload/finalization pair.
        _need(
            cfg.get("workload") is None,
            "Settlement Cycle must not define legacy workload",
        )
        _need(
            cfg.get("finalization") is None,
            "Settlement Cycle must not define legacy finalization",
        )

        return cfg

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

        finalization = cfg.get("finalization")
        _need(isinstance(finalization, dict),
              "finalization required when randomization is enabled")
        _need(set(finalization) == {"mode"},
              "finalization must contain exactly: mode")
        _need(
            finalization.get("mode") in FINALIZATION_MODES,
            f"finalization.mode must be one of {FINALIZATION_MODES}",
        )

        if any(has_multi_destinations(fl) for fl in flows):
            _need(
                finalization.get("mode") == "consolidate_then_distribute",
                "multi-destination randomized flows require "
                "finalization.mode=consolidate_then_distribute",
            )
        else:
            _need(
                finalization.get("mode") == "sweep_workers_to_destination",
                "legacy randomized flows require "
                "finalization.mode=sweep_workers_to_destination",
            )
    else:
        _need(cfg.get("finalization") is None,
              "finalization is only valid for randomized experimental configs")
        cfg["randomization"] = {"enabled": False}
    return cfg


def canonical(cfg):
    return json.dumps(cfg, sort_keys=True, separators=(",", ":"))


def config_hash(cfg):
    return hashlib.sha256(canonical(cfg).encode("utf-8")).hexdigest()
