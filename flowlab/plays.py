"""
plays.py -- compile named research plays into ordinary FlowLab configs.

A play is only a higher-level configuration generator. The executor never
receives or interprets a play directly; compile_play() returns the same normal
experimental config that is already reviewed, hashed, approved, and executed.
"""

from copy import deepcopy
import hashlib
import random

from .config_schema import settlement_cycle_identity, validate


class PlayError(ValueError):
    pass


PLAY_SPECS = {
    "random_walk": {
        "name": "random_walk",
        "title": "Random Walk",
        "description": (
            "Seeded transitions from the source into the worker set, followed "
            "by broad worker-to-worker routing without self-transfers."
        ),
        "min_workers": 1,
    },
    "ring": {
        "name": "ring",
        "title": "Ring",
        "description": (
            "Seeded circulation over an approved directed worker cycle, with "
            "a single source entry edge into the ring."
        ),
        "min_workers": 2,
    },
    "hub_and_spoke": {
        "name": "hub_and_spoke",
        "title": "Hub-and-Spoke",
        "description": (
            "A dedicated hub receives the allocation entry and connects to "
            "approved spokes in both directions."
        ),
        "min_workers": 1,
        "requires_hub": True,
    },
    "fan_out_fan_in": {
        "name": "fan_out_fan_in",
        "title": "Fan-Out / Fan-In",
        "description": (
            "Commit the allocation to Stage, distribute seeded amounts across "
            "approved workers, then sweep every branch back into Stage before "
            "the terminal destination."
        ),
        "min_workers": 2,
    },
    "settlement_cycle": {
        "name": "settlement_cycle",
        "title": "Settlement Cycle",
        "description": (
            "Run a seeded outbound workload, consolidate for an intermediate "
            "multi-output settlement, retain part of the experiment principal, "
            "run a seeded return workload, and return the remainder to Reserve."
        ),
        "min_workers": 2,
    },
}


_REQUIRED_PARAMS = {
    "source_wallet",
    "allocation_wallet",
    "workers",
    "allocation_sats",
    "decisions",
    "amount_sats_min",
    "amount_sats_max",
    "delay_seconds_min",
    "delay_seconds_max",
    "confirmations_required",
    "seed",
}


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _need(cond, msg):
    if not cond:
        raise PlayError(msg)


def list_plays():
    """Return public play metadata in stable catalog order."""
    return [
        deepcopy(PLAY_SPECS[name])
        for name in (
            "random_walk",
            "ring",
            "hub_and_spoke",
            "fan_out_fan_in",
            "settlement_cycle",
        )
    ]


def get_play_spec(name):
    """Return public metadata for one play."""
    try:
        return deepcopy(PLAY_SPECS[name])
    except KeyError as exc:
        raise PlayError(f"unknown play: {name}") from exc


def _destination_spec(params):
    has_set = isinstance(params.get("destinations"), dict)
    has_wallet = (
        isinstance(params.get("destination_wallet"), str)
        and bool(params["destination_wallet"])
    )
    has_address = (
        isinstance(params.get("destination_address"), str)
        and bool(params["destination_address"])
    )

    _need(
        sum((
            bool(has_set),
            bool(has_wallet),
            bool(has_address),
        )) == 1,
        "play requires exactly one destination definition: "
        "destination_wallet, destination_address, or destinations",
    )

    if has_set:
        return deepcopy(params["destinations"])

    if has_wallet:
        return {
            "mode": "percentage",
            "items": [{
                "type": "wallet",
                "wallet": params["destination_wallet"],
                "percent_bps": 10_000,
            }],
        }

    return {
        "mode": "percentage",
        "items": [{
            "type": "address",
            "address": params["destination_address"],
            "percent_bps": 10_000,
        }],
    }


