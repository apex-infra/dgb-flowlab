"""Read-only public-data integrity checks for seeded research.

The attestation contains a fingerprint of public synthetic
transaction data, never private ownership ground truth.

No wallet access, RPC, networking, or persistence.
"""

import hashlib
import json
import re

from .mixer_analysis import analyze_ancestry


class IntegrityError(ValueError):
    """Invalid seeded public research dataset."""


GENESIS_PATTERN = re.compile(r"genesis:[0-9a-f]{32}\Z")
TXID_PATTERN = re.compile(r"simtx:[0-9]{8}\Z")


def _validate_public(public):
    if type(public) is not dict:
        raise IntegrityError("invalid public data")

    expected = {
        "genesis_utxos",
        "transactions",
        "unspent_utxos",
    }

    if set(public) != expected:
        raise IntegrityError("unexpected public data fields")

    genesis = public["genesis_utxos"]
    transactions = public["transactions"]

    if type(genesis) is not list:
        raise IntegrityError("invalid genesis UTXOs")

    if type(transactions) is not list:
        raise IntegrityError("invalid transactions")

    for item in genesis:
        if (
            type(item) is not dict
            or set(item) != {"id", "value_sats"}
            or type(item["id"]) is not str
            or GENESIS_PATTERN.fullmatch(item["id"]) is None
        ):
            raise IntegrityError("invalid opaque genesis identifier")

    for index, tx in enumerate(transactions):
        if (
            type(tx) is not dict
            or set(tx) != {
                "id", "sequence", "inputs",
                "outputs", "fee_sats",
            }
            or type(tx["id"]) is not str
            or TXID_PATTERN.fullmatch(tx["id"]) is None
            or tx["id"] != f"simtx:{index:08d}"
        ):
            raise IntegrityError("invalid synthetic transaction")

        if type(tx["outputs"]) is not list:
            raise IntegrityError("invalid transaction outputs")

        for output_index, output in enumerate(tx["outputs"]):
            if (
                type(output) is not dict
                or set(output) != {"id", "value_sats"}
                or output.get("id") != (
                    f"{tx['id']}:{output_index}"
                )
            ):
                raise IntegrityError("invalid output identifier")

    for item in public["unspent_utxos"]:
        if (
            type(item) is not dict
            or set(item) != {"id", "value_sats"}
        ):
            raise IntegrityError("invalid unspent UTXO")

    try:
        analyze_ancestry(public)
    except (ValueError, TypeError, KeyError) as exc:
        raise IntegrityError("invalid public ancestry graph") from exc


def attest_public_workload(dataset):
    """Validate and fingerprint a seeded public workload.

    This fingerprint detects differences when compared with
    an independently retained trusted fingerprint.

    It is not a signature or proof of source authenticity.
    """

    if (
        type(dataset) is not dict
        or dataset.get("kind") != "mixer_seeded_workload"
        or dataset.get("version") != 1
        or dataset.get("execution") != "simulation_only"
    ):
        raise IntegrityError("invalid workload identity")

    public = dataset.get("public")
    _validate_public(public)

    summary = dataset.get("summary")

    if type(summary) is not dict:
        raise IntegrityError("invalid workload summary")

    count = summary.get("transaction_count")

    if (
        type(count) is not int
        or count != len(public["transactions"])
    ):
        raise IntegrityError("transaction count mismatch")

    initial = summary.get("initial_utxo_count")

    if (
        type(initial) is not int
        or initial != len(public["genesis_utxos"])
    ):
        raise IntegrityError("genesis count mismatch")

    canonical = json.dumps(
        public,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")

    fingerprint = hashlib.sha256(canonical).hexdigest()

    return {
        "kind": "mixer_public_workload_attestation",
        "version": 1,
        "execution": "simulation_only",
        "hash_algorithm": "sha256",
        "canonicalization": "json_sorted_keys_compact_v1",
        "public_sha256": fingerprint,
        "transaction_count": count,
        "genesis_utxo_count": initial,
    }


def attest_reproducible_workload(dataset, configuration):
    """Reproduce an exact seeded public workload.

    This checks agreement with the current generator
    implementation. It does not authenticate a publisher.

    Private ownership ground truth is never returned.
    """
    from .mixer_workloads import (
        WorkloadError,
        build_seeded_workload,
    )

    expected_keys = {
        "seed",
        "transaction_count",
        "max_inputs",
        "scenario_weights",
        "matched_input_profile",
    }

    if (
        type(configuration) is not dict
        or set(configuration) != expected_keys
    ):
        raise IntegrityError("invalid experiment configuration")

    config = {
        key: configuration[key]
        for key in sorted(expected_keys)
    }

    try:
        expected = build_seeded_workload(**config)
    except (WorkloadError, TypeError, ValueError) as exc:
        raise IntegrityError("invalid experiment configuration") from exc

    supplied = attest_public_workload(dataset)
    reproduced = attest_public_workload(expected)

    if supplied["public_sha256"] != reproduced["public_sha256"]:
        raise IntegrityError("reproduction mismatch: public dataset")

    # Do not trust public equality alone: also ensure that
    # research metadata identifying the scenario schedule
    # agrees with the independently regenerated workload.
    if dataset.get("summary") != expected.get("summary"):
        raise IntegrityError("reproduction mismatch: research metadata")

    canonical = json.dumps(
        config,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")

    config_hash = hashlib.sha256(
        b"flowlab:experiment-config:v1:" + canonical
    ).hexdigest()

    return {
        "kind": "mixer_reproducible_workload_attestation",
        "version": 1,
        "execution": "simulation_only",
        "reproduced": True,
        "configuration_sha256": config_hash,
        "public_sha256": supplied["public_sha256"],
        "hash_algorithm": "sha256",
        "generator_contract": "mixer_seeded_workload_v1",
        "transaction_count": supplied["transaction_count"],
        "genesis_utxo_count": supplied["genesis_utxo_count"],
    }
