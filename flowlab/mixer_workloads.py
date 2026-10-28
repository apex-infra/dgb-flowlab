"""Seeded synthetic transaction workloads for Mixer Lab.

Research-only independent multi-input transactions.
Ownership ground truth is stored separately from public data.

No wallet operations, RPC, networking, or persistence.
"""

import hashlib
import random

SCENARIOS = (
    "single_owner",
    "collaborative",
    "mixed_ownership",
)


class WorkloadError(ValueError):
    """Invalid seeded workload parameters."""


def _opaque_id(seed, index):
    # IDs do not directly encode participant names or owner indices.
    material = f"flowlab:workload:v1:{seed}:{index}"
    digest = hashlib.sha256(
        material.encode("utf-8")
    ).hexdigest()[:32]
    return f"genesis:{digest}"


def build_seeded_workload(
    seed=2049,
    transaction_count=30,
    max_inputs=6,
    scenario_weights=None,
    matched_input_profile=False,
):
    """Build reproducible independent synthetic transactions.

    Three ownership arrangements are distributed across transactions.
    The seed controls input counts, participant selection, and ordering.

    These are controlled experimental distributions, not estimates
    of activity on the actual DigiByte blockchain.
    """

    if (
        type(seed) is not int
        or not 0 <= seed <= 2**64 - 1
    ):
        raise WorkloadError("invalid seed")

    if (
        type(transaction_count) is not int
        or not 1 <= transaction_count <= 1000
    ):
        raise WorkloadError("invalid transaction count")

    if (
        type(max_inputs) is not int
        or not 3 <= max_inputs <= 12
    ):
        raise WorkloadError("invalid max inputs")

    if type(matched_input_profile) is not bool:
        raise WorkloadError("invalid matched_input_profile")

    rng = random.Random(seed)

    if matched_input_profile:
        # Independent size stream: changing ownership composition
        # does not change transaction input counts.
        from .mixer_input_profiles import build_input_profile

        input_profile = build_input_profile(
            seed=seed,
            transaction_count=transaction_count,
            max_inputs=max_inputs,
        )["input_counts"]
    else:
        input_profile = None

    # Preserve the legacy workload exactly when weights are omitted.
    # Custom distributions are deterministic but shuffled.
    if scenario_weights is None:
        scenario_schedule = None
    else:
        # Lazy import prevents a circular module dependency:
        # mixer_distribution imports SCENARIOS from this module.
        from .mixer_distribution import (
            DistributionError,
            build_distribution,
        )

        try:
            scenario_schedule = build_distribution(
                seed=seed,
                transaction_count=transaction_count,
                weights=scenario_weights,
            )["schedule"]
        except DistributionError as exc:
            raise WorkloadError(str(exc)) from exc

    genesis = []
    transactions = []
    unspent = []

    utxo_owners = {}
    transaction_participants = {}

    composition = {
        name: 0 for name in SCENARIOS
    }

    global_input_index = 0

    for sequence in range(transaction_count):
        scenario = (
            SCENARIOS[sequence % len(SCENARIOS)]
            if scenario_schedule is None
            else scenario_schedule[sequence]
        )
        composition[scenario] += 1

        input_count = (
            rng.randint(3, max_inputs)
            if input_profile is None
            else input_profile[sequence]
        )

        # A bounded synthetic participant universe.
        owner_pool = [
            f"participant_{i:04d}"
            for i in range(12)
        ]

        if scenario == "single_owner":
            owner = rng.choice(owner_pool)
            layout = [owner] * input_count

        elif scenario == "collaborative":
            # Every input is owned independently in this fixture.
            layout = rng.sample(owner_pool, input_count)

        else:
            # Mixed case: some inputs share an owner while at
            # least one input belongs to another participant.
            owner_a, owner_b = rng.sample(owner_pool, 2)
            layout = (
                [owner_a] * (input_count - 1)
                + [owner_b]
            )

        rng.shuffle(layout)

        inputs = []

        for owner in layout:
            uid = _opaque_id(seed, global_input_index)
            global_input_index += 1

            genesis.append({
                "id": uid,
                "value_sats": 100_000,
            })

            utxo_owners[uid] = owner
            inputs.append(uid)

        txid = f"simtx:{sequence:08d}"
        counts_by_owner = {}

        for owner in layout:
            counts_by_owner[owner] = (
                counts_by_owner.get(owner, 0) + 1
            )

        outputs = []

        for output_index, owner in enumerate(
            sorted(counts_by_owner)
        ):
            uid = f"{txid}:{output_index}"
            count = counts_by_owner[owner]

            output = {
                "id": uid,
                "value_sats": count * 99_900,
            }

            outputs.append(output)
            unspent.append(dict(output))
            utxo_owners[uid] = owner

        transactions.append({
            "id": txid,
            "sequence": sequence,
            "inputs": inputs,
            "outputs": outputs,
            "fee_sats": 100 * input_count,
        })

        transaction_participants[txid] = sorted(
            counts_by_owner
        )

    summary = {
        "seed": seed,
        "transaction_count": transaction_count,
        "max_inputs": max_inputs,
        "initial_utxo_count": len(genesis),
        "scenario_composition": composition,
    }

    if scenario_schedule is not None:
        summary["scenario_schedule"] = list(scenario_schedule)

    if matched_input_profile:
        summary["matched_input_profile"] = True

    return {
        "kind": "mixer_seeded_workload",
        "version": 1,
        "execution": "simulation_only",
        "public": {
            "genesis_utxos": genesis,
            "transactions": transactions,
            "unspent_utxos": unspent,
        },
        "ground_truth": {
            "utxo_owners": utxo_owners,
            "transaction_participants":
                transaction_participants,
        },
        "summary": summary,
    }
