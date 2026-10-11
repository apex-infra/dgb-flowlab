"""Read-only reconciliation of historical Mixer Lab verification evidence.

Historical VERIFIED events are immutable. This module checks whether their
recorded blocks remain on the active chain without modifying any records.
"""

from .pool_verifier import _hash
from .pool_observer import _exact_deposit_sats
from .rpc import RpcError


def _result(state, reason, *, event_id=None, blockhash=None):
    return {
        "state": state,
        "reason": reason,
        "event_id": event_id,
        "blockhash": blockhash,
    }


def reconcile_verification(conn, rpc, *, observation_id):
    """Assess the latest historical VERIFIED block against the active chain."""

    if type(observation_id) is not int or observation_id <= 0:
        raise ValueError("Invalid observation ID")

    observation = conn.execute(
        """
        SELECT id, pool_id, session_id, txid, vout,
               amount_sats, address, confirmations, blockhash, state
        FROM pool_deposit_observations
        WHERE id = ?
        """,
        (observation_id,),
    ).fetchone()

    if observation is None:
        raise ValueError("Unknown deposit observation")

    before = tuple(observation)

    event = conn.execute(
        """
        SELECT id, blockhash, confirmations
        FROM pool_verification_events
        WHERE observation_id = ?
          AND state = 'VERIFIED'
        ORDER BY id DESC
        LIMIT 1
        """,
        (observation_id,),
    ).fetchone()

    if event is None:
        return _result(
            "INDETERMINATE",
            "No historical VERIFIED evidence",
        )

    event_id = event["id"]
    blockhash = _hash(event["blockhash"])

    if blockhash is None or event["confirmations"] <= 0:
        return _result(
            "INDETERMINATE",
            "Historical verification evidence is incomplete",
            event_id=event_id,
        )

    try:
        header = rpc.get_block_header(blockhash)
    except RpcError:
        return _result(
            "INDETERMINATE",
            "Historical block header unavailable",
            event_id=event_id,
            blockhash=blockhash,
        )

    if not isinstance(header, dict):
        return _result(
            "INDETERMINATE",
            "Invalid historical block header",
            event_id=event_id,
            blockhash=blockhash,
        )

    if _hash(header.get("hash")) != blockhash:
        return _result(
            "INDETERMINATE",
            "Historical block identity not established",
            event_id=event_id,
            blockhash=blockhash,
        )

    height = header.get("height")
    confirmations = header.get("confirmations")

    if type(height) is not int or height < 0:
        return _result(
            "INDETERMINATE",
            "Historical block height unavailable",
            event_id=event_id,
            blockhash=blockhash,
        )

    try:
        active_hash = _hash(rpc.get_block_hash(height))
    except RpcError:
        return _result(
            "INDETERMINATE",
            "Active-chain lookup unavailable",
            event_id=event_id,
            blockhash=blockhash,
        )

    if active_hash is None:
        return _result(
            "INDETERMINATE",
            "Invalid active-chain response",
            event_id=event_id,
            blockhash=blockhash,
        )

    if active_hash != blockhash:
        return _result(
            "INVALIDATED",
            "Previously verified block is no longer active",
            event_id=event_id,
            blockhash=blockhash,
        )

    if type(confirmations) is not int:
        return _result(
            "INDETERMINATE",
            "Historical block confirmations unavailable",
            event_id=event_id,
            blockhash=blockhash,
        )

    if confirmations <= 0:
        return _result(
            "INVALIDATED",
            "Previously verified block is orphaned",
            event_id=event_id,
            blockhash=blockhash,
        )

    # Confirm the original transaction still belongs to this block.
    txid = _hash(observation["txid"])

    if txid is None:
        return _result(
            "INDETERMINATE",
            "Invalid historical transaction identity",
            event_id=event_id,
            blockhash=blockhash,
        )

    try:
        anchored = rpc.get_raw_transaction(
            txid,
            verbose=True,
            blockhash=blockhash,
        )
    except RpcError:
        return _result(
            "INDETERMINATE",
            "Historical transaction membership unavailable",
            event_id=event_id,
            blockhash=blockhash,
        )

    if (
        not isinstance(anchored, dict)
        or _hash(anchored.get("txid")) != txid
        or _hash(anchored.get("blockhash")) != blockhash
        or anchored.get("in_active_chain") is not True
    ):
        return _result(
            "INDETERMINATE",
            "Historical transaction membership not established",
            event_id=event_id,
            blockhash=blockhash,
        )

    outputs = anchored.get("vout")

    if not isinstance(outputs, list):
        return _result(
            "INDETERMINATE",
            "Historical transaction outputs unavailable",
            event_id=event_id,
            blockhash=blockhash,
        )

    matches = [
        output
        for output in outputs
        if isinstance(output, dict)
        and type(output.get("n")) is int
        and output["n"] == observation["vout"]
    ]

    if len(matches) != 1:
        return _result(
            "INDETERMINATE",
            "Historical output identity not established",
            event_id=event_id,
            blockhash=blockhash,
        )

    output = matches[0]
    script = output.get("scriptPubKey")

    if (
        not isinstance(script, dict)
        or script.get("address") != observation["address"]
    ):
        return _result(
            "INDETERMINATE",
            "Historical output destination mismatch",
            event_id=event_id,
            blockhash=blockhash,
        )

    try:
        amount = _exact_deposit_sats(output.get("value"))
    except ValueError:
        return _result(
            "INDETERMINATE",
            "Historical output amount unavailable",
            event_id=event_id,
            blockhash=blockhash,
        )

    if amount != observation["amount_sats"]:
        return _result(
            "INDETERMINATE",
            "Historical output amount mismatch",
            event_id=event_id,
            blockhash=blockhash,
        )

    # Recheck the active chain after transaction membership validation.
    try:
        final_hash = _hash(rpc.get_block_hash(height))
    except RpcError:
        return _result(
            "INDETERMINATE",
            "Final active-chain lookup unavailable",
            event_id=event_id,
            blockhash=blockhash,
        )

    if final_hash != blockhash:
        return _result(
            "INDETERMINATE",
            "Active chain changed during reconciliation",
            event_id=event_id,
            blockhash=blockhash,
        )

    # Historical block evidence must also match the observation
    # that existed when reconciliation began.
    if (
        observation["state"] != "CONFIRMED"
        or type(observation["confirmations"]) is not int
        or observation["confirmations"] <= 0
        or _hash(observation["blockhash"]) != blockhash
    ):
        return _result(
            "INDETERMINATE",
            "Current observation disagrees with historical verification",
            event_id=event_id,
            blockhash=blockhash,
        )

    # Re-read the observation after all external RPC calls.
    # Never report CURRENT for evidence checked against stale data.
    current = conn.execute(
        """
        SELECT id, pool_id, session_id, txid, vout,
               amount_sats, address, confirmations, blockhash, state
        FROM pool_deposit_observations
        WHERE id = ?
        """,
        (observation_id,),
    ).fetchone()

    if current is None or tuple(current) != before:
        return _result(
            "INDETERMINATE",
            "Deposit observation changed during reconciliation",
            event_id=event_id,
            blockhash=blockhash,
        )

    return _result(
        "CURRENT",
        "Historical transaction and block remain on active chain",
        event_id=event_id,
        blockhash=blockhash,
    )
