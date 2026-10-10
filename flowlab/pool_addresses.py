"""Persistent Mixer Lab deposit-address management.

No address is returned to a participant until it is verified and saved.
An uncertain request is never automatically repeated.
"""

import hashlib
import sqlite3
from datetime import datetime, timezone

from . import db


class AddressRecoveryRequired(RuntimeError):
    """An address request needs explicit reconciliation."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _label(pool_id, session_id):
    material = f"{len(pool_id)}:{pool_id}:{session_id}".encode("utf-8")
    digest = hashlib.sha256(material).hexdigest()[:32]
    return f"flowlab:deposit:{digest}"


def address_status(conn, *, session_id):
    row = conn.execute(
        """
        SELECT session_id, wallet, request_label, address, state
        FROM pool_deposit_addresses
        WHERE session_id = ?
        """,
        (session_id,),
    ).fetchone()

    return dict(row) if row else None


def request_deposit_address(conn, rpc, *, pool_id, session_id):
    """Request one address, refusing automatic retries after uncertainty."""

    if not isinstance(pool_id, str) or not pool_id:
        raise ValueError("Pool ID is required")

    if not isinstance(session_id, str) or not session_id:
        raise ValueError("Session ID is required")

    with db.tx(conn):
        session = conn.execute(
            """
            SELECT p.reserve_wallet
            FROM pool_sessions s
            JOIN pools p ON p.id = s.pool_id
            JOIN pool_registration_seals seal
                ON seal.session_id = s.id
            WHERE s.id = ? AND s.pool_id = ?
            """,
            (session_id, pool_id),
        ).fetchone()

        if session is None:
            raise ValueError("Completed registration not found")

        existing = address_status(conn, session_id=session_id)

        if existing is not None:
            if existing["state"] == "READY":
                return existing

            raise AddressRecoveryRequired(
                "Address request already exists; reconciliation required"
            )

        wallet = session["reserve_wallet"]
        label = _label(pool_id, session_id)
        now = _now()

        conn.execute(
            """
            INSERT INTO pool_deposit_addresses(
                session_id, wallet, request_label,
                address, state, created_at, updated_at
            )
            VALUES (?, ?, ?, NULL, 'REQUESTING', ?, ?)
            """,
            (session_id, wallet, label, now, now),
        )

    # The REQUESTING record is durably committed before the RPC call.
    # A crash from this point onward must never trigger a blind retry.
    try:
        address = rpc.get_new_address(wallet, label)

        if not isinstance(address, str) or not address:
            raise ValueError("Node returned an invalid deposit address")

        info = rpc.get_address_info(wallet, address)

        if not isinstance(info, dict) or info.get("ismine") is not True:
            raise ValueError("Node did not verify deposit-address ownership")

    except Exception as exc:
        with db.tx(conn):
            conn.execute(
                """
                UPDATE pool_deposit_addresses
                SET state = 'RECOVERY_REQUIRED', updated_at = ?
                WHERE session_id = ?
                  AND state = 'REQUESTING'
                """,
                (_now(), session_id),
            )

        raise AddressRecoveryRequired(
            "Deposit-address request uncertain; reconciliation required"
        ) from exc

    try:
        with db.tx(conn):
            cursor = conn.execute(
                """
                UPDATE pool_deposit_addresses
                SET address = ?, state = 'READY', updated_at = ?
                WHERE session_id = ? AND state = 'REQUESTING'
                """,
                (address, _now(), session_id),
            )

            if cursor.rowcount != 1:
                raise AddressRecoveryRequired(
                    "Address request changed before persistence"
                )

    except sqlite3.Error as exc:
        raise AddressRecoveryRequired(
            "Could not persist generated address; reconciliation required"
        ) from exc

    return address_status(conn, session_id=session_id)


def recover_deposit_address(conn, rpc, *, pool_id, session_id):
    """Recover an uncertain request without generating a new address."""

    if not isinstance(pool_id, str) or not pool_id:
        raise ValueError("Pool ID is required")

    if not isinstance(session_id, str) or not session_id:
        raise ValueError("Session ID is required")

    row = conn.execute(
        """
        SELECT a.session_id, a.wallet, a.request_label,
               a.address, a.state
        FROM pool_deposit_addresses a
        JOIN pool_sessions s ON s.id = a.session_id
        WHERE a.session_id = ? AND s.pool_id = ?
        """,
        (session_id, pool_id),
    ).fetchone()

    if row is None:
        raise ValueError("Address request not found")

    if row["state"] == "READY":
        return dict(row)

    if row["state"] not in ("REQUESTING", "RECOVERY_REQUIRED"):
        raise AddressRecoveryRequired("Unknown address-request state")

    try:
        addresses = rpc.get_addresses_by_label(
            row["wallet"],
            row["request_label"],
        )

        if not isinstance(addresses, dict):
            raise ValueError("Unexpected address-label response")

        candidates = [
            address
            for address, metadata in addresses.items()
            if isinstance(metadata, dict)
            and metadata.get("purpose") == "receive"
        ]

        if len(candidates) != 1:
            raise ValueError(
                "Recovery requires exactly one receiving address"
            )

        address = candidates[0]

        info = rpc.get_address_info(row["wallet"], address)

        if not isinstance(info, dict) or info.get("ismine") is not True:
            raise ValueError("Recovery address ownership not verified")

    except Exception as exc:
        with db.tx(conn):
            conn.execute(
                """
                UPDATE pool_deposit_addresses
                SET state = 'RECOVERY_REQUIRED', updated_at = ?
                WHERE session_id = ?
                  AND state IN ('REQUESTING', 'RECOVERY_REQUIRED')
                """,
                (_now(), session_id),
            )

        raise AddressRecoveryRequired(
            "Could not safely reconcile deposit address"
        ) from exc

    try:
        with db.tx(conn):
            cursor = conn.execute(
                """
                UPDATE pool_deposit_addresses
                SET address = ?, state = 'READY', updated_at = ?
                WHERE session_id = ?
                  AND state IN ('REQUESTING', 'RECOVERY_REQUIRED')
                """,
                (address, _now(), session_id),
            )

            if cursor.rowcount != 1:
                raise AddressRecoveryRequired(
                    "Address request changed during recovery"
                )

    except sqlite3.Error as exc:
        raise AddressRecoveryRequired(
            "Could not persist recovered address"
        ) from exc

    return address_status(conn, session_id=session_id)
