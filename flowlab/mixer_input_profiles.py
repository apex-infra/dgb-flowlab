"""Deterministic transaction input-count profiles.

Research-only synthetic workload metadata.

Input counts are generated independently of scenario
selection and participant ownership assignment.

No DigiByte RPC, networking, wallet access, or persistence.
"""

import hashlib
import random


class InputProfileError(ValueError):
    """Invalid matched input profile configuration."""


def build_input_profile(
    seed=2049,
    transaction_count=30,
    max_inputs=6,
):
    """Generate a reproducible per-transaction input schedule.

    A domain-separated random stream avoids coupling
    transaction sizes to ownership-scenario scheduling.

    The same profile can be reused across campaigns with
    different scenario distributions.
    """

    if type(seed) is not int or not 0 <= seed <= 2**64 - 1:
        raise InputProfileError("invalid seed")

    if (
        type(transaction_count) is not int
        or not 1 <= transaction_count <= 1000
    ):
        raise InputProfileError("invalid transaction_count")

    if type(max_inputs) is not int or not 3 <= max_inputs <= 12:
        raise InputProfileError("invalid max_inputs")

    # Domain separation makes this stream independent
    # of workload ownership and scenario PRNG consumption.
    material = f"flowlab:input-profile:v1:{seed}".encode("utf-8")

    derived_seed = int.from_bytes(
        hashlib.sha256(material).digest(),
        "big",
    )

    rng = random.Random(derived_seed)

    counts = [
        rng.randint(3, max_inputs)
        for _ in range(transaction_count)
    ]

    histogram = {
        count: counts.count(count)
        for count in range(3, max_inputs + 1)
    }

    return {
        "kind": "mixer_matched_input_profile",
        "version": 1,
        "execution": "simulation_only",
        "seed": seed,
        "transaction_count": transaction_count,
        "max_inputs": max_inputs,
        "input_counts": counts,
        "histogram": histogram,
        "total_inputs": sum(counts),
    }
