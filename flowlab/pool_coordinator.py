"""Operator-invoked Mixer Lab deposit evidence coordination.

Observes deposits, records independent verification and reconciliation
evidence, and returns participant lifecycle telemetry.

No financial crediting, settlement, transaction construction, or broadcast.
"""

from .pool_lifecycle import session_snapshot
from .pool_observer import observe_session
from .pool_verification_log import record_verification
from .pool_reconciliation_log import record_reconciliation


def coordinate_session(
    conn,
    rpc,
    *,
    pool_id,
    session_id,
    page_size=100,
):
    """Run one explicit observation/evidence pass for a participant.

    Each completed operation persists independently. Exceptions propagate
    so callers cannot mistake an interrupted pass for a successful one.
    """

    if type(page_size) is not int or not 1 <= page_size <= 1000:
        raise ValueError("Invalid page size")

    before = session_snapshot(
        conn,
        pool_id=pool_id,
        session_id=session_id,
    )

    if not before["registration_complete"]:
        raise ValueError("Participant registration is incomplete")

    if before["address_state"] != "READY":
        raise ValueError("READY deposit address required")

    observation_result = observe_session(
        conn,
        rpc,
        pool_id=pool_id,
        session_id=session_id,
        page_size=page_size,
    )

    observation_ids = [
        row["id"]
        for row in conn.execute(
            """
            SELECT id
            FROM pool_deposit_observations
            WHERE pool_id = ? AND session_id = ?
            ORDER BY id ASC
            """,
            (pool_id, session_id),
        )
    ]

    results = []

    for observation_id in observation_ids:
        verification = record_verification(
            conn,
            rpc,
            observation_id=observation_id,
        )

        reconciliation = None

        if verification["state"] == "VERIFIED":
            reconciliation = record_reconciliation(
                conn,
                rpc,
                observation_id=observation_id,
            )

        results.append({
            "observation_id": observation_id,
            "verification": verification,
            "reconciliation": reconciliation,
        })

    lifecycle = session_snapshot(
        conn,
        pool_id=pool_id,
        session_id=session_id,
    )

    if lifecycle["state"] == "READY_FOR_REVIEW":
        review_state = "READY_FOR_REVIEW"
    elif lifecycle["state"] == "AWAITING_DEPOSIT":
        review_state = "AWAITING_DEPOSIT"
    elif lifecycle["state"] == "EVIDENCE_REVIEW_REQUIRED":
        review_state = "EVIDENCE_REVIEW_REQUIRED"
    else:
        review_state = "RECHECK_REQUIRED"

    return {
        "pass_state": "COMPLETED",
        "review_state": review_state,
        "pool_id": pool_id,
        "session_id": session_id,
        "observation": observation_result,
        "processed_observations": len(results),
        "results": results,
        "lifecycle": lifecycle,
        "financial_actions_authorized": False,
    }
