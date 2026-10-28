"""Deterministic Mixer Lab research campaigns.

Executes existing synthetic scenarios through the public-only
common-input analyst and separate ground-truth evaluator.

No RPC, networking, wallet execution, or database persistence.
"""

from .mixer_scenarios import SCENARIOS, build_scenario
from .mixer_common_input import analyze_common_inputs
from .mixer_utxo_evaluation import evaluate_utxo_ownership


class CampaignError(ValueError):
    """Invalid research campaign request."""


COUNT_KEYS = (
    "tp",
    "fp",
    "tn",
    "fn",
    "abstained",
    "evaluated",
    "total",
)


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def _metrics(counts):
    tp = counts["tp"]
    fp = counts["fp"]
    tn = counts["tn"]
    fn = counts["fn"]

    return {
        "precision": _ratio(tp, tp + fp),
        "recall": _ratio(tp, tp + fn),
        "false_positive_rate": _ratio(fp, fp + tn),
        "coverage": _ratio(
            counts["evaluated"],
            counts["total"],
        ),
    }


def run_campaign(scenarios=SCENARIOS):
    """Run a selected set of deterministic synthetic scenarios.

    Aggregated classification metrics use pooled prediction counts.
    They are not averages of per-scenario metric values.

    This is a deterministic fixture campaign, not a statistical
    estimate of blockchain-wide ownership-inference accuracy.
    """

    if type(scenarios) not in (list, tuple):
        raise CampaignError("scenarios must be a list or tuple")

    if not scenarios:
        raise CampaignError("campaign requires scenarios")

    if len(scenarios) > len(SCENARIOS):
        raise CampaignError("too many scenarios")

    seen = set()

    for name in scenarios:
        if type(name) is not str or name not in SCENARIOS:
            raise CampaignError("unknown research scenario")

        if name in seen:
            raise CampaignError("duplicate research scenario")

        seen.add(name)

    results = []

    aggregate_counts = {
        key: 0 for key in COUNT_KEYS
    }

    composition = {
        name: 0 for name in SCENARIOS
    }

    for name in scenarios:
        # Synthetic ground truth remains separate from the
        # public observations consumed by the analyst.
        dataset = build_scenario(name)

        analysis = analyze_common_inputs(
            dataset["public"]
        )

        evaluation = evaluate_utxo_ownership(
            dataset["ground_truth"],
            analysis["predictions"],
        )

        counts = evaluation["counts"]

        for key in COUNT_KEYS:
            aggregate_counts[key] += counts[key]

        composition[name] += 1

        results.append({
            "scenario": name,
            "analyst_policy": analysis["policy"],
            "prediction_count": len(
                analysis["predictions"]
            ),
            "counts": dict(counts),
            "metrics": dict(evaluation["metrics"]),
        })

    return {
        "kind": "mixer_research_campaign",
        "version": 1,
        "execution": "simulation_only",
        "analyst": "common_input_same_owner_hypothesis",
        "scenarios": results,
        "aggregate": {
            "scenario_count": len(results),
            "scenario_composition": composition,
            "counts": aggregate_counts,
            "metrics": _metrics(aggregate_counts),
            "aggregation": "micro_average_over_submitted_pairs",
        },
    }
