"""Mixer Lab deposit discovery.

Observations are provisional. This module never credits funds,
creates payouts, or broadcasts transactions.
"""

import re
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone

from . import db
from .pool_ledger import MAX_SQLITE_INTEGER


TXID_RE = re.compile(r"^[0-9a-fA-F]{64}$")


SATS_PER_DGB = Decimal("100000000")


def _exact_deposit_sats(amount):
    """Convert a reported DGB amount without rounding satoshis."""

    if (
        amount is None
        or isinstance(amount, bool)
        or not isinstance(amount, (str, int, float, Decimal))
    ):
        raise ValueError("Invalid deposit amount type")

    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError):
        raise ValueError("Invalid deposit amount") from None

    if not value.is_finite() or value <= 0:
        raise ValueError("Deposit amount must be finite and positive")

    # Check the DGB value before multiplication to avoid unnecessary
    # operations on extraordinarily large numeric inputs.
    if value > Decimal(MAX_SQLITE_INTEGER) / SATS_PER_DGB:
        raise ValueError("Deposit exceeds SQLite integer limit")

    try:
        satoshis = value * SATS_PER_DGB
    except InvalidOperation:
        raise ValueError("Invalid deposit precision") from None

    if satoshis != satoshis.to_integral_value():
        raise ValueError("Fractional satoshi amount is not allowed")

    result = int(satoshis)

    if not 0 < result <= MAX_SQLITE_INTEGER:
        raise ValueError("Deposit outside valid satoshi range")

    return result


def _now():
    return datetime.now(timezone.utc).isoformat()


def _state(confirmations):
    if confirmations < 0:
        return "CONFLICTED"
    if confirmations == 0:
        return "UNCONFIRMED"
    return "CONFIRMED"


def observe_session(conn, rpc, *, pool_id, session_id, page_size=100):
    """Discover deposits for one READY session without financial crediting."""

    if type(page_size) is not int or not 1 <= page_size <= 1000:
        raise ValueError("Invalid page size")

    session = conn.execute(
        """
        SELECT a.wallet, a.request_label, a.address
        FROM pool_deposit_addresses a
        JOIN pool_sessions s ON s.id = a.session_id
        WHERE s.id = ? AND s.pool_id = ?
          AND a.state = 'READY'
        """,
        (session_id, pool_id),
    ).fetchone()

    if session is None:
        raise ValueError("READY deposit address not found")

    wallet = session["wallet"]
    label = session["request_label"]
    expected_address = session["address"]

    def scan_once():
        found = {}
        skip = 0

        while True:
            page = rpc.list_transactions(
                wallet,
                label=label,
                count=page_size,
                skip=skip,
                include_watchonly=False,
            )

            if not isinstance(page, list):
                raise ValueError("Invalid transaction-history response")

            for entry in page:
                if not isinstance(entry, dict):
                    raise ValueError("Invalid transaction-history entry")

                if entry.get("category") != "receive":
                    continue

                if entry.get("address") != expected_address:
                    continue

                txid = entry.get("txid")
                vout = entry.get("vout")
                confirmations = entry.get("confirmations")

                if (
                    not isinstance(txid, str)
                    or TXID_RE.fullmatch(txid) is None
                    or type(vout) is not int
                    or vout < 0
                    or type(confirmations) is not int
                ):
                    raise ValueError("Invalid deposit transaction metadata")

                amount = _exact_deposit_sats(entry.get("amount"))

                if amount <= 0:
                    raise ValueError("Deposit amount must be positive")

                blockhash = entry.get("blockhash")

                if blockhash is not None and (
                    not isinstance(blockhash, str)
                    or TXID_RE.fullmatch(blockhash) is None
                ):
                    raise ValueError("Invalid deposit block hash")

                outpoint = (txid.lower(), vout)

                observation = {
                    "txid": outpoint[0],
                    "vout": vout,
                    "amount_sats": amount,
                    "confirmations": confirmations,
                    "blockhash": blockhash.lower() if blockhash else None,
                    "state": _state(confirmations),
                }

                prior = found.get(outpoint)

                if prior is not None and prior != observation:
                    raise ValueError(
                        "Conflicting observations for the same output"
                    )

                found[outpoint] = observation

            if len(page) < page_size:
                break

            skip += len(page)
        return found

    # Offset-based RPC pagination is not an atomic snapshot.
    # Require two consecutive identical complete scans.
    MAX_CONSISTENCY_SCANS = 5
    previous = None
    found = None

    for _ in range(MAX_CONSISTENCY_SCANS):
        current = scan_once()

        if previous is not None and current == previous:
            found = current
            break

        previous = current

    if found is None:
        raise ValueError(
            'Transaction history changed during observation; '
            'refusing incomplete scan'
        )

    inserted = 0
    updated = 0
    now = _now()

    # Persist the completed scan atomically.
    with db.tx(conn):
        for observation in found.values():
            existing = conn.execute(
                """
                SELECT id, pool_id, session_id, address,
                       amount_sats, confirmations, blockhash, state
                FROM pool_deposit_observations
                WHERE txid = ? AND vout = ?
                """,
                (observation["txid"], observation["vout"]),
            ).fetchone()

            if existing is None:
                cursor = conn.execute(
                    """
                    INSERT INTO pool_deposit_observations(
                        pool_id, session_id, txid, vout,
                        amount_sats, address, confirmations,
                        blockhash, state, first_seen_at, last_seen_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        pool_id,
                        session_id,
                        observation["txid"],
                        observation["vout"],
                        observation["amount_sats"],
                        expected_address,
                        observation["confirmations"],
                        observation["blockhash"],
                        observation["state"],
                        now,
                        now,
                    ),
                )
                observation_id = cursor.lastrowid
                inserted += 1
                changed = True
            else:
                if (
                    existing["pool_id"] != pool_id
                    or existing["session_id"] != session_id
                    or existing["address"] != expected_address
                    or existing["amount_sats"] != observation["amount_sats"]
                ):
                    raise ValueError(
                        "Previously observed output has conflicting identity"
                    )

                changed = any(
                    existing[key] != observation[key]
                    for key in ("confirmations", "blockhash", "state")
                )

                observation_id = existing["id"]

                if changed:
                    conn.execute(
                        """
                        UPDATE pool_deposit_observations
                        SET confirmations = ?, blockhash = ?,
                            state = ?, last_seen_at = ?
                        WHERE id = ?
                        """,
                        (
                            observation["confirmations"],
                            observation["blockhash"],
                            observation["state"],
                            now,
                            observation_id,
                        ),
                    )
                    updated += 1

            if changed:
                conn.execute(
                    """
                    INSERT INTO pool_observation_history(
                        observation_id, confirmations,
                        blockhash, state, observed_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        observation_id,
                        observation["confirmations"],
                        observation["blockhash"],
                        observation["state"],
                        now,
                    ),
                )

    return {
        "pool_id": pool_id,
        "session_id": session_id,
        "scanned_outputs": len(found),
        "inserted": inserted,
        "updated": updated,
    }
