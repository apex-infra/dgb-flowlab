"""Deterministic synthetic scenario distribution scheduler.

Produces a seeded schedule of research scenario categories.
Contains no ownership ground truth or blockchain operations.
"""

import random

from .mixer_workloads import SCENARIOS


class DistributionError(ValueError):
    """Invalid synthetic scenario distribution."""


DEFAULT_WEIGHTS = {
    "single_owner": 40,
    "collaborative": 30,
    "mixed_ownership": 30,
}


def build_distribution(
    seed=2049,
    transaction_count=30,
    weights=None,
):
    """Allocate and shuffle synthetic scenario categories.

    Weights are integer percentages totaling exactly 100.
    Allocation uses the largest-remainder method with
    canonical scenario order to break equal remainders.

    The seed determines the scenario permutation.
    """

    if type(seed) is not int or not 0 <= seed <= 2**64 - 1:
        raise DistributionError("invalid seed")

    if (
        type(transaction_count) is not int
        or not 1 <= transaction_count <= 1000
    ):
        raise DistributionError("invalid transaction count")

    if weights is None:
        weights = DEFAULT_WEIGHTS

    if (
        type(weights) is not dict
        or set(weights) != set(SCENARIOS)
    ):
        raise DistributionError("invalid scenario weights")

    if any(
        type(weights[name]) is not int
        or not 0 <= weights[name] <= 100
        for name in SCENARIOS
    ):
        raise DistributionError("invalid scenario weight value")

    if sum(weights.values()) != 100:
        raise DistributionError("scenario weights must total 100")

    composition = {}
    remainders = []

    for index, name in enumerate(SCENARIOS):
        numerator = transaction_count * weights[name]
        composition[name] = numerator // 100
        remainders.append((numerator % 100, index, name))

    remaining = transaction_count - sum(composition.values())

    # Largest fractional remainder receives the next slot.
    # Ties are resolved by canonical scenario order.
    order = sorted(
        remainders,
        key=lambda item: (-item[0], item[1]),
    )

    for _, _, name in order[:remaining]:
        composition[name] += 1

    schedule = []

    for name in SCENARIOS:
        schedule.extend([name] * composition[name])

    rng = random.Random(seed)
    rng.shuffle(schedule)

    if len(schedule) != transaction_count:
        raise DistributionError("distribution accounting mismatch")

    return {
        "kind": "mixer_scenario_distribution",
        "version": 1,
        "execution": "simulation_only",
        "seed": seed,
        "transaction_count": transaction_count,
        "weights": {
            name: weights[name] for name in SCENARIOS
        },
        "scenario_composition": composition,
        "schedule": schedule,
    }
