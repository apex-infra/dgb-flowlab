"""Ground-truth evaluation for synthetic ownership predictions.

This module scores predictions only. It does not create predictions,
infer wallet ownership, access blockchain nodes, or execute transactions.
"""


class EvaluationError(ValueError):
    """Invalid ground-truth evaluation input."""


def _need(condition, message):
    if not condition:
        raise EvaluationError(message)


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def evaluate_ownership(ground_truth, predictions):
    """Score pairwise same-owner predictions.

    A positive prediction means same owner. A negative prediction means
    different owners. None represents abstention.

    Metrics are calculated over classified pairs only. Coverage and
    abstention counts describe how much of the submitted evaluation set
    was actually classified.
    """

    _need(
        type(ground_truth) is dict
        and set(ground_truth) == {"transaction_owners"},
        "ground truth requires transaction_owners",
    )
    owners = ground_truth["transaction_owners"]
    _need(
        type(owners) is dict and bool(owners),
        "transaction_owners must be a nonempty object",
    )
    for txid, owner in owners.items():
        _need(
            type(txid) is str and bool(txid)
            and type(owner) is str and bool(owner),
            "invalid ground-truth identifier",
        )
    _need(type(predictions) is list, "predictions must be a list")
    _need(
        len(predictions) <= 100_000,
        "too many ownership predictions",
    )

    counts = {
        "tp": 0, "fp": 0, "tn": 0, "fn": 0,
        "abstained": 0, "evaluated": 0, "total": 0,
    }
    seen = set()
    decisions = []

    for index, item in enumerate(predictions):
        _need(
            type(item) is dict
            and set(item) == {"left", "right", "same_owner"},
            f"prediction {index} has invalid fields",
        )
        left, right = item["left"], item["right"]
        verdict = item["same_owner"]
        _need(
            type(left) is str and type(right) is str
            and left in owners and right in owners,
            f"prediction {index} references unknown transaction",
        )
        _need(left != right, "self-comparison is not permitted")
        _need(
            verdict is None or type(verdict) is bool,
            "same_owner must be boolean or null",
        )

        pair = tuple(sorted((left, right)))
        _need(pair not in seen, "duplicate ownership prediction")
        seen.add(pair)
        actual = owners[left] == owners[right]

        if verdict is None:
            label = "abstained"
        elif verdict and actual:
            label = "tp"
        elif verdict and not actual:
            label = "fp"
        elif not verdict and actual:
            label = "fn"
        else:
            label = "tn"

        counts[label] += 1
        counts["total"] += 1
        if label != "abstained":
            counts["evaluated"] += 1

        decisions.append({
            "left": pair[0],
            "right": pair[1],
            "prediction": verdict,
            "actual_same_owner": actual,
            "classification": label,
        })

    decisions.sort(key=lambda item: (item["left"], item["right"]))

    tp, fp = counts["tp"], counts["fp"]
    tn, fn = counts["tn"], counts["fn"]

    return {
        "kind": "mixer_ownership_evaluation",
        "version": 1,
        "counts": counts,
        "metrics": {
            "precision": _ratio(tp, tp + fp),
            "recall": _ratio(tp, tp + fn),
            "false_positive_rate": _ratio(fp, fp + tn),
            "coverage": _ratio(
                counts["evaluated"], counts["total"]
            ),
        },
        "decisions": decisions,
    }