def _validate_settlement_cycle_params(params):
    required = {
        "source_wallet",
        "allocation_wallet",
        "workers",
        "hubs",
        "allocation_sats",
        "outbound_decisions",
        "return_decisions",
        "amount_sats_min",
        "amount_sats_max",
        "delay_seconds_min",
        "delay_seconds_max",
        "settlement_delay_seconds_min",
        "settlement_delay_seconds_max",
        "reserve_return_delay_seconds_min",
        "reserve_return_delay_seconds_max",
        "confirmations_required",
        "seed",
        "max_total_transactions",
        "settlement",
    }

    missing = sorted(required - set(params))
    _need(
        not missing,
        "missing play parameters: " + ", ".join(missing),
    )

    source = params["source_wallet"]
    stage = params["allocation_wallet"]
    workers = params["workers"]
    hubs = params["hubs"]

    _need(
        isinstance(source, str) and source,
        "source_wallet is required",
    )
    _need(
        isinstance(stage, str) and stage,
        "allocation_wallet is required",
    )
    _need(
        isinstance(workers, list)
        and all(isinstance(w, str) and w for w in workers),
        "workers must be a list of wallet names",
    )
    _need(
        len(workers) >= 2,
        "settlement_cycle requires at least 2 workers",
    )
    _need(
        isinstance(hubs, list)
        and all(isinstance(w, str) and w for w in hubs),
        "hubs must be a list of wallet names",
    )

    names = [source, stage, *workers, *hubs]
    _need(
        len(set(names)) == len(names),
        "Reserve, Stage, workers, and hubs must all be distinct",
    )

    allocation = params["allocation_sats"]
    minimum = params["amount_sats_min"]
    maximum = params["amount_sats_max"]

    _need(
        _is_int(allocation) and allocation > 0,
        "allocation_sats must be a positive integer",
    )
    _need(
        _is_int(minimum) and minimum > 0,
        "amount_sats_min must be a positive integer",
    )
    _need(
        _is_int(maximum) and maximum >= minimum,
        "amount_sats_max must be >= amount_sats_min",
    )
    _need(
        maximum <= allocation,
        "amount_sats_max cannot exceed allocation_sats",
    )

    for key in ("outbound_decisions", "return_decisions"):
        _need(
            _is_int(params[key]) and params[key] > 0,
            f"{key} must be a positive integer",
        )

    for lo, hi in (
        ("delay_seconds_min", "delay_seconds_max"),
        (
            "settlement_delay_seconds_min",
            "settlement_delay_seconds_max",
        ),
        (
            "reserve_return_delay_seconds_min",
            "reserve_return_delay_seconds_max",
        ),
    ):
        _need(
            _is_int(params[lo]) and params[lo] >= 0,
            f"{lo} must be a non-negative integer",
        )
        _need(
            _is_int(params[hi]) and params[hi] >= params[lo],
            f"{hi} must be >= {lo}",
        )

    _need(
        _is_int(params["confirmations_required"])
        and params["confirmations_required"] >= 1,
        "confirmations_required must be an integer >= 1",
    )
    _need(
        _is_int(params["seed"]) and params["seed"] >= 0,
        "seed must be a non-negative integer",
    )
    _need(
        _is_int(params["max_total_transactions"])
        and params["max_total_transactions"] > 0,
        "max_total_transactions must be a positive integer",
    )

    settlement = params["settlement"]

    _need(
        isinstance(settlement, dict),
        "settlement must be an object",
    )
    _need(
        set(settlement) == {"mode", "items"},
        "settlement must contain exactly: mode, items",
    )
    _need(
        settlement["mode"] in ("fixed", "percentage"),
        "settlement.mode must be fixed or percentage",
    )

    items = settlement["items"]

    _need(
        isinstance(items, list) and 1 <= len(items) <= 10,
        "settlement.items must contain 1..10 payouts",
    )

    targets = []
    fixed_total = 0
    percent_total = 0

    for i, item in enumerate(items):
        where = f"settlement.items[{i}]"

        _need(
            isinstance(item, dict),
            f"{where} must be an object",
        )

        kind = item.get("type")
        _need(
            kind in ("wallet", "address"),
            f"{where}.type must be wallet or address",
        )

        target_key = "wallet" if kind == "wallet" else "address"
        target = item.get(target_key)

        _need(
            isinstance(target, str) and target,
            f"{where}.{target_key} must be a non-empty string",
        )

        targets.append((kind, target))

        if settlement["mode"] == "fixed":
            _need(
                set(item) == {"type", target_key, "amount_sats"},
                f"{where} must contain exactly: "
                f"type, {target_key}, amount_sats",
            )

            amount = item["amount_sats"]

            _need(
                _is_int(amount) and amount > 0,
                f"{where}.amount_sats must be a positive integer",
            )

            fixed_total += amount

        else:
            _need(
                set(item) == {"type", target_key, "percent_bps"},
                f"{where} must contain exactly: "
                f"type, {target_key}, percent_bps",
            )

            bps = item["percent_bps"]

            _need(
                _is_int(bps) and 1 <= bps <= 10_000,
                f"{where}.percent_bps must be an integer in 1..10000",
            )

            percent_total += bps

    _need(
        len(set(targets)) == len(targets),
        "settlement contains duplicate payout targets",
    )

    if settlement["mode"] == "fixed":
        _need(
            fixed_total < allocation,
            "fixed settlement payouts must leave retained experiment value",
        )
    else:
        _need(
            percent_total < 10_000,
            "percentage settlement payouts must total less than 100% "
            "so experiment value is retained",
        )


