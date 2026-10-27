"""Public-only synthetic transaction ancestry analysis.

This module does not accept owner labels, access ground truth, invoke
wallet RPC, use networking, or write persistent state.
"""


class AnalysisError(ValueError):
    """Invalid public transaction dataset."""


def _need(condition, message):
    if not condition:
        raise AnalysisError(message)


def _record(value, keys, label):
    _need(type(value) is dict, f"{label} must be an object")
    _need(
        set(value) == set(keys),
        f"{label} has invalid fields",
    )


def _identifier(value, label):
    _need(
        type(value) is str and bool(value),
        f"{label} must be a nonempty string",
    )


def _amount(value, label, *, positive=False):
    _need(
        type(value) is int
        and value >= (1 if positive else 0),
        f"{label} must be a valid integer amount",
    )


def analyze_ancestry(public):
    """Validate public UTXOs and reconstruct transaction ancestry.

    The input must contain only the three public dataset fields.
    Transaction records must be in creation order.
    """

    _record(
        public,
        {"genesis_utxos", "transactions", "unspent_utxos"},
        "public dataset",
    )

    for key in public:
        _need(type(public[key]) is list, f"{key} must be a list")

    values = {}
    producers = {}
    spent = set()
    results = {}
    total_fees = 0

    for item in public["genesis_utxos"]:
        _record(item, {"id", "value_sats"}, "genesis UTXO")
        uid = item["id"]
        _identifier(uid, "genesis UTXO ID")
        _amount(item["value_sats"], "genesis value", positive=True)
        _need(uid not in values, "duplicate UTXO identifier")

        values[uid] = item["value_sats"]
        producers[uid] = None

    initial_value = sum(values.values())

    for index, tx in enumerate(public["transactions"]):
        _record(
            tx,
            {"id", "sequence", "inputs", "outputs", "fee_sats"},
            "transaction",
        )

        txid = tx["id"]
        _identifier(txid, "transaction ID")
        _need(txid not in results, "duplicate transaction ID")
        _need(
            type(tx["sequence"]) is int
            and tx["sequence"] == index,
            "transaction sequence mismatch",
        )

        inputs = tx["inputs"]
        outputs = tx["outputs"]
        _need(
            type(inputs) is list and bool(inputs),
            "transaction requires inputs",
        )
        _need(
            type(outputs) is list and bool(outputs),
            "transaction requires outputs",
        )
        _amount(tx["fee_sats"], "transaction fee")

        parents = set()
        input_total = 0
        local_inputs = set()

        for uid in inputs:
            _identifier(uid, "input UTXO ID")
            _need(uid in values, "unknown or future input UTXO")
            _need(uid not in spent, "double-spent UTXO")
            _need(uid not in local_inputs, "duplicate transaction input")
            local_inputs.add(uid)
            input_total += values[uid]

            producer = producers[uid]
            if producer is not None:
                parents.add(producer)

        output_total = 0
        local_outputs = {}

        for item in outputs:
            _record(item, {"id", "value_sats"}, "output UTXO")
            uid = item["id"]
            _identifier(uid, "output UTXO ID")
            _amount(item["value_sats"], "output value", positive=True)
            _need(
                uid not in values and uid not in local_outputs,
                "duplicate output identifier",
            )
            local_outputs[uid] = item["value_sats"]
            output_total += item["value_sats"]

        _need(
            input_total == output_total + tx["fee_sats"],
            "transaction does not conserve value",
        )

        ancestors = set(parents)
        for parent in parents:
            ancestors.update(results[parent]["ancestors"])

        results[txid] = {
            "parents": sorted(parents),
            "ancestors": sorted(ancestors),
        }

        spent.update(local_inputs)

        for uid, value in local_outputs.items():
            values[uid] = value
            producers[uid] = txid

        total_fees += tx["fee_sats"]

    remaining = {
        uid: value
        for uid, value in values.items()
        if uid not in spent
    }

    supplied = {}
    for item in public["unspent_utxos"]:
        _record(item, {"id", "value_sats"}, "unspent UTXO")
        uid = item["id"]
        _identifier(uid, "unspent UTXO ID")
        _amount(item["value_sats"], "unspent value", positive=True)
        _need(uid not in supplied, "duplicate unspent UTXO")
        supplied[uid] = item["value_sats"]

    _need(supplied == remaining, "unspent UTXO set mismatch")

    remaining_total = sum(remaining.values())

    _need(
        initial_value == remaining_total + total_fees,
        "global value conservation failed",
    )

    return {
        "kind": "mixer_public_ancestry",
        "version": 1,
        "transactions": results,
        "summary": {
            "transaction_count": len(results),
            "initial_utxo_count": len(public["genesis_utxos"]),
            "unspent_utxo_count": len(remaining),
            "initial_value_sats": initial_value,
            "remaining_value_sats": remaining_total,
            "total_fees_sats": total_fees,
        },
    }
