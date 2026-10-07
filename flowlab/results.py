"""Pure, read-only experiment result analysis.

This module consumes the safe dashboard snapshot projection only.
It performs no RPC, database writes, wallet actions, or transaction actions.
"""

from collections import Counter
from datetime import datetime
import math
import statistics


def _jobs(snapshot):
    return [
        job
        for flow in snapshot.get("flows", [])
        for job in flow.get("jobs", [])
    ]


def _workload_jobs(snapshot):
    return [
        job
        for job in _jobs(snapshot)
        if job.get("generated")
        and isinstance(job["generated"].get("decision_index"), int)
    ]


def _parse_time(value):
    if not value:
        return None
    return datetime.fromisoformat(value)


def _stats(values):
    values = list(values)

    if not values:
        return {
            "count": 0,
            "min": None,
            "max": None,
            "mean": None,
            "median": None,
            "stdev": None,
        }

    return {
        "count": len(values),
        "min": min(values),
        "max": max(values),
        "mean": sum(values) / len(values),
        "median": statistics.median(values),
        "stdev": statistics.pstdev(values),
    }


def _entropy(counts):
    total = sum(counts.values())
    if total <= 0:
        return 0.0

    result = 0.0

    for count in counts.values():
        if count <= 0:
            continue

        p = count / total
        result -= p * math.log2(p)

    return result


def _normalized_entropy(counts):
    positive = sum(1 for count in counts.values() if count > 0)

    if positive <= 1:
        return 0.0

    return _entropy(counts) / math.log2(positive)


def _bounded_variation(values):
    """Map coefficient of variation onto 0..1.

    0 means no observed variation. Increasing dispersion approaches 1.
    """
    values = list(values)

    if len(values) < 2:
        return 0.0

    mean = sum(values) / len(values)

    if mean == 0:
        return 0.0

    cv = statistics.pstdev(values) / abs(mean)
    return cv / (1.0 + cv)


def _inverse_concentration(counts):
    """Return 0..1 diversity derived from normalized HHI concentration."""
    values = [count for count in counts.values() if count > 0]

    if len(values) <= 1:
        return 0.0

    total = sum(values)
    shares = [count / total for count in values]
    hhi = sum(share * share for share in shares)

    minimum = 1.0 / len(values)
    normalized_concentration = (
        (hhi - minimum) / (1.0 - minimum)
    )

    return max(
        0.0,
        min(1.0, 1.0 - normalized_concentration),
    )


def _mixing_label(score):
    if score >= 85:
        return "VERY HIGH"
    if score >= 70:
        return "HIGH"
    if score >= 50:
        return "MODERATE"
    if score >= 25:
        return "LIMITED"
    return "LOW"