def _validate_params(name, params):
    _need(name in PLAY_SPECS, f"unknown play: {name}")
    _need(isinstance(params, dict), "play parameters must be an object")

    if name == "settlement_cycle":
        _validate_settlement_cycle_params(params)
        return

    required = set(_REQUIRED_PARAMS)

    if name == "fan_out_fan_in":
        required.discard("decisions")

    missing = sorted(required - set(params))
    _need(not missing, f"missing play parameters: {', '.join(missing)}")

    src = params["source_wallet"]
    stage = params["allocation_wallet"]
    workers = params["workers"]

    destination_spec = _destination_spec(params)

    _need(isinstance(src, str) and src, "source_wallet is required")
    _need(isinstance(stage, str) and stage, "allocation_wallet is required")
    _need(
        isinstance(workers, list)
        and all(isinstance(w, str) and w for w in workers),
        "workers must be a list of wallet names",
    )

    min_workers = PLAY_SPECS[name]["min_workers"]
    _need(
        len(workers) >= min_workers,
        f"{name} requires at least {min_workers} worker"
        + ("" if min_workers == 1 else "s"),
    )

    extra_wallets = []

    if name == "hub_and_spoke":
        hub = params.get("hub_wallet")
        _need(
            isinstance(hub, str) and hub,
            "hub_wallet is required for hub_and_spoke",
        )
        extra_wallets.append(hub)

    internal_destinations = []

    items = destination_spec.get("items")
    if isinstance(items, list):
        for item in items:
            if (
                isinstance(item, dict)
                and item.get("type") == "wallet"
                and isinstance(item.get("wallet"), str)
                and item["wallet"]
            ):
                internal_destinations.append(item["wallet"])

    names = [
        src,
        stage,
        *extra_wallets,
        *workers,
        *internal_destinations,
    ]

    _need(
        len(set(names)) == len(names),
        "source, allocation wallet, play wallets, and internal "
        "destinations must all be distinct",
    )

    allocation = params["allocation_sats"]
    minimum = params["amount_sats_min"]
    maximum = params["amount_sats_max"]
    decisions = params.get("decisions")
    min_delay = params["delay_seconds_min"]
    max_delay = params["delay_seconds_max"]
    confirmations = params["confirmations_required"]
    seed = params["seed"]

    _need(_is_int(allocation) and allocation > 0, "allocation_sats must be a positive integer")
    _need(_is_int(minimum) and minimum > 0, "amount_sats_min must be a positive integer")
    _need(_is_int(maximum) and maximum >= minimum, "amount_sats_max must be >= amount_sats_min")
    _need(maximum <= allocation, "amount_sats_max cannot exceed allocation_sats")
    if name != "fan_out_fan_in":
        _need(
            _is_int(decisions) and decisions > 0,
            "decisions must be a positive integer",
        )

    _need(_is_int(min_delay) and min_delay >= 0, "delay_seconds_min must be >= 0")
    _need(_is_int(max_delay) and max_delay >= min_delay, "delay_seconds_max must be >= delay_seconds_min")
    _need(
        _is_int(confirmations) and confirmations >= 1,
        "confirmations_required must be an integer >= 1",
    )
    _need(_is_int(seed) and seed >= 0, "seed must be a non-negative integer")


