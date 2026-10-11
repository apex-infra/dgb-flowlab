"""Read-only verification of observed Mixer Lab deposits.

Never credits participants, updates observations, or broadcasts transactions.
Verification describes blockchain evidence at the time of the RPC calls.
"""

import re

from .pool_observer import _exact_deposit_sats
from .rpc import RpcError


HASH_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def _hash(value):
    if not isinstance(value, str) or not HASH_RE.fullmatch(value):
        return None
    return value.lower()


def _result(state, reason, confirmations=0, blockhash=None):
    return {
        "state": state,
        "reason": reason,
        "confirmations": confirmations,
        "blockhash": blockhash,
    }


def verify_observation(conn, rpc, *, observation_id):
    """Verify a stored observation using independent node RPC evidence."""

    if type(observation_id) is not int or observation_id <= 0:
        raise ValueError("Invalid observation ID")

    row = conn.execute(
        """
        SELECT o.id, o.pool_id, o.session_id, o.txid, o.vout,
               o.amount_sats, o.address, o.blockhash,
               o.confirmations, o.state,
               a.address AS registered_address, a.state AS address_state
        FROM pool_deposit_observations o
        JOIN pool_deposit_addresses a ON a.session_id = o.session_id
        JOIN pool_sessions s
          ON s.id = o.session_id AND s.pool_id = o.pool_id
        WHERE o.id = ?
        """,
        (observation_id,),
    ).fetchone()

    if row is None:
        raise ValueError("Unknown deposit observation")

    if (
        row["address_state"] != "READY"
        or row["registered_address"] != row["address"]
    ):
        return _result("REJECTED", "Registration/address mismatch")

    txid = _hash(row["txid"])

    if txid is None:
        return _result("REJECTED", "Invalid observed transaction ID")

    if row["state"] == "CONFLICTED" or row["confirmations"] < 0:
        return _result("PENDING", "Observed transaction is conflicted")

    # Confirmation evidence must agree with the stored state.
    # A later observer refresh can resolve a stale observation.
    if (
        row["state"] != "CONFIRMED"
        or type(row["confirmations"]) is not int
        or row["confirmations"] <= 0
        or _hash(row["blockhash"]) is None
    ):
        return _result(
            "PENDING",
            "Stored observation is not consistently confirmed",
        )

    # No block lookup is needed for a transaction that has not been
    # observed in a block. It can be checked for output identity, but
    # cannot be declared VERIFIED.
    try:
        raw = rpc.get_raw_transaction(txid, verbose=True)
    except RpcError:
        return _result("PENDING", "Transaction unavailable from node")

    if not isinstance(raw, dict) or _hash(raw.get("txid")) != txid:
        return _result("REJECTED", "Transaction identity mismatch")

    outputs = raw.get("vout")

    if not isinstance(outputs, list):
        return _result("REJECTED", "Missing transaction outputs")

    matches = [
        output for output in outputs
        if isinstance(output, dict)
        and type(output.get("n")) is int
        and output["n"] == row["vout"]
    ]

    if len(matches) != 1:
        return _result("REJECTED", "Expected output not found uniquely")

    output = matches[0]
    script = output.get("scriptPubKey")

    if not isinstance(script, dict):
        return _result("REJECTED", "Missing output destination")

    # DigiByte Core 9.26.6 exposes the address field for outputs
    # having a well-defined destination. Do not guess when absent.
    if script.get("address") != row["address"]:
        return _result("REJECTED", "Output destination mismatch")

    try:
        sats = _exact_deposit_sats(output.get("value"))
    except ValueError:
        return _result("REJECTED", "Invalid output amount")

    if sats != row["amount_sats"]:
        return _result("REJECTED", "Output amount mismatch")

    blockhash = _hash(raw.get("blockhash"))

    if blockhash is None:
        return _result("PENDING", "Matching output is unconfirmed")

    # The observation may become stale during a reorganization.
    # Do not silently substitute a newly observed block.
    if _hash(row["blockhash"]) != blockhash:
        return _result("PENDING", "Observed block identity changed")

    try:
        header = rpc.get_block_header(blockhash)
    except RpcError:
        return _result("PENDING", "Block header unavailable")

    if not isinstance(header, dict):
        return _result("PENDING", "Invalid block header response")

    height = header.get("height")
    header_confirmations = header.get("confirmations")

    if (
        _hash(header.get("hash")) != blockhash
        or type(height) is not int
        or height < 0
        or type(header_confirmations) is not int
        or header_confirmations <= 0
    ):
        return _result("PENDING", "Block is not confirmed on active chain")

    try:
        active_hash = rpc.get_block_hash(height)
    except RpcError:
        return _result("PENDING", "Active-chain lookup unavailable")

    if _hash(active_hash) != blockhash:
        return _result("PENDING", "Block is not on active chain")

    # Explicit-block lookup provides independent membership evidence
    # and exposes the in_active_chain field.
    try:
        anchored = rpc.get_raw_transaction(
            txid,
            verbose=True,
            blockhash=blockhash,
        )
    except RpcError:
        return _result("PENDING", "Block-specific transaction unavailable")

    if not isinstance(anchored, dict):
        return _result("PENDING", "Invalid block-specific response")

    if (
        _hash(anchored.get("txid")) != txid
        or _hash(anchored.get("blockhash")) != blockhash
        or anchored.get("in_active_chain") is not True
    ):
        return _result("PENDING", "Active-chain membership not established")


    # Validate the output again using the block-specific transaction.
    # Never trust the initial lookup as proof of block membership.
    anchored_outputs = anchored.get("vout")

    if not isinstance(anchored_outputs, list):
        return _result("PENDING", "Block-specific outputs unavailable")

    anchored_matches = [
        output for output in anchored_outputs
        if isinstance(output, dict)
        and type(output.get("n")) is int
        and output["n"] == row["vout"]
    ]

    if len(anchored_matches) != 1:
        return _result("PENDING", "Block-specific output mismatch")

    anchored_output = anchored_matches[0]
    anchored_script = anchored_output.get("scriptPubKey")

    if not isinstance(anchored_script, dict):
        return _result("PENDING", "Block-specific destination unavailable")

    if anchored_script.get("address") != row["address"]:
        return _result("PENDING", "Block-specific destination mismatch")

    try:
        anchored_sats = _exact_deposit_sats(
            anchored_output.get("value")
        )
    except ValueError:
        return _result("PENDING", "Invalid block-specific amount")

    if anchored_sats != row["amount_sats"]:
        return _result("PENDING", "Block-specific output amount mismatch")

    raw_confirmations = raw.get("confirmations")
    anchored_confirmations = anchored.get("confirmations")

    if (
        type(raw_confirmations) is not int
        or type(anchored_confirmations) is not int
        or raw_confirmations != header_confirmations
        or anchored_confirmations != header_confirmations
    ):
        return _result("PENDING", "Confirmation evidence changed during check")

    # A final active-chain lookup narrows the window for a reorg
    # occurring between the earlier node queries.
    try:
        final_hash = rpc.get_block_hash(height)
    except RpcError:
        return _result("PENDING", "Final active-chain lookup unavailable")

    if _hash(final_hash) != blockhash:
        return _result("PENDING", "Active chain changed during verification")

    return _result(
        "VERIFIED",
        "Output and active-chain evidence match",
        confirmations=header_confirmations,
        blockhash=blockhash,
    )
