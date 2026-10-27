"""Synthetic baseline UTXO simulator for FlowLab Mixer Lab.

No RPC, networking, wallet operations, or persistent database writes.
Public observations and private simulation ground truth are separate.
"""

import random

from .mixer_research import validate_research


class SimulationError(ValueError):
    """Simulation cannot satisfy its declared contract."""


def simulate_baseline(config):
    """Generate deterministic, single-owner synthetic transactions.

    This baseline performs one-input/one-output self-transfers.
    It establishes observable transaction ancestry, not privacy.
    """

    cfg = validate_research(config)

    if cfg["mode"] != "baseline":
        raise SimulationError("baseline simulator requires baseline mode")

    synthetic = cfg["synthetic"]
    rng = random.Random(cfg["seed"])

    participant_count = synthetic["participant_count"]
    utxos_each = synthetic["utxos_per_participant"]
    initial_value = synthetic["initial_value_sats"]
    fee = synthetic["network_fee_sats"]
    count = synthetic["transaction_count"]

    genesis = []
    transactions = []
    utxo_values = {}
    utxo_owners = {}
    transaction_owners = {}
    available = [[] for _ in range(participant_count)]
    eligible = []

    for participant in range(participant_count):
        owner = f"participant_{participant:04d}"

        for index in range(utxos_each):
            utxo_id = f"genesis:{participant:04d}:{index:06d}"
            genesis.append({
                "id": utxo_id,
                "value_sats": initial_value,
            })
            utxo_values[utxo_id] = initial_value
            utxo_owners[utxo_id] = owner

            if initial_value > fee:
                available[participant].append(utxo_id)

        if available[participant]:
            eligible.append(participant)

    total_fees = 0

    for index in range(count):
        if not eligible:
            raise SimulationError(
                "insufficient spendable synthetic value "
                "for requested transaction count"
            )

        participant = rng.choice(eligible)
        owner = f"participant_{participant:04d}"
        choices = available[participant]
        selected = rng.randrange(len(choices))
        input_id = choices.pop(selected)

        input_value = utxo_values[input_id]
        output_value = input_value - fee

        if output_value <= 0:
            raise SimulationError("nonpositive synthetic output")

        transaction_id = f"simtx:{index:08d}"
        output_id = f"{transaction_id}:0"

        transactions.append({
            "id": transaction_id,
            "sequence": index,
            "inputs": [input_id],
            "outputs": [{
                "id": output_id,
                "value_sats": output_value,
            }],
            "fee_sats": fee,
        })

        utxo_values[output_id] = output_value
        utxo_owners[output_id] = owner
        transaction_owners[transaction_id] = owner

        if output_value > fee:
            choices.append(output_id)

        if not choices:
            eligible.remove(participant)

        total_fees += fee

    initial_total = participant_count * utxos_each * initial_value
    remaining = initial_total - total_fees

    spent_ids = {
        utxo_id
        for tx in transactions
        for utxo_id in tx["inputs"]
    }

    unspent_utxos = [
        {
            "id": utxo_id,
            "value_sats": value,
        }
        for utxo_id, value in sorted(utxo_values.items())
        if utxo_id not in spent_ids
    ]

    independently_remaining = sum(
        item["value_sats"]
        for item in unspent_utxos
    )

    if remaining < 0 or independently_remaining != remaining:
        raise SimulationError("synthetic value conservation failed")

    return {
        "kind": "mixer_research_dataset",
        "version": 1,
        "execution": "simulation_only",
        "public": {
            "genesis_utxos": genesis,
            "transactions": transactions,
            "unspent_utxos": unspent_utxos,
        },
        "ground_truth": {
            "utxo_owners": utxo_owners,
            "transaction_owners": transaction_owners,
        },
        "summary": {
            "participant_count": participant_count,
            "initial_utxo_count": len(genesis),
            "transaction_count": len(transactions),
            "initial_value_sats": initial_total,
            "total_fees_sats": total_fees,
            "remaining_value_sats": remaining,
        },
    }
