"""Statistical summaries of synthetic Mixer Lab campaign reports.

Consumes completed simulation-only campaign reports.
Does not execute experiments, access wallets, or modify state.
"""

import math


class StatisticsError(ValueError):
    """Invalid research statistics input."""


COUNT_KEYS = (
    "tp", "fp", "tn", "fn",
    "abstained", "evaluated", "total",
)


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def _validate_counts(counts):
    if type(counts) is not dict:
        raise StatisticsError("invalid classification counts")

    if set(counts) != set(COUNT_KEYS):
        raise StatisticsError("invalid classification count keys")

    if any(
        type(counts[key]) is not int or counts[key] < 0
        for key in COUNT_KEYS
    ):
        raise StatisticsError("invalid classification count value")

    if counts["evaluated"] != sum(
        counts[key] for key in ("tp", "fp", "tn", "fn")
    ):
        raise StatisticsError("inconsistent evaluated count")

    if counts["total"] != (
        counts["evaluated"] + counts["abstained"]
    ):
        raise StatisticsError("inconsistent total count")


def summarize_seed_statistics(report):
    """Summarize precision variation across campaign seeds.

    Mean and population standard deviation describe variation
    among seeds with defined precision.

    Micro precision pools classified predictions, rather than
    assigning equal weight to each seed.

    Undefined precision is excluded from seed-level statistics.
    """

    if type(report) is not dict:
        raise StatisticsError("invalid campaign report")

    if (
        report.get("kind") != "mixer_seeded_research_campaign"
        or report.get("version") != 1
        or report.get("execution") != "simulation_only"
    ):
        raise StatisticsError("invalid campaign identity")

    seeds = report.get("seeds")
    aggregate = report.get("aggregate")

    if (
        type(seeds) is not list
        or not seeds
        or len(seeds) > 8
        or type(aggregate) is not dict
    ):
        raise StatisticsError("invalid campaign structure")

    aggregate_counts = aggregate.get("counts")
    _validate_counts(aggregate_counts)

    if (
        type(aggregate.get("prediction_count")) is not int
        or aggregate["prediction_count"] != aggregate_counts["total"]
    ):
        raise StatisticsError("invalid aggregate prediction count")

    pooled = {key: 0 for key in COUNT_KEYS}
    values = []
    undefined = 0
    seen_seeds = set()

    for item in seeds:
        if type(item) is not dict:
            raise StatisticsError("invalid seed report")

        seed = item.get("seed")

        if (
            type(seed) is not int
            or not 0 <= seed <= 2**64 - 1
            or seed in seen_seeds
        ):
            raise StatisticsError("invalid or duplicate seed")

        seen_seeds.add(seed)

        counts = item.get("counts")
        _validate_counts(counts)

        if (
            type(item.get("prediction_count")) is not int
            or item["prediction_count"] != counts["total"]
        ):
            raise StatisticsError("invalid seed prediction count")

        metrics = item.get("metrics")

        if type(metrics) is not dict:
            raise StatisticsError("invalid seed metrics")

        actual = _ratio(
            counts["tp"],
            counts["tp"] + counts["fp"],
        )

        reported = metrics.get("precision")

        if actual is None:
            if reported is not None:
                raise StatisticsError(
                    "undefined seed precision must be null"
                )
            undefined += 1
        else:
            if (
                type(reported) not in (float, int)
                or not math.isfinite(reported)
                or not math.isclose(
                    reported,
                    actual,
                    rel_tol=1e-12,
                    abs_tol=1e-12,
                )
            ):
                raise StatisticsError(
                    "seed precision does not match counts"
                )

            values.append(actual)

        for key in COUNT_KEYS:
            pooled[key] += counts[key]

    if pooled != aggregate_counts:
        raise StatisticsError("aggregate classification mismatch")

    micro = _ratio(
        pooled["tp"],
        pooled["tp"] + pooled["fp"],
    )

    reported_micro = aggregate.get("metrics", {}).get(
        "precision"
    )

    if micro is None:
        if reported_micro is not None:
            raise StatisticsError(
                "undefined aggregate precision must be null"
            )
    elif (
        type(reported_micro) not in (float, int)
        or not math.isfinite(reported_micro)
        or not math.isclose(
            micro,
            reported_micro,
            rel_tol=1e-12,
            abs_tol=1e-12,
        )
    ):
        raise StatisticsError(
            "aggregate precision does not match counts"
        )

    if values:
        mean = sum(values) / len(values)

        variance = sum(
            (value - mean) ** 2 for value in values
        ) / len(values)

        stddev = math.sqrt(variance)
        minimum = min(values)
        maximum = max(values)
    else:
        mean = None
        stddev = None
        minimum = None
        maximum = None

    return {
        "kind": "mixer_seed_statistics",
        "version": 1,
        "execution": "simulation_only",
        "seed_count": len(seeds),
        "total_predictions": pooled["total"],
        "counts": dict(pooled),
        "precision": {
            "micro": micro,
            "mean": mean,
            "population_stddev": stddev,
            "minimum": minimum,
            "maximum": maximum,
            "defined_seeds": len(values),
            "undefined_seeds": undefined,
        },
    }


