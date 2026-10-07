"""
plays.py -- compile named research plays into ordinary FlowLab configs.

A play is only a higher-level configuration generator. The executor never
receives or interprets a play directly; compile_play() returns the same normal
experimental config that is already reviewed, hashed, approved, and executed.
"""

from copy import deepcopy

from .config_schema import validate


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
        for name in ("random_walk", "ring", "hub_and_spoke")
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


def _validate_params(name, params):
    _need(name in PLAY_SPECS, f"unknown play: {name}")
    _need(isinstance(params, dict), "play parameters must be an object")

    missing = sorted(_REQUIRED_PARAMS - set(params))
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
    decisions = params["decisions"]
    min_delay = params["delay_seconds_min"]
    max_delay = params["delay_seconds_max"]
    confirmations = params["confirmations_required"]
    seed = params["seed"]

    _need(_is_int(allocation) and allocation > 0, "allocation_sats must be a positive integer")
    _need(_is_int(minimum) and minimum > 0, "amount_sats_min must be a positive integer")
    _need(_is_int(maximum) and maximum >= minimum, "amount_sats_max must be >= amount_sats_min")
    _need(maximum <= allocation, "amount_sats_max cannot exceed allocation_sats")
    _need(_is_int(decisions) and decisions > 0, "decisions must be a positive integer")
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


def compile_play(name, params):
    """
    Compile a named play into the existing experimental FlowLab config schema.

    The returned object contains no play-specific execution semantics.
    """
    _validate_params(name, params)

    src = params["source_wallet"]
    stage = params["allocation_wallet"]
    workers = list(params["workers"])
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
