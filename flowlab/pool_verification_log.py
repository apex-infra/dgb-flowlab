"""Append-only Mixer Lab blockchain-verification evidence.

Records independent verification results without financial crediting.
"""

from datetime import datetime, timezone

from . import db
from .pool_recovery_persistence import require_active_lease
from .pool_verifier import verify_observation, _hash


def _now():
    return datetime.now(timezone.utc).isoformat()



def _observation_snapshot(conn, observation_id):
    """Capture the identity and blockchain state of an observation."""

    row = conn.execute(
        """
        SELECT pool_id, session_id, txid, vout,
               amount_sats, address, confirmations,
               blockhash, state
        FROM pool_deposit_observations
        WHERE id = ?
        """,
        (observation_id,),
    ).fetchone()

    if row is None:
        raise ValueError("Unknown deposit observation")

    return tuple(row)



def record_verification(conn, rpc, *, observation_id, lease=None):
    """Verify a deposit and permanently record the verification outcome."""

    if type(observation_id) is not int or observation_id <= 0:
        raise ValueError("Invalid observation ID")

    # Snapshot the observation before querying the node.
    before = _observation_snapshot(conn, observation_id)

    # Query the node before acquiring SQLite's write lock.
    result = verify_observation(
        conn,
        rpc,
        observation_id=observation_id,
    )

    state = result["state"]
    reason = result["reason"]
    confirmations = result["confirmations"]
    blockhash = result["blockhash"]

    if state not in ("VERIFIED", "PENDING", "REJECTED"):
        raise ValueError("Unexpected verification state")

    if not isinstance(reason, str) or not reason:
        raise ValueError("Invalid verification reason")

    if type(confirmations) is not int or confirmations < 0:
        raise ValueError("Invalid verification confirmations")

    if blockhash is not None:
        blockhash = _hash(blockhash)

        if blockhash is None:
            raise ValueError("Invalid verification block hash")

    if state == "VERIFIED" and (
        confirmations <= 0 or blockhash is None
    ):
        raise ValueError("Incomplete verified evidence")

    with db.tx(conn):
        if lease is not None:
            require_active_lease(conn, lease)
        # BEGIN IMMEDIATE serializes this check and insertion
        # against other SQLite writers.
        after = _observation_snapshot(conn, observation_id)

        if after != before:
            raise ValueError(
                'Deposit observation changed during verification'
            )

        cursor = conn.execute(
            """
            INSERT INTO pool_verification_events(
                observation_id, state, reason,
                confirmations, blockhash, verified_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                observation_id,
                state,
                reason,
                confirmations,
                blockhash,
                _now(),
            ),
        )

    return {
        "event_id": cursor.lastrowid,
        "observation_id": observation_id,
        **result,
    }


def verification_history(conn, *, observation_id):
    """Return immutable verification events in chronological order."""

    if type(observation_id) is not int or observation_id <= 0:
        raise ValueError("Invalid observation ID")

    rows = conn.execute(
        """
        SELECT id, observation_id, state, reason,
               confirmations, blockhash, verified_at
        FROM pool_verification_events
        WHERE observation_id = ?
        ORDER BY id ASC
        """,
        (observation_id,),
    ).fetchall()

    return [dict(row) for row in rows]
