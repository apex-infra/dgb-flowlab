"""Deterministic synthetic multi-owner transaction fixtures.

Research-only datasets. No wallet access, RPC, or persistence.
"""


def multi_owner_fixture():
    """Return a two-owner, two-input, two-output synthetic transaction.

    This fixture deliberately invalidates the assumption that all
    inputs to a transaction necessarily have the same owner.
    """

    genesis = [
        {"id": "genesis:a:0", "value_sats": 100_000},
        {"id": "genesis:b:0", "value_sats": 100_000},
    ]

    tx = {
        "id": "simtx:0001",
        "sequence": 0,
        "inputs": [
            "genesis:a:0",
            "genesis:b:0",
        ],
        "outputs": [
            {"id": "simtx:0001:0", "value_sats": 99_900},
            {"id": "simtx:0001:1", "value_sats": 99_900},
        ],
        "fee_sats": 200,
    }

    outputs = [
        dict(item)
        for item in tx["outputs"]
    ]

    return {
        "kind": "mixer_research_fixture",
        "version": 1,
        "execution": "simulation_only",
        "public": {
            "genesis_utxos": genesis,
            "transactions": [tx],
            "unspent_utxos": outputs,
        },
        "ground_truth": {
            "utxo_owners": {
                "genesis:a:0": "participant_a",
                "genesis:b:0": "participant_b",
                "simtx:0001:0": "participant_a",
                "simtx:0001:1": "participant_b",
            },
            "transaction_participants": {
                "simtx:0001": [
                    "participant_a",
                    "participant_b",
                ],
            },
        },
    }
