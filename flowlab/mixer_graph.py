"""Bounded public-only synthetic UTXO graph adapter.

Transforms validated public synthetic transactions into stable
UTXO and transaction nodes with directional spend/create edges.

No ownership inference is performed here. No wallet access,
networking, RPC, database writes, or transaction broadcasts.
"""

from .mixer_integrity import (
    IntegrityError,
    attest_public_workload,
)


class GraphError(ValueError):
    """Invalid public synthetic graph request."""


DEFAULT_TRANSACTION_LIMIT = 24
MAX_TRANSACTION_LIMIT = 24


def _attest_public(public):
    if type(public) is not dict:
        raise GraphError("invalid public graph")

    genesis = public.get("genesis_utxos")
    transactions = public.get("transactions")

    if type(genesis) is not list or type(transactions) is not list:
        raise GraphError("invalid public graph")

    proxy = {
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
        return attest_public_workload(proxy)
    except (IntegrityError, ValueError, TypeError) as exc:
        raise GraphError("invalid public graph") from exc


def build_public_graph(
    public,
    transaction_limit=DEFAULT_TRANSACTION_LIMIT,
):
    """Build a bounded view of a validated synthetic UTXO graph.

    The first N transactions are shown in their original
    deterministic sequence.

    Every displayed spend/create edge references a real
    transaction or UTXO in the supplied public dataset.

    The output intentionally excludes deterministic generator
    inputs and private synthetic ownership ground truth.
    """

    if (
        type(transaction_limit) is not int
        or not 1 <= transaction_limit <= MAX_TRANSACTION_LIMIT
    ):
        raise GraphError("invalid transaction visualization limit")

    attestation = _attest_public(public)

    transactions = public["transactions"]
    visible = transactions[:transaction_limit]

    # Map public UTXO identifiers to their public values.
    # Validation above checks the complete transaction graph.
    values = {}

    for utxo in public["genesis_utxos"]:
        values[utxo["id"]] = utxo["value_sats"]

    for tx in transactions:
        for output in tx["outputs"]:
            values[output["id"]] = output["value_sats"]

    nodes = []
    edges = []
    seen = set()

    def add_utxo(identifier):
        if identifier in seen:
            return

        if identifier not in values:
            raise GraphError("unresolved public UTXO reference")

        nodes.append({
            "id": identifier,
            "kind": "utxo",
            "label": (
                "GENESIS UTXO"
                if identifier.startswith("genesis:")
                else "OUTPUT UTXO"
            ),
            "value_sats": values[identifier],
            "origin": (
                "genesis"
                if identifier.startswith("genesis:")
                else "transaction"
            ),
        })

        seen.add(identifier)

    spend_count = 0
    create_count = 0

    for tx in visible:
        txid = tx["id"]

        for uid in tx["inputs"]:
            add_utxo(uid)

        if txid in seen:
            raise GraphError("duplicate transaction identifier")

        nodes.append({
            "id": txid,
            "kind": "transaction",
            "label": "TX " + str(tx["sequence"]).zfill(8),
            "sequence": tx["sequence"],
            "fee_sats": tx["fee_sats"],
            "input_count": len(tx["inputs"]),
            "output_count": len(tx["outputs"]),
        })

        seen.add(txid)

        for uid in tx["inputs"]:
            edges.append({
                "source": uid,
                "target": txid,
                "kind": "spend",
                "sequence": tx["sequence"],
            })
            spend_count += 1

        for output in tx["outputs"]:
            add_utxo(output["id"])

            edges.append({
                "source": txid,
                "target": output["id"],
                "kind": "create",
                "sequence": tx["sequence"],
            })
            create_count += 1

    return {
        "kind": "mixer_public_utxo_graph",
        "version": 1,
        "execution": "simulation_only",
        "nodes": nodes,
        "edges": edges,
        "summary": {
            "total_transactions": len(transactions),
            "visible_transactions": len(visible),
            "visible_utxos": sum(
                node["kind"] == "utxo"
                for node in nodes
            ),
            "visible_spend_edges": spend_count,
            "visible_create_edges": create_count,
            "truncated": len(visible) < len(transactions),
            "public_sha256": attestation["public_sha256"],
        },
    }
