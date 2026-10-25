"""Persistent, append-only Mixer Lab reconciliation evidence.

Rechecks historical verification without modifying observations,
financial balances, or historical verification records.
"""

from datetime import datetime, timezone

from . import db
from .pool_recovery_persistence import require_active_lease
from .pool_reconciliation import reconcile_verification
from .pool_verifier import _hash


def _now():
    return datetime.now(timezone.utc).isoformat()


def _snapshot(conn, observation_id):
    """Capture observation state and verification-history generation."""

    observation = conn.execute(
        """
        SELECT pool_id, session_id, txid, vout,
               amount_sats, address, confirmations,
               blockhash, state
        FROM pool_deposit_observations
        WHERE id = ?
        """,
        (observation_id,),
    ).fetchone()

    if observation is None:
        raise ValueError("Unknown deposit observation")

    latest_event = conn.execute(
        """
        SELECT id, state, blockhash, confirmations
        FROM pool_verification_events
        WHERE observation_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (observation_id,),
    ).fetchone()

    latest_verified = conn.execute(
        """
        SELECT id
        FROM pool_verification_events
        WHERE observation_id = ?
          AND state = 'VERIFIED'
        ORDER BY id DESC
        LIMIT 1
        """,
        (observation_id,),
    ).fetchone()

    return (
        tuple(observation),
        tuple(latest_event) if latest_event is not None else None,
        latest_verified["id"] if latest_verified is not None else None,
    )


def record_reconciliation(conn, rpc, *, observation_id, lease=None):
    """Reconcile historical evidence and append an immutable audit event."""

    if type(observation_id) is not int or observation_id <= 0:
        raise ValueError("Invalid observation ID")

    before = _snapshot(conn, observation_id)

    # External RPC calls occur before acquiring SQLite's write lock.
    result = reconcile_verification(
        conn,
        rpc,
        observation_id=observation_id,
    )

    state = result["state"]
    reason = result["reason"]
    verification_event_id = result["event_id"]
    blockhash = result["blockhash"]

    if state not in ("CURRENT", "INVALIDATED", "INDETERMINATE"):
        raise ValueError("Invalid reconciliation state")

    if not isinstance(reason, str) or not reason:
        raise ValueError("Invalid reconciliation reason")

    if blockhash is not None:
        blockhash = _hash(blockhash)

        if blockhash is None:
            raise ValueError("Invalid reconciliation block hash")

    if verification_event_id is not None:
        if (
            type(verification_event_id) is not int
            or verification_event_id <= 0
        ):
            raise ValueError("Invalid historical verification event")

    if state == "CURRENT" and (
        verification_event_id is None or blockhash is None
    ):
        raise ValueError("Incomplete CURRENT reconciliation evidence")

    with db.tx(conn):
        if lease is not None:
            require_active_lease(conn, lease)
        after = _snapshot(conn, observation_id)

        if after != before:
            raise ValueError(
                "Observation or verification history changed "
                "during reconciliation"
            )

        # Ensure the reconciler referenced the latest VERIFIED event.
        if verification_event_id != after[2]:
            raise ValueError(
                "Reconciliation references outdated verification evidence"
            )

        cursor = conn.execute(
            """
            INSERT INTO pool_reconciliation_events(
                observation_id, verification_event_id,
                state, reason, blockhash, reconciled_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                observation_id,
                verification_event_id,
                state,
                reason,
                blockhash,
                _now(),
            ),
        )

    return {
        "event_id": cursor.lastrowid,
        "observation_id": observation_id,
        "verification_event_id": verification_event_id,
        "state": state,
        "reason": reason,
        "blockhash": blockhash,
    }


def reconciliation_history(conn, *, observation_id):
    """Return reconciliation events in insertion order."""

    if type(observation_id) is not int or observation_id <= 0:
        raise ValueError("Invalid observation ID")

    rows = conn.execute(
        """
        SELECT id, observation_id, verification_event_id,
               state, reason, blockhash, reconciled_at
        FROM pool_reconciliation_events
        WHERE observation_id = ?
        ORDER BY id ASC
        """,
        (observation_id,),
    ).fetchall()

    return [dict(row) for row in rows]