def _random_walk_transitions(src, workers):
    transitions = [{"from": src, "to": worker} for worker in workers]

    for sender in workers:
        for receiver in workers:
            if sender != receiver:
                transitions.append({"from": sender, "to": receiver})

    return transitions


def _ring_transitions(src, workers):
    transitions = [{"from": src, "to": workers[0]}]

    for i, sender in enumerate(workers):
        receiver = workers[(i + 1) % len(workers)]
        transitions.append({"from": sender, "to": receiver})

    return transitions


def _hub_and_spoke_transitions(src, hub, workers):
    transitions = [{"from": src, "to": hub}]

    for worker in workers:
        transitions.append({"from": hub, "to": worker})

    for worker in workers:
        transitions.append({"from": worker, "to": hub})

    return transitions


def _fan_out_fan_in_transfers(params):
    """Return one reproducible ordered Fan-Out/Fan-In transfer lifecycle."""
    source = params["source_wallet"]
    stage = params["allocation_wallet"]
    workers = list(params["workers"])
    allocation = params["allocation_sats"]

    material = (
        f"flowlab-fan-out-fan-in-v1:{params['seed']}"
    ).encode()
    derived = int.from_bytes(hashlib.sha256(material).digest(), "big")
    rng = random.Random(derived)

    rng.shuffle(workers)

    fanout = []
    total_fanout = 0

    for worker in workers:
        amount = rng.randint(
            params["amount_sats_min"],
            params["amount_sats_max"],
        )
        total_fanout += amount

        fanout.append({
            "from": stage,
            "to": worker,
            "amount_sats": amount,
            "delay_seconds": rng.randint(
                params["delay_seconds_min"],
                params["delay_seconds_max"],
            ),
        })

    _need(
        total_fanout < allocation,
        "fan_out_fan_in seeded fan-out total must be less than allocation_sats "
        "so Stage retains fee headroom",
    )

    fanin = [
        {
            "from": worker,
            "to": stage,
            "amount_sats": "all",
            "delay_seconds": rng.randint(
                params["delay_seconds_min"],
                params["delay_seconds_max"],
            ),
        }
        for worker in workers
    ]

    destination = params.get("destination_wallet")
    if not destination:
        destination = params.get("destination_address")

    _need(
        isinstance(destination, str) and destination,
        "fan_out_fan_in v1 requires one destination_wallet or "
        "destination_address",
    )

    return [
        {
            "from": source,
            "to": stage,
            "amount_sats": allocation,
            "delay_seconds": 0,
        },
        *fanout,
        *fanin,
        {
            "from": stage,
            "to": destination,
            "amount_sats": "all",
            "delay_seconds": rng.randint(
                params["delay_seconds_min"],
                params["delay_seconds_max"],
            ),
        },
    ]


