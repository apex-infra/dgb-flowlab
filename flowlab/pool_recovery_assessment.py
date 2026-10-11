"""Read-only Mixer Lab coordination recovery assessment.

Classifies missing, incomplete, and invalidated recorded evidence.
These are operator guidance states, not proof of an interrupted pass.

No RPC calls, database mutations, automatic retries, or financial actions.
"""

from .pool_lifecycle import session_snapshot


def _recovery_action(conn, observation):
    """Classify the latest recorded evidence for one observation."""

    observation_id = observation["observation_id"]

    verification = conn.execute(
        """
        SELECT id, state
        FROM pool_verification_events
        WHERE observation_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (observation_id,),
    ).fetchone()

    if verification is None:
        return "VERIFICATION_REQUIRED"

    if verification["state"] != "VERIFIED":
        return "VERIFICATION_RECHECK_REQUIRED"

    reconciliation = conn.execute(
        """
        SELECT verification_event_id, state
        FROM pool_reconciliation_events
        WHERE observation_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (observation_id,),
    ).fetchone()

    if reconciliation is None:
        return "RECONCILIATION_REQUIRED"

    if reconciliation["verification_event_id"] != verification["id"]:
        return "RECONCILIATION_REQUIRED"

    if observation["evidence_state"] == "INVALIDATED":
        return "EVIDENCE_INVALIDATED"

    if observation["evidence_state"] == "CURRENTLY_SUPPORTED":
        return "NO_ACTION"

    return "EVIDENCE_RECHECK_REQUIRED"


def assess_session_recovery(conn, *, pool_id, session_id):
    """Report outstanding evidence work without performing it."""

    lifecycle = session_snapshot(
        conn,
        pool_id=pool_id,
        session_id=session_id,
    )

    observations = []

    for item in lifecycle["observations"]:
        observations.append({
            "observation_id": item["observation_id"],
            "txid": item["txid"],
            "vout": item["vout"],
            "evidence_state": item["evidence_state"],
            "recovery_action": _recovery_action(conn, item),
        })

    action_required = any(
        item["recovery_action"] != "NO_ACTION"
        for item in observations
    )

    return {
        "pool_id": pool_id,
        "session_id": session_id,
        "lifecycle_state": lifecycle["state"],
        "state": "ACTION_REQUIRED" if action_required else "NO_ACTION",
        "observation_count": len(observations),
        "observations": observations,
        "financial_actions_authorized": False,
    }
