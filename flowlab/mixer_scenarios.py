"""Controlled synthetic ownership scenarios for Mixer Lab.

Research-only transaction fixtures. Public records contain no
ownership labels. Ground truth is maintained separately.

No RPC, networking, transaction execution, or persistence.
"""

import hashlib


SCENARIOS = (
    "single_owner",
    "collaborative",
    "mixed_ownership",
)


class ScenarioError(ValueError):
    """Unsupported research scenario."""


def _genesis_id(scenario, index):
    material = f"mixer_scenario:v1:{scenario}:{index}"
    digest = hashlib.sha256(
        material.encode("utf-8")
    ).hexdigest()[:32]
    return f"genesis:{digest}"


def build_scenario(name):
    """Create a deterministic multi-input ownership scenario.

    single_owner: three inputs controlled by participant_a.
    collaborative: two inputs controlled by separate participants.
    mixed_ownership: two inputs controlled by participant_a,
                     one controlled by participant_b.

    These scenarios are test fixtures, not realistic samples
    of blockchain transaction frequency.
    """

    if type(name) is not str or name not in SCENARIOS:
        raise ScenarioError("unsupported ownership scenario")

    layouts = {
        "single_owner": [
            "participant_a",
            "participant_a",
            "participant_a",
        ],
        "collaborative": [
            "participant_a",
            "participant_b",
        ],
        "mixed_ownership": [
            "participant_a",
            "participant_a",
            "participant_b",
        ],
    }

    layout = layouts[name]

    genesis = []
    owners = {}
    inputs = []

    for index, owner in enumerate(layout):
        uid = _genesis_id(name, index)

        genesis.append({
            "id": uid,
            "value_sats": 100_000,
        })

        owners[uid] = owner
        inputs.append(uid)

    txid = "simtx:00000000"
    fee = 100 * len(layout)

    # Each participant's inputs are accounted for separately
    # in the synthetic ownership ledger.
    #
    # For this fixture, each input contributes 100 sats
    # toward the total network fee.
    by_owner = {}

    for owner in layout:
        by_owner[owner] = by_owner.get(owner, 0) + 1

    outputs = []

    for index, owner in enumerate(sorted(by_owner)):
        count = by_owner[owner]
        uid = f"{txid}:{index}"

        output = {
            "id": uid,
            "value_sats": count * 99_900,
        }

        outputs.append(output)
        owners[uid] = owner

    tx = {
        "id": txid,
        "sequence": 0,
        "inputs": inputs,
        "outputs": outputs,
        "fee_sats": fee,
    }

    return {
        "kind": "mixer_ownership_scenario",
        "version": 1,
        "execution": "simulation_only",
        "scenario": name,
        "public": {
            "genesis_utxos": genesis,
            "transactions": [tx],
            "unspent_utxos": [
                dict(item) for item in outputs
            ],
        },
        "ground_truth": {
            "utxo_owners": owners,
            "transaction_participants": {
                txid: sorted(set(layout)),
            },
        },
    }
