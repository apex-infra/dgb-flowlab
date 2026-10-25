"""Mixer Lab automatic evidence recovery supervisor.

Initial component: read-only eligibility discovery.
No RPC calls, financial operations, or background workers.
"""

from .pool_lifecycle import session_snapshot
from .pool_recovery_coordinator import recover_session


def eligible_sessions(conn):
    """Discover registered sessions eligible for evidence monitoring."""

    rows = conn.execute(
        """
        SELECT s.pool_id, s.id AS session_id
        FROM pool_sessions s
        JOIN pools p ON p.id = s.pool_id
        WHERE p.state = 'ACTIVE'
          AND s.state = 'REGISTERED'
        ORDER BY s.pool_id, s.id
        """
    ).fetchall()

    eligible = []

    for row in rows:
        snapshot = session_snapshot(
            conn,
            pool_id=row["pool_id"],
            session_id=row["session_id"],
        )

        if (
            snapshot["registration_complete"]
            and snapshot["address_state"] == "READY"
        ):
            eligible.append({
                "pool_id": row["pool_id"],
                "session_id": row["session_id"],
            })

    return eligible


def run_recovery_pass(conn, rpc):
    """Process eligible evidence sessions in one supervised pass.

    Each session is isolated so an RPC or evidence failure does not
    prevent subsequent sessions from being processed.
    """


    sessions = eligible_sessions(conn)
    results = []

    for session in sessions:
        pool_id = session["pool_id"]
        session_id = session["session_id"]

        try:
            recovery = recover_session(
                conn,
                rpc,
                pool_id=pool_id,
                session_id=session_id,
            )

            if recovery.get("pass_state") != "COMPLETED":
                raise RuntimeError("Recovery pass did not complete")

            results.append({
                "pool_id": pool_id,
                "session_id": session_id,
                "state": "COMPLETED",
                "review_state": recovery.get(
                    "coordination", {}
                ).get("review_state"),
            })

        except Exception as error:
            results.append({
                "pool_id": pool_id,
                "session_id": session_id,
                "state": "FAILED",
                "error_type": type(error).__name__,
            })

    return {
        "completed": sum(
            item["state"] == "COMPLETED" for item in results
        ),
        "failed": sum(
            item["state"] == "FAILED" for item in results
        ),
        "results": results,
        "financial_actions_authorized": False,
    }
