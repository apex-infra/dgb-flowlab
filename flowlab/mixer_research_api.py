"""Simulation-only research request adapter for FlowLab.

Returns allowlisted research summaries suitable for a dashboard.

No wallet access, DigiByte RPC, database writes,
participant sessions, or transaction broadcasts.
"""

from .mixer_distribution import (
    DistributionError,
    build_distribution,
)
from .mixer_seeded_campaign import (
    SeededCampaignError,
    run_seeded_campaign,
)
from .mixer_statistics import (
    StatisticsError,
    summarize_seed_statistics,
)


from .mixer_graph import build_public_graph
from .mixer_workloads import build_seeded_workload


class ResearchAPIError(ValueError):
    """Invalid synthetic research API request."""


REQUIRED_KEYS = {
    "seeds",
    "transaction_count",
    "max_inputs",
    "scenario_weights",
    "matched_input_profile",
}


def _validate_request(request):
    if (
        type(request) is not dict
        or set(request) != REQUIRED_KEYS
    ):
        raise ResearchAPIError("invalid research request fields")

    seeds = request["seeds"]
    count = request["transaction_count"]
    maximum = request["max_inputs"]
    weights = request["scenario_weights"]
    matched = request["matched_input_profile"]

    if type(seeds) is not list or not 1 <= len(seeds) <= 8:
        raise ResearchAPIError("invalid research seeds")

    if (
        any(
            type(seed) is not int
            or not 0 <= seed <= 2**64 - 1
            for seed in seeds
        )
        or len(set(seeds)) != len(seeds)
    ):
        raise ResearchAPIError("invalid research seeds")

    if type(count) is not int or not 1 <= count <= 1000:
        raise ResearchAPIError("invalid transaction count")

    if type(maximum) is not int or not 3 <= maximum <= 12:
        raise ResearchAPIError("invalid maximum inputs")

    if type(matched) is not bool:
        raise ResearchAPIError("invalid matched profile flag")

    try:
        build_distribution(
            seed=seeds[0],
            transaction_count=count,
            weights=weights,
        )
    except DistributionError as exc:
        raise ResearchAPIError(
            "invalid research scenario distribution"
        ) from exc

    # Check the bounded campaign budget before generating data.
    upper_bound = (
        len(seeds)
        * count
        * (maximum * (maximum - 1) // 2)
    )

    if upper_bound > 100_000:
        raise ResearchAPIError(
            "research prediction budget exceeded"
        )


def run_research_request(request):
    """Execute a bounded synthetic campaign.

    Input seeds are used internally and deliberately omitted
    from the response: the deterministic generator can
    reconstruct synthetic ownership labels from those seeds.

    Results describe controlled simulations, not observed
    DigiByte ownership or population-level performance.
    """

    _validate_request(request)

    try:
        report = run_seeded_campaign(
            seeds=list(request["seeds"]),
            transaction_count=request["transaction_count"],
            max_inputs=request["max_inputs"],
            scenario_weights=dict(request["scenario_weights"]),
            matched_input_profile=request["matched_input_profile"],
        )

        statistics = summarize_seed_statistics(report)

    except (
        SeededCampaignError,
        StatisticsError,
        DistributionError,
    ) as exc:
        raise ResearchAPIError(
            "synthetic research execution failed"
        ) from exc

    # Visualization is a bounded view of the first campaign seed.
    # No private ground truth or generator configuration is returned.
    graph_dataset = build_seeded_workload(
        seed=request["seeds"][0],
        transaction_count=request["transaction_count"],
        max_inputs=request["max_inputs"],
        scenario_weights=dict(request["scenario_weights"]),
        matched_input_profile=request["matched_input_profile"],
    )

    graph = build_public_graph(
        graph_dataset["public"],
    )

    graph["summary"]["campaign_seed_position"] = 0

    aggregate = report["aggregate"]

    # Explicit allowlists prevent private campaign metadata,
    # experiment seeds, or future restricted fields from
    # reaching the dashboard response accidentally.
    safe_aggregate = {
        "seed_count": aggregate["seed_count"],
        "transaction_count": aggregate["transaction_count"],
        "prediction_count": aggregate["prediction_count"],
        "counts": dict(aggregate["counts"]),
        "metrics": dict(aggregate["metrics"]),
        "aggregation": aggregate["aggregation"],
    }

    safe_scenarios = {}

    for name, item in report["by_scenario"].items():
        safe_scenarios[name] = {
            "transaction_count": item["transaction_count"],
            "prediction_count": item["prediction_count"],
            "counts": dict(item["counts"]),
            "metrics": dict(item["metrics"]),
        }

    return {
        "kind": "mixer_dashboard_research_result",
        "version": 1,
        "execution": "simulation_only",
        "analyst": "common_input_same_owner_hypothesis",
        "aggregate": safe_aggregate,
        "by_scenario": safe_scenarios,
        "graph": graph,
        "statistics": {
            "seed_count": statistics["seed_count"],
            "total_predictions": statistics["total_predictions"],
            "precision": dict(statistics["precision"]),
        },
    }
