"""Deterministic, simulation-only Mixer Lab research contracts.

This module does not interact with DigiByte Core, databases, wallet
interfaces, network services, or the live transaction executor.
"""

import copy
import hashlib
import json


class ResearchContractError(ValueError):
    """Invalid research configuration."""


RESEARCH_VERSION = 1

RESEARCH_MODES = frozenset({
    "baseline",
    "topology",
    "utxo",
    "analyst",
})

OBSERVER_MODELS = frozenset({
    "public_chain",
    "simulated_network",
    "auxiliary_information",
})

MAX_PARTICIPANTS = 1000
MAX_UTXOS_PER_PARTICIPANT = 1000
MAX_SYNTHETIC_UTXOS = 100_000
MAX_TRANSACTIONS = 100_000
MAX_TOTAL_SATS = 21_000_000 * 100_000_000
MAX_FEE_SATS = 100_000_000


def _require(condition, message):
    if not condition:
        raise ResearchContractError(message)


def _exact_fields(value, fields, name):
    _require(type(value) is dict, f"{name} must be an object")
    _require(
        set(value) == set(fields),
        f"{name} requires exactly: {', '.join(sorted(fields))}",
    )


def _integer(value, *, minimum, maximum, name):
    _require(
        type(value) is int and minimum <= value <= maximum,
        f"{name} must be an integer in [{minimum}, {maximum}]",
    )


def validate_research(config):
    """Validate and return a normalized, independent configuration."""

    _exact_fields(
        config,
        {
            "version",
            "mode",
            "seed",
            "hypothesis",
            "synthetic",
            "analysis",
        },
        "research config",
    )

    _require(
        type(config["version"]) is int
        and config["version"] == RESEARCH_VERSION,
        "unsupported research contract version",
    )

    _require(
        type(config["mode"]) is str
        and config["mode"] in RESEARCH_MODES,
        "unsupported research mode",
    )

    _integer(
        config["seed"],
        minimum=0,
        maximum=(2**64) - 1,
        name="seed",
    )

    hypothesis = config["hypothesis"]

    _require(
        type(hypothesis) is str
        and 1 <= len(hypothesis) <= 500
        and bool(hypothesis.strip()),
        "hypothesis must be a non-empty string of at most 500 characters",
    )

    synthetic = config["synthetic"]

    _exact_fields(
        synthetic,
        {
            "participant_count",
            "utxos_per_participant",
            "initial_value_sats",
            "transaction_count",
            "network_fee_sats",
        },
        "synthetic",
    )

    _integer(
        synthetic["participant_count"],
        minimum=1,
        maximum=MAX_PARTICIPANTS,
        name="participant_count",
    )

    _integer(
        synthetic["utxos_per_participant"],
        minimum=1,
        maximum=MAX_UTXOS_PER_PARTICIPANT,
        name="utxos_per_participant",
    )

    _integer(
        synthetic["initial_value_sats"],
        minimum=1,
        maximum=MAX_TOTAL_SATS,
        name="initial_value_sats",
    )

    _integer(
        synthetic["transaction_count"],
        minimum=1,
        maximum=MAX_TRANSACTIONS,
        name="transaction_count",
    )

    _integer(
        synthetic["network_fee_sats"],
        minimum=0,
        maximum=MAX_FEE_SATS,
        name="network_fee_sats",
    )

    total_utxos = (
        synthetic["participant_count"]
        * synthetic["utxos_per_participant"]
    )

    _require(
        total_utxos <= MAX_SYNTHETIC_UTXOS,
        "synthetic UTXO count exceeds research limit",
    )

    total_value = total_utxos * synthetic["initial_value_sats"]

    _require(
        total_value <= MAX_TOTAL_SATS,
        "synthetic initial value exceeds research limit",
    )

    total_network_fees = (
        synthetic["transaction_count"]
        * synthetic["network_fee_sats"]
    )

    _require(
        total_network_fees <= total_value,
        "synthetic network fee budget exceeds initial funding",
    )

    analysis = config["analysis"]

    _exact_fields(
        analysis,
        {"observer"},
        "analysis",
    )

    _require(
        type(analysis["observer"]) is str
        and analysis["observer"] in OBSERVER_MODELS,
        "unsupported observer model",
    )

    return copy.deepcopy(config)


def research_identity(config):
    """Return stable SHA-256 identity of the validated research contract."""

    normalized = validate_research(config)

    encoded = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")

    digest = hashlib.sha256(encoded).hexdigest()

    return f"mixer_research:v1:{digest}"


def compile_research(config):
    """Compile research metadata, never executable transaction jobs."""

    normalized = validate_research(config)

    return {
        "kind": "mixer_research",
        "version": RESEARCH_VERSION,
        "execution": "simulation_only",
        "identity": research_identity(normalized),
        "config": normalized,
    }