def compare_seeded_campaigns(reports):
    """Compare synthetic campaigns using aligned seeds.

    Precision deltas are computed seed by seed.

    Matching prediction counts are a necessary consistency
    check, not proof of identical external input profiles.

    No confidence intervals or population-level claims
    are inferred from these descriptive statistics.
    """

    if (
        type(reports) is not dict
        or not 2 <= len(reports) <= 3
    ):
        raise StatisticsError("invalid campaign collection")

    names = list(reports)

    if any(
        type(name) is not str
        or not name
        or not name.isidentifier()
        for name in names
    ):
        raise StatisticsError("invalid campaign label")

    summaries = {}
    seed_ids = None
    expected_predictions = None
    reference_name = names[0]

    for name, report in reports.items():
        summary = summarize_seed_statistics(report)

        ids = [item["seed"] for item in report["seeds"]]

        prediction_counts = [
            item["prediction_count"]
            for item in report["seeds"]
        ]

        if seed_ids is None:
            seed_ids = ids
            expected_predictions = prediction_counts
        else:
            if ids != seed_ids:
                raise StatisticsError(
                    "campaign seeds are not aligned"
                )

            if prediction_counts != expected_predictions:
                raise StatisticsError(
                    "campaign prediction counts differ"
                )

        if any(
            item["metrics"]["precision"] is None
            for item in report["seeds"]
        ):
            raise StatisticsError(
                "paired precision undefined for a seed"
            )

        summaries[name] = {
            "seed_count": summary["seed_count"],
            "total_predictions": summary["total_predictions"],
            "precision": summary["precision"],
        }

    reference = reports[reference_name]["seeds"]
    comparisons = {}

    for name in names[1:]:
        candidate = reports[name]["seeds"]

        deltas = [
            left["metrics"]["precision"]
            - right["metrics"]["precision"]
            for left, right in zip(reference, candidate)
        ]

        mean = sum(deltas) / len(deltas)

        variance = sum(
            (value - mean) ** 2 for value in deltas
        ) / len(deltas)

        comparisons[f"{reference_name}_vs_{name}"] = {
            "reference": reference_name,
            "candidate": name,
            "direction": "reference_minus_candidate",
            "deltas": deltas,
            "mean_delta": mean,
            "population_stddev": math.sqrt(variance),
            "minimum_delta": min(deltas),
            "maximum_delta": max(deltas),
        }

    return {
        "kind": "mixer_paired_campaign_statistics",
        "version": 1,
        "execution": "simulation_only",
        "seed_count": len(seed_ids),
        "seeds": list(seed_ids),
        "predictions_per_campaign": sum(
            expected_predictions
        ),
        "per_seed_prediction_counts_equal": True,
        "matched_profile_proof": False,
        "campaigns": summaries,
        "comparisons": comparisons,
    }
