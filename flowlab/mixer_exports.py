"""Portable public synthetic research exports.

The public format intentionally omits seeds, generator settings,
scenario schedules, and ownership ground truth.

Deterministic synthetic seeds may reconstruct ground truth;
they must not be included in public research exports.

No wallet access, RPC, networking, or persistence.
"""

from copy import deepcopy

from .mixer_integrity import (
    IntegrityError,
    attest_public_workload,
)


class ExportError(ValueError):
    """Invalid public research export."""


EXPORT_KEYS = {
    "kind",
    "version",
    "execution",
    "public",
    "attestation",
}


def _attest(public):
    if type(public) is not dict:
        raise ExportError("invalid public graph")

    genesis = public.get("genesis_utxos")
    transactions = public.get("transactions")

    if type(genesis) is not list or type(transactions) is not list:
        raise ExportError("invalid public graph")

    dataset = {
        "kind": "mixer_seeded_workload",
        "version": 1,
        "execution": "simulation_only",
        "public": public,
        "summary": {
            "transaction_count": len(transactions),
            "initial_utxo_count": len(genesis),
        },
    }

    try:
        return attest_public_workload(dataset)
    except (IntegrityError, TypeError, ValueError) as exc:
        raise ExportError("invalid public graph") from exc


def build_public_export(dataset):
    """Create a public-only export of a seeded workload.

    A public-only attestation is retained. Generator configuration
    and synthetic ownership records are deliberately excluded.
    """

    if (
        type(dataset) is not dict
        or dataset.get("kind") != "mixer_seeded_workload"
        or dataset.get("version") != 1
        or dataset.get("execution") != "simulation_only"
    ):
        raise ExportError("invalid seeded workload")

    try:
        attestation = attest_public_workload(dataset)
    except (IntegrityError, TypeError, ValueError) as exc:
        raise ExportError("invalid seeded workload") from exc

    result = {
        "kind": "mixer_public_research_export",
        "version": 1,
        "execution": "simulation_only",
        "public": deepcopy(dataset["public"]),
        "attestation": deepcopy(attestation),
    }

    verify_public_export(result)

    return result


def verify_public_export(export):
    """Validate a public export and its embedded fingerprint.

    A matching fingerprint detects changes relative to the
    fingerprint embedded in this record, but does not authenticate
    the source. An attacker could replace both data and hash.

    Compare against an independently trusted hash when source
    integrity is important.
    """

    if type(export) is not dict or set(export) != EXPORT_KEYS:
        raise ExportError("invalid export structure")

    if (
        export["kind"] != "mixer_public_research_export"
        or export["version"] != 1
        or export["execution"] != "simulation_only"
    ):
        raise ExportError("invalid export identity")

    expected = _attest(export["public"])

    attestation = export["attestation"]

    if (
        type(attestation) is not dict
        or attestation != expected
    ):
        raise ExportError("public attestation mismatch")

    return {
        "valid": True,
        "execution": "simulation_only",
        "public_sha256": expected["public_sha256"],
        "transaction_count": expected["transaction_count"],
        "genesis_utxo_count": expected["genesis_utxo_count"],
    }


def build_restricted_export(dataset, configuration):
    """Build a local, restricted synthetic evaluation record.

    This record contains ownership ground truth and generator
    configuration. It must never be served by a public endpoint.
    """
    from .mixer_integrity import (
        attest_reproducible_workload,
    )

    try:
        attestation = attest_reproducible_workload(
            dataset, configuration
        )
        public_export = build_public_export(dataset)
    except (IntegrityError, ExportError, TypeError, ValueError) as exc:
        raise ExportError("invalid restricted research input") from exc

    record = {
        "kind": "mixer_restricted_research_export",
        "version": 1,
        "execution": "simulation_only",
        "access": "restricted",
        "configuration": deepcopy(configuration),
        "public_export": public_export,
        "ground_truth": deepcopy(dataset["ground_truth"]),
        "reproducibility": attestation,
    }

    verify_restricted_export(record)
    return record


def verify_restricted_export(record):
    """Regenerate and verify the complete restricted record.

    Ground truth is compared against independently regenerated
    synthetic ownership records, not merely against a self-hash.
    """
    from .mixer_integrity import (
        attest_reproducible_workload,
    )
    from .mixer_workloads import build_seeded_workload

    expected_keys = {
        "kind",
        "version",
        "execution",
        "access",
        "configuration",
        "public_export",
        "ground_truth",
        "reproducibility",
    }

    if type(record) is not dict or set(record) != expected_keys:
        raise ExportError("invalid restricted export structure")

    if (
        record["kind"] != "mixer_restricted_research_export"
        or type(record["version"]) is not int
        or record["version"] != 1
        or record["execution"] != "simulation_only"
        or record["access"] != "restricted"
    ):
        raise ExportError("invalid restricted export identity")

    configuration = record["configuration"]

    required_config = {
        "seed",
        "transaction_count",
        "max_inputs",
        "scenario_weights",
        "matched_input_profile",
    }

    if (
        type(configuration) is not dict
        or set(configuration) != required_config
    ):
        raise ExportError("invalid restricted configuration")

    try:
        expected = build_seeded_workload(**configuration)

        expected_attestation = attest_reproducible_workload(
            expected, configuration
        )

        expected_public = build_public_export(expected)

        if record["reproducibility"] != expected_attestation:
            raise ExportError("reproducibility mismatch")

        if record["public_export"] != expected_public:
            raise ExportError("public export mismatch")

        if record["ground_truth"] != expected["ground_truth"]:
            raise ExportError("ground truth mismatch")

        verify_public_export(record["public_export"])

    except (IntegrityError, TypeError, ValueError) as exc:
        raise ExportError("restricted verification failed") from exc

    return {
        "valid": True,
        "reproduced": True,
        "execution": "simulation_only",
        "access": "restricted",
        "public_sha256": expected_attestation["public_sha256"],
        "configuration_sha256": (
            expected_attestation["configuration_sha256"]
        ),
        "transaction_count": (
            expected_attestation["transaction_count"]
        ),
    }
