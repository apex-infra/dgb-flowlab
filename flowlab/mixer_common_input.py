"""Synthetic common-input ownership heuristic research.

Produces predictions from validated public data only.

The heuristic is not proof of ownership.
No wallet RPC, broadcasting, networking, or persistence.
"""

from itertools import combinations

from .mixer_analysis import analyze_ancestry


def analyze_common_inputs(public):
    """Predict common ownership for inputs spent together.

    Only explicit co-input relationships are considered.
    No transitive clustering or output ownership inference is
    performed.

    The analyzer never receives ground-truth ownership records.
    """

    # Validate UTXO existence, spending, fees, and conservation.
    analyze_ancestry(public)

    max_predictions = 100_000
    pairs = set()

    for tx in public["transactions"]:
        inputs = sorted(tx["inputs"])

        # Each UTXO can be spent only once, so co-input pairs
        # cannot recur in another valid transaction.
        candidate_count = len(inputs) * (len(inputs) - 1) // 2

        if len(pairs) + candidate_count > max_predictions:
            raise ValueError(
                "common-input prediction limit exceeded"
            )

        for left, right in combinations(inputs, 2):
            pairs.add((left, right))

    predictions = [
        {
            "left": left,
            "right": right,
            "same_owner": True,
        }
        for left, right in sorted(pairs)
    ]

    return {
        "kind": "mixer_common_input_analysis",
        "version": 1,
        "policy": "common_input_same_owner_hypothesis",
        "predictions": predictions,
        "summary": {
            "transaction_count": len(public["transactions"]),
            "prediction_count": len(predictions),
        },
    }