def _compile_settlement_cycle(params):
    source = params["source_wallet"]
    stage = params["allocation_wallet"]
    workers = list(params["workers"])
    hubs = list(params["hubs"])

    outbound_transitions = [
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

    return_transitions = [
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

    cycle = {
        "workers": workers,
        "hubs": hubs,
        "outbound": {
            "decisions": params["outbound_decisions"],
            "amount_sats_min": params["amount_sats_min"],
            "amount_sats_max": params["amount_sats_max"],
            "delay_seconds_min": params["delay_seconds_min"],
            "delay_seconds_max": params["delay_seconds_max"],
            "multi_output": True,
            "transitions": outbound_transitions,
        },
        "settlement": {
            "source_wallet": stage,
            "mode": params["settlement"]["mode"],
            "items": deepcopy(params["settlement"]["items"]),
            "retain_remainder": True,
            "delay_seconds_min":
                params["settlement_delay_seconds_min"],
            "delay_seconds_max":
                params["settlement_delay_seconds_max"],
        },
        "return": {
            "decisions": params["return_decisions"],
            "amount_sats_min": params["amount_sats_min"],
            "amount_sats_max": params["amount_sats_max"],
            "delay_seconds_min": params["delay_seconds_min"],
            "delay_seconds_max": params["delay_seconds_max"],
            "multi_output": True,
            "transitions": return_transitions,
        },
        "reserve_return": {
            "from_wallet": stage,
            "to_wallet": source,
            "delay_seconds_min":
                params["reserve_return_delay_seconds_min"],
            "delay_seconds_max":
                params["reserve_return_delay_seconds_max"],
        },
        "max_total_transactions":
            params["max_total_transactions"],
    }

    flow = {
        "description": PLAY_SPECS["settlement_cycle"]["title"],
        "source_wallet": source,
        "allocation_wallet": stage,
        "flow_wallets": [stage, *hubs, *workers],
        "allocation_sats": params["allocation_sats"],
    }

    flow["flow_identity"] = settlement_cycle_identity(
        flow,
        cycle,
    )

    cfg = {
        "play": {
            "name": "settlement_cycle",
            "version": 1,
        },
        "flows": [flow],
        "settlement_cycle": cycle,
        "confirmations_required":
            params["confirmations_required"],
        "fee_policy": {
            "type": "minimum",
        },
        "address_policy": "new",
        "randomization": {
            "enabled": True,
            "model": "seeded_deterministic",
            "seed": params["seed"],
        },
    }

    try:
        return validate(cfg)
    except Exception as exc:
        raise PlayError(
            f"compiled play is invalid: {exc}"
        ) from exc


def compile_play(name, params):
    """
    Compile a named play into the existing experimental FlowLab config schema.

    The returned object contains no play-specific execution semantics.
    """
    _validate_params(name, params)

    if name == "settlement_cycle":
        return _compile_settlement_cycle(params)

    src = params["source_wallet"]
    stage = params["allocation_wallet"]
    workers = list(params["workers"])

    if name == "fan_out_fan_in":
        _need(
            params.get("destinations") is None,
            "fan_out_fan_in v1 does not support multi-destination settlement",
        )

        flow = {
            "description": PLAY_SPECS[name]["title"],
            "source_wallet": src,
            "allocation_wallet": stage,
            "flow_wallets": [stage, *workers],
            "allocation_sats": params["allocation_sats"],
            "transfers": _fan_out_fan_in_transfers(params),
        }

        if params.get("destination_wallet"):
            flow["destination_wallet"] = params["destination_wallet"]
        else:
            flow["destination_address"] = params["destination_address"]

        cfg = {
            "play": {
                "name": "fan_out_fan_in",
                "version": 1,
            },
            "flows": [flow],
            "confirmations_required": params["confirmations_required"],
            "fee_policy": {
                "type": "minimum",
            },
            "address_policy": "new",
            "randomization": {
                "enabled": False,
            },
        }

        try:
            return validate(cfg)
        except Exception as exc:
            raise PlayError(
                f"compiled play is invalid: {exc}"
            ) from exc

    destinations = _destination_spec(params)
    hub = None

    if name == "random_walk":
        transitions = _random_walk_transitions(stage, workers)
    elif name == "ring":
        transitions = _ring_transitions(stage, workers)
    elif name == "hub_and_spoke":
        hub = params["hub_wallet"]
        transitions = _hub_and_spoke_transitions(stage, hub, workers)
    else:
        raise PlayError(f"unknown play: {name}")

    workload_wallets = [stage]
    if hub is not None:
        workload_wallets.append(hub)
    workload_wallets.extend(workers)

    cfg = {
        "flows": [{
            "description": PLAY_SPECS[name]["title"],
            "source_wallet": src,
            "allocation_wallet": stage,
            "finalization_wallet": stage,
            "flow_wallets": workload_wallets,
            "destinations": destinations,
            "allocation_sats": params["allocation_sats"],
            "experimental_topology": {
                "transitions": transitions,
            },
        }],
        "workload": {
            "mode": "count",
            "jobs": params["decisions"],
            "amount_sats_min": params["amount_sats_min"],
            "amount_sats_max": params["amount_sats_max"],
            "delay_seconds_min": params["delay_seconds_min"],
            "delay_seconds_max": params["delay_seconds_max"],
        },
        "confirmations_required": params["confirmations_required"],
        "fee_policy": {
            "type": "minimum",
        },
        "address_policy": "new",
        "randomization": {
            "enabled": True,
            "model": "uniform",
            "seed": params["seed"],
        },
        "finalization": {
            "mode": "consolidate_then_distribute",
        },
    }

    try:
        return validate(cfg)
    except Exception as exc:
        raise PlayError(f"compiled play is invalid: {exc}") from exc
