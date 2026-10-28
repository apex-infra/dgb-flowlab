"""Multi-seed synthetic research campaign runner.

Ground truth is passed only to evaluation, never to the analyst.
No blockchain RPC, networking, execution, or persistence.
"""

from .mixer_workloads import (
    SCENARIOS,
    WorkloadError,
    build_seeded_workload,
)
from .mixer_common_input import analyze_common_inputs
from .mixer_utxo_evaluation import evaluate_utxo_ownership


class SeededCampaignError(ValueError):
    """Invalid seeded campaign configuration."""


COUNT_KEYS = (
    "tp", "fp", "tn", "fn",
    "abstained", "evaluated", "total",
)

MAX_CAMPAIGN_PREDICTIONS = 100_000


def _empty_counts():
    return {key: 0 for key in COUNT_KEYS}


def _add_counts(destination, source):
    for key in COUNT_KEYS:
        destination[key] += source[key]


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def _metrics(counts):
    tp, fp = counts["tp"], counts["fp"]
    tn, fn = counts["tn"], counts["fn"]

    return {
        "precision": _ratio(tp, tp + fp),
        "recall": _ratio(tp, tp + fn),
        "false_positive_rate": _ratio(fp, fp + tn),
        "coverage": _ratio(
            counts["evaluated"], counts["total"]
        ),
    }


def run_seeded_campaign(
    seeds=(42, 2049, 9999),
    transaction_count=30,
    max_inputs=6,
    scenario_weights=None,
):
    """Evaluate generated workloads across multiple seeds.

    Per-scenario results are attributed by the workload generator's
    fixture contract: transaction sequence modulo scenario count.

    Results are descriptive of controlled synthetic workloads,
    not population estimates for real-world blockchain activity.
    """

    if type(seeds) not in (list, tuple) or not 1 <= len(seeds) <= 8:
        raise SeededCampaignError("invalid seed collection")

    seen = set()
    for seed in seeds:
        if type(seed) is not int or not 0 <= seed <= 2**64 - 1:
            raise SeededCampaignError("invalid seed")
        if seed in seen:
            raise SeededCampaignError("duplicate seed")
        seen.add(seed)

    if type(transaction_count) is not int or not (
        1 <= transaction_count <= 1000
    ):
        raise SeededCampaignError("invalid transaction_count")

    if type(max_inputs) is not int or not (
        3 <= max_inputs <= 12
    ):
        raise SeededCampaignError("invalid max_inputs")

    # Conservative upper bound before generating any workload.
    max_pairs_per_tx = max_inputs * (max_inputs - 1) // 2

    if (
        len(seeds) * transaction_count * max_pairs_per_tx
        > MAX_CAMPAIGN_PREDICTIONS
    ):
        raise SeededCampaignError(
            "campaign prediction budget exceeded"
        )

    if scenario_weights is not None:
        # Validate the distribution before generating workloads.
        from .mixer_distribution import (
            DistributionError,
            build_distribution,
        )

        try:
            build_distribution(
                seed=seeds[0],
                transaction_count=transaction_count,
                weights=scenario_weights,
            )
        except DistributionError as exc:
            raise SeededCampaignError(str(exc)) from exc

    aggregate = _empty_counts()
    scenario_totals = {
        name: {
            "transaction_count": 0,
            "counts": _empty_counts(),
        }
        for name in SCENARIOS
    }
    seed_results = []

    for seed in seeds:
        try:
            dataset = build_seeded_workload(
                seed=seed,
                transaction_count=transaction_count,
                max_inputs=max_inputs,
                scenario_weights=scenario_weights,
            )
        except WorkloadError as exc:
            raise SeededCampaignError(str(exc)) from exc

        public = dataset["public"]
        analysis = analyze_common_inputs(public)

        evaluation = evaluate_utxo_ownership(
            dataset["ground_truth"],
            analysis["predictions"],
        )

        seed_counts = dict(evaluation["counts"])
        _add_counts(aggregate, seed_counts)

        # Each input belongs to precisely one transaction.
        # Map co-input predictions to their originating scenario.
        input_scenarios = {}

        # The schedule is research metadata, not analyst input.
        # Legacy workloads retain their original sequence mapping.
        schedule = dataset["summary"].get("scenario_schedule")

        if schedule is None:
            schedule = [
                SCENARIOS[index % len(SCENARIOS)]
                for index in range(len(public["transactions"]))
            ]

        if len(schedule) != len(public["transactions"]):
            raise SeededCampaignError(
                "scenario schedule length mismatch"
            )

        for tx, name in zip(public["transactions"], schedule):
            scenario_totals[name]["transaction_count"] += 1

            for uid in tx["inputs"]:
                input_scenarios[uid] = name

        # Classifications come from the evaluator; the analyst
        # itself receives public blockchain records only.
        for decision in evaluation["decisions"]:
            left = decision["left"]
            right = decision["right"]

            name = input_scenarios[left]

            if input_scenarios[right] != name:
                raise SeededCampaignError(
                    "inconsistent scenario attribution"
                )

            counts = scenario_totals[name]["counts"]
            classification = decision["classification"]

            counts[classification] += 1
            counts["total"] += 1

            if classification != "abstained":
                counts["evaluated"] += 1

        seed_results.append({
            "seed": seed,
            "transaction_count": transaction_count,
            "prediction_count": len(analysis["predictions"]),
            "counts": seed_counts,
            "metrics": dict(evaluation["metrics"]),
        })

    scenario_reports = {}

    for name, entry in scenario_totals.items():
        counts = entry["counts"]
        scenario_reports[name] = {
            "transaction_count": entry["transaction_count"],
            "prediction_count": counts["total"],
            "counts": counts,
            "metrics": _metrics(counts),
        }

    # Prevent attribution mistakes from silently corrupting
    # the aggregate research report.
    for key in COUNT_KEYS:
        if sum(
            item["counts"][key]
            for item in scenario_reports.values()
        ) != aggregate[key]:
            raise SeededCampaignError(
                "scenario aggregation mismatch"
            )

    return {
        "kind": "mixer_seeded_research_campaign",
        "version": 1,
        "execution": "simulation_only",
        "analyst": "common_input_same_owner_hypothesis",
        "seeds": seed_results,
        "by_scenario": scenario_reports,
        "aggregate": {
            "seed_count": len(seeds),
            "transaction_count": len(seeds) * transaction_count,
            "prediction_count": aggregate["total"],
            "counts": aggregate,
            "metrics": _metrics(aggregate),
            "aggregation": "micro_average_over_submitted_pairs",
        },
    }