def analyze_experiment(snapshot, wallet_roles=None):
    """Return deterministic measurements derived from one safe snapshot."""

    if not snapshot:
        raise ValueError("snapshot is required")

    wallet_roles = wallet_roles or {}

    jobs = _jobs(snapshot)
    workload = _workload_jobs(snapshot)

    states = Counter(job.get("state") for job in jobs)

    phase_counts = Counter()

    for job in jobs:
        generated = job.get("generated") or {}
        source = generated.get("source")
        phase = generated.get("phase")

        if source == "experimental allocation commit":
            phase_counts["allocation"] += 1
        elif isinstance(generated.get("decision_index"), int):
            phase_counts["workload"] += 1
        elif phase == "consolidation":
            phase_counts["consolidation"] += 1
        elif phase == "terminal_distribution":
            phase_counts["terminal_distribution"] += 1
        elif phase == "finalization":
            phase_counts["finalization"] += 1
        else:
            phase_counts["other"] += 1

    edges = Counter(
        (job.get("from"), job.get("to"))
        for job in workload
        if job.get("from") and job.get("to")
    )

    wallet_activity = Counter()

    for job in workload:
        src = job.get("from")
        dst = job.get("to")

        if src:
            wallet_activity[src] += 1
        if dst:
            wallet_activity[dst] += 1

    route_executions = sum(edges.values())
    unique_edges = len(edges)

    repeated_edge_executions = sum(
        max(0, count - 1)
        for count in edges.values()
    )

    repeated_edge_ratio = (
        repeated_edge_executions / route_executions
        if route_executions
        else 0.0
    )

    self_transfers = sum(
        count
        for (src, dst), count in edges.items()
        if src == dst
    )

    self_transfer_ratio = (
        self_transfers / route_executions
        if route_executions
        else 0.0
    )

    amounts = [
        job["amount"]
        for job in workload
        if isinstance(job.get("amount"), int)
        and not isinstance(job.get("amount"), bool)
    ]

    planned_delays = [
        job["delay_s"]
        for job in workload
        if isinstance(job.get("delay_s"), int)
        and not isinstance(job.get("delay_s"), bool)
    ]

    executed = sorted(
        t
        for t in (
            _parse_time(job.get("actual_executed_at"))
            for job in workload
        )
        if t is not None
    )

    execution_gaps = [
        (b - a).total_seconds()
        for a, b in zip(executed, executed[1:])
    ]

    confirmation_durations = []

    for job in jobs:
        start = _parse_time(job.get("actual_executed_at"))
        confirmed = _parse_time(job.get("confirmed_at"))

        if start is not None and confirmed is not None:
            confirmation_durations.append(
                (confirmed - start).total_seconds()
            )

    exp = snapshot.get("exp") or {}

    started = _parse_time(exp.get("started_at"))
    completed = _parse_time(exp.get("completed_at"))

    runtime_s = (
        (completed - started).total_seconds()
        if started is not None and completed is not None
        else None
    )

    accounting = snapshot.get("staged_accounting")

    route_diversity = _normalized_entropy(edges)
    wallet_diversity = _normalized_entropy(wallet_activity)
    edge_distribution_diversity = _inverse_concentration(edges)
    amount_diversity = _bounded_variation(amounts)
    timing_diversity = _bounded_variation(execution_gaps)

    mixing_components = {
        "route_diversity": route_diversity,
        "wallet_activity_diversity": wallet_diversity,
        "edge_distribution_diversity":
            edge_distribution_diversity,
        "amount_diversity": amount_diversity,
        "timing_diversity": timing_diversity,
    }

    mixing_score = round(
        100 * (
            0.30 * route_diversity
            + 0.20 * wallet_diversity
            + 0.20 * edge_distribution_diversity
            + 0.15 * amount_diversity
            + 0.15 * timing_diversity
        )
    )

    return {
        "summary": {
            "id": exp.get("id"),
            "description": exp.get("description"),
            "state": exp.get("state"),
            "mode": snapshot.get("mode"),
            "started_at": exp.get("started_at"),
            "completed_at": exp.get("completed_at"),
            "runtime_s": runtime_s,
        },
        "accounting": accounting,
        "activity": {
            "total_jobs": len(jobs),
            "confirmed": states["CONFIRMED"],
            "broadcast": states["BROADCAST"],
            "planned": states["PLANNED"],
            "failed": states["FAILED"],
            "cancelled": states["CANCELLED"],
            "allocation_jobs": phase_counts["allocation"],
            "workload_jobs": phase_counts["workload"],
            "consolidation_jobs": phase_counts["consolidation"],
            "finalization_jobs": phase_counts["finalization"],
            "terminal_distribution_jobs":
                phase_counts["terminal_distribution"],
            "other_jobs": phase_counts["other"],
        },
        "topology": {
            "route_executions": route_executions,
            "unique_edges": unique_edges,
            "edges": [
                {
                    "from": src,
                    "to": dst,
                    "count": count,
                }
                for (src, dst), count in sorted(edges.items())
            ],
            "repeated_edge_executions": repeated_edge_executions,
            "repeated_edge_ratio": repeated_edge_ratio,
            "self_transfers": self_transfers,
            "self_transfer_ratio": self_transfer_ratio,
            "wallets_exercised": len(wallet_activity),
            "wallet_activity": dict(sorted(wallet_activity.items())),
            "route_entropy_bits": _entropy(edges),
            "route_entropy_normalized": _normalized_entropy(edges),
            "wallet_entropy_bits": _entropy(wallet_activity),
            "wallet_entropy_normalized":
                _normalized_entropy(wallet_activity),
        },
        "amounts": {
            "total_sats": sum(amounts),
            **_stats(amounts),
        },
        "timing": {
            "planned_delay_s": _stats(planned_delays),
            "execution_gap_s": _stats(execution_gaps),
            "confirmation_s": _stats(confirmation_durations),
        },
        "observability": {
            "route_diversity": route_diversity,
            "wallet_activity_diversity": wallet_diversity,
            "edge_distribution_diversity":
                edge_distribution_diversity,
            "amount_diversity": amount_diversity,
            "timing_diversity": timing_diversity,
            "repeated_edge_ratio": repeated_edge_ratio,
            "self_transfer_ratio": self_transfer_ratio,
        },
        "mixing": {
            "model": "mixing_model_v1",
            "score": mixing_score,
            "label": _mixing_label(mixing_score),
            "components": mixing_components,
            "weights": {
                "route_diversity": 0.30,
                "wallet_activity_diversity": 0.20,
                "edge_distribution_diversity": 0.20,
                "amount_diversity": 0.15,
                "timing_diversity": 0.15,
            },
            "interpretation": (
                "Descriptive FlowLab graph-diversity measurement; "
                "it does not imply that transaction provenance "
                "has been removed."
            ),
        },
        "wallet_roles": {
            role: list(names)
            for role, names in wallet_roles.items()
        },
    }
