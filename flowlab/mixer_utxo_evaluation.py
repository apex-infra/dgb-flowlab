"""Synthetic UTXO ownership evaluation for Mixer Lab.

Reuses existing classification metrics without altering the
transaction-level ownership evaluator.

No wallet RPC, networking, transaction execution, or persistence.
"""

from .mixer_evaluation import (
    EvaluationError,
    evaluate_ownership,
)


class UtxoEvaluationError(ValueError):
    """Invalid UTXO ownership evaluation."""


def evaluate_utxo_ownership(ground_truth, predictions):
    """Evaluate pairwise same-owner predictions for synthetic UTXOs.

    The accepted ground truth may come from either the baseline
    simulator or the multi-owner research fixture.

    Only utxo_owners is used and validated for scoring.
    Optional transaction_owners and transaction_participants fields
    are accepted for compatibility but are not inspected or verified.
    Callers must not interpret successful evaluation as validation
    of that auxiliary metadata.

    Predictions use UTXO IDs, never transaction IDs.
    """

    if type(ground_truth) is not dict:
        raise UtxoEvaluationError(
            "ground truth must be an object"
        )

    allowed = {
        "utxo_owners",
        "transaction_owners",
        "transaction_participants",
    }

    if not set(ground_truth).issubset(allowed):
        raise UtxoEvaluationError(
            "ground truth contains unsupported fields"
        )

    owners = ground_truth.get("utxo_owners")

    if type(owners) is not dict or not owners:
        raise UtxoEvaluationError(
            "ground truth requires utxo_owners"
        )

    # The existing evaluator has deliberately strict validation,
    # pair normalization, duplicate detection, and metrics.
    # Reuse it instead of introducing divergent calculations.

    try:
        result = evaluate_ownership(
            {"transaction_owners": owners},
            predictions,
        )
    except EvaluationError as exc:
        raise UtxoEvaluationError(str(exc)) from exc

    return {
        "kind": "mixer_utxo_ownership_evaluation",
        "version": 1,
        "counts": result["counts"],
        "metrics": result["metrics"],
        "decisions": result["decisions"],
    }
