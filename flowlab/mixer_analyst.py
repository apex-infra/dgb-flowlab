"""Conservative public-only Mixer Lab research analyst.

Reports observable transaction ancestry without inferring ownership.
No RPC, networking, ground-truth access, or persistent writes.
"""

from .mixer_analysis import analyze_ancestry


class AnalystError(ValueError):
    """Invalid analyst request."""


def _need(condition, message):
    if not condition:
        raise AnalystError(message)


def analyze_pairs(public, pairs):
    """Assess transaction-pair evidence with explicit abstention.

    This baseline deliberately never infers common ownership.
    Only validated public transaction ancestry is reported.
    """

    ancestry = analyze_ancestry(public)
    transactions = ancestry["transactions"]

    _need(type(pairs) is list, "pairs must be a list")
    _need(len(pairs) <= 100_000, "too many pairs")

    seen = set()
    predictions = []
    evidence = []

    for index, pair in enumerate(pairs):
        _need(
            type(pair) in (list, tuple) and len(pair) == 2,
            f"pair {index} must contain two transaction IDs",
        )

        left, right = pair
        _need(
            type(left) is str and type(right) is str
            and left in transactions and right in transactions,
            f"pair {index} references unknown transaction",
        )
        _need(left != right, "self-comparison is not permitted")

        key = tuple(sorted((left, right)))
        _need(key not in seen, "duplicate transaction pair")
        seen.add(key)

        canonical_left, canonical_right = key

        left_ancestors = transactions[canonical_left]["ancestors"]
        right_ancestors = transactions[canonical_right]["ancestors"]

        if canonical_left in right_ancestors:
            relationship = "ancestor"
        elif canonical_right in left_ancestors:
            relationship = "descendant"
        else:
            relationship = "unconnected"

        predictions.append({
            "left": key[0],
            "right": key[1],
            "same_owner": None,
        })
        evidence.append({
            "left": key[0],
            "right": key[1],
            "relationship": relationship,
        })

    predictions.sort(key=lambda p: (p["left"], p["right"]))
    evidence.sort(key=lambda p: (p["left"], p["right"]))

    return {
        "kind": "mixer_public_analyst",
        "version": 1,
        "policy": "ancestry_evidence_abstain_on_ownership",
        "predictions": predictions,
        "evidence": evidence,
    }
